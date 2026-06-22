# Codex Session Host Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 Codex CLI / Codex App 的只读宿主检测能力，明确 direct PTY、tmux detached、tmux attached 和 Codex App thread 的展示与激活置信度。

**Architecture:** 新增 `agent_deck.hosts` 包承载宿主上下文模型、进程表读取、tmux 只读探测和 Codex resolver。CLI 只调用 resolver 并输出 JSON，不执行聚焦动作；后续 `focus_agent` 再消费结构化 activation target。

**Tech Stack:** Python 3.11+、Pydantic 2、Typer、pytest、标准库 `subprocess`/`datetime`/`pathlib`；不新增第三方依赖。

## Global Constraints

- 默认中文文档注释；新增或修改的代码文件顶部必须有文档级注释。
- 新增函数、方法、类、协议、对外符号必须有中文 docstring，说明语义、入参约束、返回值、错误处理和副作用。
- 所有时间字段必须使用 timezone-aware `datetime`。
- host detection 默认只读；不得启动终端、attach tmux、写 Codex App SQLite、写 rollout JSONL 或执行 focus。
- 不读取或上传完整环境变量；只允许 allowlist host metadata。
- 事件 payload 和 CLI 输出不得泄露 token、secret、authorization、api_key、password。
- 真实 tmux / ps / Codex App 状态只能作为 smoke/manual 验证；自动测试必须使用 fake reader 或 fixture。

---

## File Structure

- Create: `src/agent_deck/hosts/__init__.py`
  - 导出 host context 模型和 resolver 入口。
- Create: `src/agent_deck/hosts/models.py`
  - 定义 `RuntimeKind`、`ExecutionHostKind`、`PresentationClientKind`、`ActivationStrategy`、`Confidence`、`ExecutionHostContext`、`PresentationClientContext`、`ActivationContext`、`AgentHostContext`。
- Create: `src/agent_deck/hosts/processes.py`
  - 定义可测试的 `ProcessInfo`、`ProcessTable` 协议、`StaticProcessTable`、`MacOSProcessTable` 和进程链解析 helper。
- Create: `src/agent_deck/hosts/tmux.py`
  - 定义 `TmuxPane`、`TmuxClient`、`TmuxSnapshot`、`TmuxReader` 协议、`SubprocessTmuxReader`、`StaticTmuxReader` 和 pane/client 匹配 helper。
- Create: `src/agent_deck/hosts/codex.py`
  - 定义 `CodexHostResolver`，合并 hook pid、process table、tmux snapshot、Codex App active sessions。
- Modify: `src/agent_deck/cli.py`
  - 新增 `agent-deckctl codex-hosts --json` 命令。
- Test: `tests/test_host_context.py`
  - 覆盖模型校验、direct PTY、tmux detached、tmux attached、multiple clients、missing pid 降级。
- Test: `tests/test_cli.py`
  - 覆盖 CLI 命令调用 resolver 并输出 JSON。
- Modify: `docs/references/agent-deck-roadmap.md`
  - 已同步 tmux execution host / presentation client 边界；代码任务完成后只需补充命令名。

---

### Task 1: Host Context Models

**Files:**
- Create: `src/agent_deck/hosts/__init__.py`
- Create: `src/agent_deck/hosts/models.py`
- Test: `tests/test_host_context.py`

**Interfaces:**
- Produces: `AgentHostContext`, `ExecutionHostContext`, `PresentationClientContext`, `ActivationContext`
- Produces enums: `RuntimeKind`, `ExecutionHostKind`, `PresentationClientKind`, `ActivationStrategy`, `Confidence`
- Consumes: no project runtime dependencies beyond Pydantic and timezone-aware `datetime`

- [ ] **Step 1: Write failing model tests**

Add to `tests/test_host_context.py`:

```python
"""Codex 会话宿主上下文模型测试。

这些测试只构造内存模型，不读取真实进程、tmux、Codex App 状态或硬件。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent_deck.hosts.models import (
    ActivationContext,
    ActivationStrategy,
    AgentHostContext,
    Confidence,
    ExecutionHostContext,
    ExecutionHostKind,
    RuntimeKind,
)


def test_agent_host_context_accepts_tmux_detached_target() -> None:
    """验证 tmux detached 会话能表达 reattach 激活策略。

    入参：无；测试内构造固定 timezone-aware 时间与 tmux pane 字段。
    返回：无返回值；断言通过代表模型字段和枚举值可序列化。
    错误处理：字段缺失或枚举非法时由 pytest 报告。
    副作用：只创建内存 Pydantic model。
    """

    observed_at = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)
    context = AgentHostContext(
        runtime_kind=RuntimeKind.CODEX_CLI,
        execution_host=ExecutionHostContext(
            kind=ExecutionHostKind.TMUX_PANE,
            tmux_session_name="agent",
            tmux_window_id="@1",
            tmux_window_index=0,
            tmux_pane_id="%7",
            tmux_pane_index=1,
            pane_tty="/dev/ttys006",
            pane_pid=90077,
            attached=False,
        ),
        activation=ActivationContext(
            strategy=ActivationStrategy.TMUX_REATTACH_NEW_CLIENT,
            confidence=Confidence.HIGH,
            target={"tmux_pane_id": "%7", "tmux_session_name": "agent"},
            requires_terminal_launch=True,
        ),
        agent_pid=73879,
        tty="ttys006",
        observed_at=observed_at,
        confidence=Confidence.HIGH,
    )

    payload = context.model_dump(mode="json")
    assert payload["runtime_kind"] == "codex_cli"
    assert payload["execution_host"]["kind"] == "tmux_pane"
    assert payload["activation"]["strategy"] == "tmux_reattach_new_client"


def test_agent_host_context_rejects_naive_observed_at() -> None:
    """验证宿主检测时间必须带时区。

    入参：无；测试内传入 naive datetime。
    返回：无返回值；断言通过代表模型拒绝模糊时间。
    错误处理：未抛 ValidationError 时由 pytest 报告。
    副作用：只创建内存 Pydantic model。
    """

    with pytest.raises(ValidationError):
        AgentHostContext(
            runtime_kind=RuntimeKind.UNKNOWN,
            execution_host=ExecutionHostContext(kind=ExecutionHostKind.UNKNOWN),
            activation=ActivationContext(
                strategy=ActivationStrategy.UNAVAILABLE,
                confidence=Confidence.LOW,
            ),
            observed_at=datetime(2026, 6, 22, 8, 0),
            confidence=Confidence.LOW,
        )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_host_context.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_deck.hosts'`.

- [ ] **Step 3: Implement host model package**

Create `src/agent_deck/hosts/__init__.py`:

```python
"""Agent Deck 会话宿主检测包。

本包提供只读宿主上下文模型、进程表读取、tmux 探测和 Codex resolver。它不执行
focus、不启动终端、不写 Codex 配置或本地状态；真实副作用只发生在调用生产 reader
读取进程表或 tmux 状态时。
"""

from agent_deck.hosts.models import (
    ActivationContext,
    ActivationStrategy,
    AgentHostContext,
    Confidence,
    ExecutionHostContext,
    ExecutionHostKind,
    PresentationClientContext,
    PresentationClientKind,
    RuntimeKind,
)

__all__ = [
    "ActivationContext",
    "ActivationStrategy",
    "AgentHostContext",
    "Confidence",
    "ExecutionHostContext",
    "ExecutionHostKind",
    "PresentationClientContext",
    "PresentationClientKind",
    "RuntimeKind",
]
```

Create `src/agent_deck/hosts/models.py` with the enums and Pydantic models shown by the tests. Add field validators for `observed_at` and `pid_start_time` to reject naive datetimes. `ActivationContext.target` should default to an empty immutable mapping-compatible dict field using `Field(default_factory=dict)`.

- [ ] **Step 4: Run model tests**

Run:

```bash
uv run pytest tests/test_host_context.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit task**

```bash
git add src/agent_deck/hosts/__init__.py src/agent_deck/hosts/models.py tests/test_host_context.py
git commit -m "feat(hosts): 增加 Codex 宿主上下文模型"
```

---

### Task 2: Process Table and TTY Host Inference

**Files:**
- Create: `src/agent_deck/hosts/processes.py`
- Modify: `tests/test_host_context.py`

**Interfaces:**
- Consumes: `ExecutionHostContext`, `ExecutionHostKind`, `PresentationClientContext`, `PresentationClientKind`, `Confidence`
- Produces: `ProcessInfo(pid: int, ppid: int | None, command: str, args: tuple[str, ...], tty: str | None, cwd: str | None, started_at: datetime | None)`
- Produces: `StaticProcessTable`, `MacOSProcessTable`, `process_chain(table, pid)`, `infer_direct_pty_host(chain)`

- [ ] **Step 1: Write failing process inference tests**

Append to `tests/test_host_context.py`:

```python
from agent_deck.hosts.processes import ProcessInfo, StaticProcessTable, infer_direct_pty_host, process_chain


def test_process_chain_finds_otty_direct_pty_host() -> None:
    """验证 Codex CLI 进程可通过父进程链归因到 Otty direct PTY。

    入参：无；测试内构造 codex -> zsh -> login -> Otty 的静态进程表。
    返回：无返回值；断言通过代表直接 PTY 宿主可被识别。
    错误处理：链路顺序或 host 推断不符合预期时由 pytest 报告。
    副作用：只读取测试内存进程表。
    """

    table = StaticProcessTable(
        {
            15010: ProcessInfo(pid=15010, ppid=11910, command="codex", args=("codex", "resume"), tty="ttys003"),
            11910: ProcessInfo(pid=11910, ppid=11904, command="-zsh", args=("-zsh",), tty="ttys003"),
            11904: ProcessInfo(pid=11904, ppid=16260, command="/usr/bin/login", args=("/usr/bin/login",), tty="ttys003"),
            16260: ProcessInfo(pid=16260, ppid=1, command="/Applications/Otty.app/Contents/MacOS/Otty", args=("/Applications/Otty.app/Contents/MacOS/Otty",), tty=None),
        }
    )

    chain = process_chain(table, 15010)
    host = infer_direct_pty_host(chain)

    assert [item.pid for item in chain] == [15010, 11910, 11904, 16260]
    assert host.app_name == "Otty"
    assert host.app_pid == 16260
    assert host.confidence == Confidence.MEDIUM
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run pytest tests/test_host_context.py::test_process_chain_finds_otty_direct_pty_host -q
```

Expected: FAIL with `ModuleNotFoundError` or missing function error for `agent_deck.hosts.processes`.

- [ ] **Step 3: Implement process helpers**

Create `src/agent_deck/hosts/processes.py`:

```python
"""macOS 进程表读取与 Codex CLI 父进程链推断。

本模块把真实 `ps` 输出转换为可测试的 `ProcessInfo` 记录，并提供纯函数用于从
Codex pid 上溯宿主进程。生产 reader 只做只读进程枚举；测试可使用 `StaticProcessTable`
避免访问真实系统进程。
"""
```

Implement:

- `ProcessInfo` as frozen Pydantic model with documented fields.
- `ProcessTable` protocol with `get(pid: int) -> ProcessInfo | None`.
- `StaticProcessTable` copying a mapping in memory.
- `process_chain(table, pid, max_depth=32) -> tuple[ProcessInfo, ...]`, stopping on missing pid, pid cycle, or `ppid in {None, 0}`.
- `infer_direct_pty_host(chain) -> PresentationClientContext | None`, matching app names from command paths containing `.app/Contents/MacOS/`.
- `MacOSProcessTable.read()` that runs:

```bash
ps -axo pid=,ppid=,tty=,lstart=,command=
```

Parse enough fields for PID, PPID, TTY and command. If parsing `lstart` is fragile, set `started_at=None` and leave a docstring note; do not guess local timezone without converting to timezone-aware datetime.

- [ ] **Step 4: Run process tests**

Run:

```bash
uv run pytest tests/test_host_context.py -q
```

Expected: all existing host context tests pass.

- [ ] **Step 5: Commit task**

```bash
git add src/agent_deck/hosts/processes.py tests/test_host_context.py
git commit -m "feat(hosts): 增加 Codex 进程链宿主推断"
```

---

### Task 3: tmux Snapshot Reader and Pane Matching

**Files:**
- Create: `src/agent_deck/hosts/tmux.py`
- Modify: `tests/test_host_context.py`

**Interfaces:**
- Consumes: `ProcessInfo.tty`
- Produces: `TmuxPane`, `TmuxClient`, `TmuxSnapshot`, `StaticTmuxReader`, `SubprocessTmuxReader`
- Produces: `find_pane_for_tty(snapshot, tty)`, `clients_for_session(snapshot, session_name)`

- [ ] **Step 1: Write failing tmux tests**

Append to `tests/test_host_context.py`:

```python
from agent_deck.hosts.tmux import TmuxClient, TmuxPane, TmuxSnapshot, clients_for_session, find_pane_for_tty


def test_tmux_snapshot_detects_detached_pane_by_tty() -> None:
    """验证 tmux pane 可通过 pane_tty 绑定 Codex CLI TTY，并识别 detached。

    入参：无；测试内构造没有 client 的 tmux snapshot。
    返回：无返回值；断言通过代表 detached pane 可被匹配。
    错误处理：pane 匹配失败时由 pytest 报告。
    副作用：只读取测试内存 snapshot。
    """

    snapshot = TmuxSnapshot(
        panes=(
            TmuxPane(
                pane_id="%7",
                pane_tty="/dev/ttys006",
                pane_pid=90077,
                session_name="agent",
                window_id="@1",
                window_index=0,
                pane_index=1,
                current_path="/Users/kenn/Projects/agent-deck",
            ),
        ),
        clients=(),
    )

    pane = find_pane_for_tty(snapshot, "ttys006")

    assert pane is not None
    assert pane.pane_id == "%7"
    assert clients_for_session(snapshot, "agent") == ()


def test_tmux_snapshot_lists_attached_clients_for_session() -> None:
    """验证 attached tmux session 能列出 presentation clients。

    入参：无；测试内构造一个 pane 和一个 client。
    返回：无返回值；断言通过代表 client 可按 session 归组。
    错误处理：client 过滤错误时由 pytest 报告。
    副作用：只读取测试内存 snapshot。
    """

    snapshot = TmuxSnapshot(
        panes=(
            TmuxPane(
                pane_id="%7",
                pane_tty="/dev/ttys006",
                pane_pid=90077,
                session_name="agent",
                window_id="@1",
                window_index=0,
                pane_index=1,
                current_path="/Users/kenn/Projects/agent-deck",
            ),
        ),
        clients=(TmuxClient(client_tty="/dev/ttys010", client_pid=16260, session_name="agent", client_activity=1782111632),),
    )

    clients = clients_for_session(snapshot, "agent")

    assert len(clients) == 1
    assert clients[0].client_pid == 16260
```

- [ ] **Step 2: Run tmux tests to verify failure**

Run:

```bash
uv run pytest tests/test_host_context.py::test_tmux_snapshot_detects_detached_pane_by_tty tests/test_host_context.py::test_tmux_snapshot_lists_attached_clients_for_session -q
```

Expected: FAIL with missing `agent_deck.hosts.tmux`.

- [ ] **Step 3: Implement tmux models and readers**

Create `src/agent_deck/hosts/tmux.py` with:

- Frozen Pydantic models `TmuxPane`, `TmuxClient`, `TmuxSnapshot`.
- `TmuxReader` protocol with `read() -> TmuxSnapshot`.
- `StaticTmuxReader(snapshot: TmuxSnapshot | None)` returning an empty snapshot if no snapshot is provided.
- `SubprocessTmuxReader(timeout_seconds: float = 1.0)` using bounded `subprocess.run` for:

```bash
tmux list-panes -a -F "#{pane_id}\t#{pane_tty}\t#{pane_pid}\t#{session_name}\t#{window_id}\t#{window_index}\t#{pane_index}\t#{pane_current_path}"
tmux list-clients -F "#{client_tty}\t#{client_pid}\t#{session_name}\t#{client_activity}"
```

If tmux is missing, exits non-zero, or times out, return empty `TmuxSnapshot` and expose no exception to resolver. This keeps tmux optional.

- [ ] **Step 4: Run host tests**

Run:

```bash
uv run pytest tests/test_host_context.py -q
```

Expected: all host context tests pass.

- [ ] **Step 5: Commit task**

```bash
git add src/agent_deck/hosts/tmux.py tests/test_host_context.py
git commit -m "feat(hosts): 增加 tmux 只读快照匹配"
```

---

### Task 4: Codex Host Resolver

**Files:**
- Create: `src/agent_deck/hosts/codex.py`
- Modify: `src/agent_deck/hosts/__init__.py`
- Modify: `tests/test_host_context.py`

**Interfaces:**
- Consumes: `ProcessTable`, `TmuxReader`, `CodexAppActiveSession`
- Produces: `CodexHostResolver.resolve_cli(agent_pid: int, cwd: str | None = None) -> AgentHostContext`
- Produces: `CodexHostResolver.resolve_app_sessions(active_sessions: Iterable[CodexAppActiveSession]) -> tuple[AgentHostContext, ...]`

- [ ] **Step 1: Write failing resolver tests**

Append to `tests/test_host_context.py`:

```python
from agent_deck.hosts.codex import CodexHostResolver


def test_codex_resolver_prefers_tmux_pane_over_terminal_app() -> None:
    """验证 Codex CLI 在 tmux pane 中时 focus target 优先使用 tmux。

    入参：无；测试内构造 Codex 进程链和 tmux snapshot。
    返回：无返回值；断言通过代表 resolver 不把 Otty 当成唯一宿主事实。
    错误处理：runtime、execution host 或 activation 策略错误时由 pytest 报告。
    副作用：只读取测试 fake process table 和 fake tmux snapshot。
    """

    table = StaticProcessTable(
        {
            73879: ProcessInfo(pid=73879, ppid=90077, command="codex", args=("codex", "resume"), tty="ttys006"),
            90077: ProcessInfo(pid=90077, ppid=90072, command="-zsh", args=("-zsh",), tty="ttys006"),
            90072: ProcessInfo(pid=90072, ppid=16260, command="/usr/bin/login", args=("/usr/bin/login",), tty="ttys006"),
            16260: ProcessInfo(pid=16260, ppid=1, command="/Applications/Otty.app/Contents/MacOS/Otty", args=("/Applications/Otty.app/Contents/MacOS/Otty",), tty=None),
        }
    )
    snapshot = TmuxSnapshot(
        panes=(TmuxPane(pane_id="%7", pane_tty="/dev/ttys006", pane_pid=90077, session_name="agent", window_id="@1", window_index=0, pane_index=1, current_path="/repo"),),
        clients=(),
    )

    context = CodexHostResolver(process_table=table, tmux_snapshot=snapshot).resolve_cli(agent_pid=73879, cwd="/repo")

    assert context.runtime_kind == RuntimeKind.CODEX_CLI
    assert context.execution_host.kind == ExecutionHostKind.TMUX_PANE
    assert context.execution_host.tmux_pane_id == "%7"
    assert context.activation.strategy == ActivationStrategy.TMUX_REATTACH_NEW_CLIENT
    assert context.presentation_clients == ()


def test_codex_resolver_marks_missing_pid_unknown() -> None:
    """验证缺失 agent_pid 时 resolver 降级为 unknown。

    入参：无；测试内构造空进程表。
    返回：无返回值；断言通过代表检测失败不会伪造 focus target。
    错误处理：降级字段不符合预期时由 pytest 报告。
    副作用：只读取测试 fake process table。
    """

    context = CodexHostResolver(process_table=StaticProcessTable({}), tmux_snapshot=TmuxSnapshot()).resolve_cli(agent_pid=999999)

    assert context.runtime_kind == RuntimeKind.UNKNOWN
    assert context.execution_host.kind == ExecutionHostKind.UNKNOWN
    assert context.activation.strategy == ActivationStrategy.UNAVAILABLE
    assert context.confidence == Confidence.LOW
```

- [ ] **Step 2: Run resolver tests to verify failure**

Run:

```bash
uv run pytest tests/test_host_context.py::test_codex_resolver_prefers_tmux_pane_over_terminal_app tests/test_host_context.py::test_codex_resolver_marks_missing_pid_unknown -q
```

Expected: FAIL with missing `agent_deck.hosts.codex`.

- [ ] **Step 3: Implement resolver**

Create `src/agent_deck/hosts/codex.py` with:

- `CodexHostResolver.__init__(process_table: ProcessTable | None = None, tmux_reader: TmuxReader | None = None, tmux_snapshot: TmuxSnapshot | None = None, now: Callable[[], datetime] | None = None)`.
- `resolve_cli(agent_pid: int, cwd: str | None = None)`.
- `resolve_app_sessions(active_sessions: Iterable[CodexAppActiveSession])`.

Resolver logic:

1. Get `ProcessInfo` for `agent_pid`; if missing, return `unknown`/`unavailable` with low confidence.
2. Read `process_chain`.
3. If process `tty` matches a tmux pane, return `tmux_pane`.
4. If tmux pane has no clients, activation is `tmux_reattach_new_client`.
5. If tmux pane has clients, activation is `tmux_select_existing_client` and presentation clients include tmux client records.
6. If no tmux pane matches, use `infer_direct_pty_host(chain)` and activation `app_activate_only`.
7. For Codex App sessions, return `runtime_kind=codex_app`, `execution_host.kind=codex_app`, activation `app_activate_only`, thread/cwd/rollout fields populated.

- [ ] **Step 4: Export resolver**

Update `src/agent_deck/hosts/__init__.py` to export `CodexHostResolver`.

- [ ] **Step 5: Run resolver tests**

Run:

```bash
uv run pytest tests/test_host_context.py -q
```

Expected: all host context tests pass.

- [ ] **Step 6: Commit task**

```bash
git add src/agent_deck/hosts/__init__.py src/agent_deck/hosts/codex.py tests/test_host_context.py
git commit -m "feat(hosts): 解析 Codex CLI 与 App 宿主"
```

---

### Task 5: `agent-deckctl codex-hosts` CLI

**Files:**
- Modify: `src/agent_deck/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `docs/references/agent-deck-roadmap.md`

**Interfaces:**
- Consumes: `CodexHostResolver`
- Produces command: `agent-deckctl codex-hosts --json`
- Optional inputs: `--agent-pid <int>` for targeted CLI probe; `--include-codex-app/--no-include-codex-app` default true.

- [ ] **Step 1: Write failing CLI tests**

Add to `tests/test_cli.py`:

```python
def test_codex_hosts_prints_resolver_json(monkeypatch: Any) -> None:
    """Verify `codex-hosts --agent-pid --json` prints host context JSON.

    入参：`monkeypatch` 替换 CLI 内的 host resolver factory。
    返回：无返回值；断言通过代表 CLI 输出结构化 JSON。
    错误处理：退出码或 JSON 字段不符合预期时由 pytest 报告。
    副作用：只运行 Typer in-process，不读取真实进程、tmux 或 Codex App 状态。
    """

    class FakeResolver:
        """测试用 resolver，返回固定 Codex CLI host context。

        入参：无。
        返回：提供 `resolve_cli` 的 fake 对象。
        错误处理：不主动抛异常。
        副作用：无。
        """

        def resolve_cli(self, *, agent_pid: int, cwd: str | None = None) -> Any:
            """Return one fixed host context for the requested pid.

            入参：`agent_pid` 是 CLI 传入的 pid；`cwd` 本测试不使用。
            返回：`AgentHostContext`。
            错误处理：不主动抛异常。
            副作用：无。
            """

            from datetime import UTC, datetime
            from agent_deck.hosts.models import (
                ActivationContext,
                ActivationStrategy,
                AgentHostContext,
                Confidence,
                ExecutionHostContext,
                ExecutionHostKind,
                RuntimeKind,
            )

            return AgentHostContext(
                runtime_kind=RuntimeKind.CODEX_CLI,
                execution_host=ExecutionHostContext(kind=ExecutionHostKind.DIRECT_PTY, host_app_name="Otty"),
                activation=ActivationContext(strategy=ActivationStrategy.APP_ACTIVATE_ONLY, confidence=Confidence.MEDIUM),
                agent_pid=agent_pid,
                observed_at=datetime(2026, 6, 22, 8, 0, tzinfo=UTC),
                confidence=Confidence.MEDIUM,
            )

    monkeypatch.setattr(cli, "_build_codex_host_resolver", lambda: FakeResolver())

    result = runner.invoke(cli.ctl_app, ["codex-hosts", "--agent-pid", "15010", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["sessions"][0]["runtime_kind"] == "codex_cli"
    assert payload["sessions"][0]["agent_pid"] == 15010
```

- [ ] **Step 2: Run CLI test to verify failure**

Run:

```bash
uv run pytest tests/test_cli.py::test_codex_hosts_prints_resolver_json -q
```

Expected: FAIL because `codex-hosts` command or `_build_codex_host_resolver` is missing.

- [ ] **Step 3: Implement CLI command**

In `src/agent_deck/cli.py`:

1. Import `CodexHostResolver`.
2. Add helper `_build_codex_host_resolver() -> CodexHostResolver`.
3. Add Typer command:

```python
@ctl_app.command("codex-hosts")
def codex_hosts(
    agent_pid: Annotated[int | None, typer.Option("--agent-pid", help="Optional Codex CLI pid to inspect.")] = None,
    include_codex_app: Annotated[bool, typer.Option("--include-codex-app/--no-include-codex-app", help="Include Codex App local state sessions.")] = True,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    ...
```

Behavior:

- If `agent_pid` is provided, append `resolver.resolve_cli(agent_pid=agent_pid)`.
- If `include_codex_app` is true, call existing `scan_codex_app_state` and `select_active_codex_app_sessions`, then append `resolver.resolve_app_sessions(...)`.
- On Codex App scan errors, include `"codex_app_error": "<message>"` in JSON and continue with CLI result.
- If `--json` is false, print compact human-readable lines containing runtime, execution host, activation strategy and confidence.

- [ ] **Step 4: Update roadmap command reference**

Add `agent-deckctl codex-hosts --json` to the P1 installer / doctor backlog paragraph in `docs/references/agent-deck-roadmap.md`, noting it is read-only.

- [ ] **Step 5: Run CLI and host tests**

Run:

```bash
uv run pytest tests/test_host_context.py tests/test_cli.py::test_codex_hosts_prints_resolver_json -q
```

Expected: host tests pass and the new CLI test passes.

- [ ] **Step 6: Commit task**

```bash
git add src/agent_deck/cli.py tests/test_cli.py docs/references/agent-deck-roadmap.md
git commit -m "feat(cli): 增加 Codex 宿主只读探针"
```

---

### Task 6: Verification and Manual Smoke

**Files:**
- Modify: `docs/superpowers/specs/2026-06-22-codex-session-host-detection-design.md`
- Modify: `docs/references/agent-deck-roadmap.md`

**Interfaces:**
- Consumes: `agent-deckctl codex-hosts --json`
- Produces: documented verification evidence and known limits

- [ ] **Step 1: Run targeted automated tests**

Run:

```bash
uv run pytest tests/test_host_context.py tests/test_cli.py::test_codex_hosts_prints_resolver_json -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
uv run pytest -q
```

Expected: full suite passes. If environment lacks real hardware or system permissions, tests must still avoid real hardware and pass with fakes.

- [ ] **Step 3: Run local read-only smoke**

Run:

```bash
uv run agent-deckctl codex-hosts --json
```

Expected: command exits 0 and prints JSON with `sessions`. If sandbox prevents `ps`, rerun with approved read-only process-list permission and record that the smoke required escalation for process enumeration.

- [ ] **Step 4: Run tmux manual smoke when a tmux Codex CLI is available**

Run:

```bash
tmux list-panes -a -F "#{pane_id}\t#{pane_tty}\t#{pane_pid}\t#{session_name}\t#{window_id}\t#{window_index}\t#{pane_index}\t#{pane_current_path}"
tmux list-clients -F "#{client_tty}\t#{client_pid}\t#{session_name}\t#{client_activity}"
uv run agent-deckctl codex-hosts --json
```

Expected: Codex CLI in tmux reports `execution_host.kind == "tmux_pane"`. Detached pane reports `activation.strategy == "tmux_reattach_new_client"`; attached pane reports `activation.strategy == "tmux_select_existing_client"` when client data is available.

- [ ] **Step 5: Update docs with observed limits**

In `docs/superpowers/specs/2026-06-22-codex-session-host-detection-design.md`, append a short “已验证事实” section with actual command names and observed behavior. Do not paste sensitive prompts, raw full Codex App titles, tokens, or private environment variables.

- [ ] **Step 6: Run final checks**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: `git diff --check` has no output; status shows only intended files.

- [ ] **Step 7: Commit verification docs**

```bash
git add docs/superpowers/specs/2026-06-22-codex-session-host-detection-design.md docs/references/agent-deck-roadmap.md
git commit -m "docs(hosts): 记录 Codex 宿主检测验证"
```

---

## Self-Review

Spec coverage:

- CLI/App runtime identification is covered by Tasks 4 and 5.
- direct PTY, tmux detached and tmux attached are covered by Tasks 2, 3 and 4.
- Codex App thread downgrade to `app_activate_only` is covered by Task 4.
- Read-only CLI probe is covered by Task 5.
- Verification and known limits are covered by Task 6.

Placeholder scan:

- 本计划已避免未解析占位项和泛化执行描述；每个任务都有明确文件、接口、测试命令和验收结果。

Type consistency:

- `AgentHostContext`, `ExecutionHostContext`, `PresentationClientContext` and `ActivationContext` are introduced in Task 1 and consumed by later tasks.
- `ProcessInfo`, `StaticProcessTable`, `TmuxSnapshot`, `TmuxPane`, `TmuxClient` are introduced before resolver tests consume them.
- `CodexHostResolver.resolve_cli` and `_build_codex_host_resolver` signatures are fixed before CLI tests consume them.
