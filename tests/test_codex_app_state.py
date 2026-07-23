"""Codex App 本地状态扫描器的单元测试。

本文件只验证对 pytest 临时 SQLite 和 rollout JSONL 的只读解析契约。测试不读取真实
`~/.codex`，不启动 Codex App，不连接 Agent Deck daemon，也不写用户配置。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from agent_deck.adapters.codex_app_state import (
    CodexAppStateReport,
    CodexAppThreadSnapshot,
    build_codex_app_state_events,
    scan_codex_app_state,
    select_active_codex_app_sessions,
)
from agent_deck.core.events import EventType
from agent_deck.core.state import AgentStatus


def _create_state_db(path: Path, rollout_path: Path) -> None:
    """创建包含一个 Codex thread 的最小 state SQLite 数据库。

    入参：`path` 是数据库路径；`rollout_path` 是 thread 指向的 JSONL 文件。
    返回：无返回值。
    错误处理：SQLite 写入失败由 sqlite3 抛出并交给 pytest。
    副作用：在 pytest 临时目录创建 SQLite 文件。
    """

    conn = sqlite3.connect(path)
    conn.execute(
        """
        create table threads (
            id text primary key,
            rollout_path text not null,
            created_at integer not null,
            updated_at integer not null,
            source text not null,
            model_provider text not null,
            cwd text not null,
            title text not null,
            sandbox_policy text not null,
            approval_mode text not null,
            archived integer not null default 0,
            preview text not null default ''
        )
        """
    )
    conn.execute(
        """
        insert into threads (
            id, rollout_path, created_at, updated_at, source, model_provider,
            cwd, title, sandbox_policy, approval_mode, archived, preview
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "thread-1",
            str(rollout_path),
            1781768400,
            1781768538,
            "vscode",
            "openai",
            "/repo",
            "请求用户选择选项",
            "{}",
            "never",
            0,
            "preview",
        ),
    )
    conn.commit()
    conn.close()


def _write_rollout(path: Path) -> None:
    """写入同时包含已完成和未完成 request_user_input 的 rollout JSONL。

    入参：`path` 是 JSONL 路径。
    返回：无返回值。
    错误处理：文件写入或 JSON 序列化失败由标准异常报告。
    副作用：在 pytest 临时目录写入一个 JSONL 文件。
    """

    rows = [
        {
            "timestamp": "2026-06-18T07:39:30.147Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "completed-call",
                "arguments": json.dumps(
                    {
                        "questions": [
                            {
                                "id": "done",
                                "question": "已完成问题",
                                "options": [{"label": "A"}],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            },
        },
        {
            "timestamp": "2026-06-18T07:39:31.147Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "completed-call",
                "output": "{}",
            },
        },
        {
            "timestamp": "2026-06-18T07:42:18.871Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "pending-call",
                "arguments": json.dumps(
                    {
                        "questions": [
                            {
                                "header": "选项测试",
                                "id": "choice_test",
                                "question": "请选择一个测试选项",
                                "options": [
                                    {"label": "A 字符串 (Recommended)"},
                                    {"label": "B 字符串"},
                                ],
                            }
                        ],
                        "autoResolutionMs": 60000,
                    },
                    ensure_ascii=False,
                ),
            },
        },
    ]
    path.write_text(
        "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows),
        encoding="utf-8",
    )


def _write_tool_rollout(path: Path, *, completed: bool = False) -> None:
    """写入包含普通工具调用的 Codex rollout JSONL。

    入参：`path` 是输出 JSONL 路径；`completed` 控制是否补匹配的 `function_call_output`。
    返回：无返回值。
    错误处理：文件写入或 JSON 序列化失败由标准异常报告。
    副作用：在 pytest 临时目录写入一个 JSONL 文件。
    """

    rows: list[dict[str, object]] = [
        {
            "timestamp": "2026-06-18T09:00:00.000Z",
            "payload": {
                "type": "function_call",
                "call_id": "tool-call",
                "name": "shell",
                "arguments": "{}",
            },
        }
    ]
    if completed:
        rows.append(
            {
                "timestamp": "2026-06-18T09:00:01.000Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "tool-call",
                    "output": "{}",
                },
            }
        )
    path.write_text(
        "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows),
        encoding="utf-8",
    )


def _write_subagent_rollout(path: Path, *, parent_thread_id: str) -> None:
    """写入带 Codex 子代理 metadata 的 rollout JSONL。

    入参：`path` 是输出 JSONL 路径；`parent_thread_id` 是父 Codex thread id。
    返回：无返回值。
    错误处理：文件写入或 JSON 序列化失败由标准异常报告。
    副作用：在 pytest 临时目录写入一个 JSONL 文件。
    """

    rows = [
        {
            "timestamp": "2026-06-18T09:00:00.000Z",
            "payload": {
                "id": "child-thread",
                "session_id": parent_thread_id,
                "parent_thread_id": parent_thread_id,
                "thread_source": "subagent",
                "source": {"subagent": {"other": "worker"}},
                "cwd": "/repo",
            },
        },
        {
            "timestamp": "2026-06-18T09:00:01.000Z",
            "payload": {
                "type": "function_call",
                "call_id": "tool-call",
                "name": "shell",
                "arguments": "{}",
            },
        },
    ]
    path.write_text(
        "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows),
        encoding="utf-8",
    )


def _create_state_db_with_threads(
    path: Path,
    rows: list[tuple[str, Path, int, str, str]],
) -> None:
    """创建包含多条 Codex thread 的最小 state SQLite 数据库。

    入参：`path` 是数据库路径；`rows` 每项为 `(id, rollout_path, updated_at, cwd, title)`。
    返回：无返回值。
    错误处理：SQLite 写入失败由 sqlite3 抛出并交给 pytest。
    副作用：在 pytest 临时目录创建 SQLite 文件。
    """

    conn = sqlite3.connect(path)
    conn.execute(
        """
        create table threads (
            id text primary key,
            rollout_path text not null,
            created_at integer not null,
            updated_at integer not null,
            source text not null,
            model_provider text not null,
            cwd text not null,
            title text not null,
            sandbox_policy text not null,
            approval_mode text not null,
            archived integer not null default 0,
            preview text not null default ''
        )
        """
    )
    for thread_id, rollout_path, updated_at, cwd, title in rows:
        conn.execute(
            """
            insert into threads (
                id, rollout_path, created_at, updated_at, source, model_provider,
                cwd, title, sandbox_policy, approval_mode, archived, preview
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                str(rollout_path),
                updated_at - 10,
                updated_at,
                "vscode",
                "openai",
                cwd,
                title,
                "{}",
                "never",
                0,
                "preview",
            ),
        )
    conn.commit()
    conn.close()


def test_scan_codex_app_state_detects_pending_user_input(tmp_path: Path) -> None:
    """验证扫描器能发现未完成的 Codex App request_user_input。

    入参：`tmp_path` 提供 fake Codex home、state DB 和 rollout JSONL。
    返回：无返回值；断言通过代表已完成 call 被忽略，未完成 call 被映射为 waiting_user。
    错误处理：扫描器解析错误或字段不符合预期时由 pytest 断言报告。
    副作用：只读扫描 pytest 临时目录中的 fake 文件。
    """

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    rollout_path = tmp_path / "rollout.jsonl"
    state_db = codex_home / "state_5.sqlite"
    _write_rollout(rollout_path)
    _create_state_db(state_db, rollout_path)

    report = scan_codex_app_state(codex_home=codex_home)

    assert len(report.threads) == 1
    thread = report.threads[0]
    assert thread.thread_id == "thread-1"
    assert thread.status == "waiting_user"
    assert thread.pending_user_input is not None
    assert thread.pending_user_input.call_id == "pending-call"
    assert thread.pending_user_input.question == "请选择一个测试选项"
    assert thread.pending_user_input.option_labels == (
        "A 字符串 (Recommended)",
        "B 字符串",
    )
    assert thread.pending_user_input.auto_resolution_ms == 60000


def test_build_codex_app_state_events_maps_waiting_input(tmp_path: Path) -> None:
    """验证待用户输入的 Codex App thread 会生成 input.requested 事件。

    入参：`tmp_path` 提供 fake Codex home、state DB 和 rollout JSONL。
    返回：无返回值；断言通过代表事件可直接喂给 Agent Deck daemon。
    错误处理：事件类型、session/cwd/summary/payload 不符合预期时由 pytest 报告。
    副作用：只读扫描 pytest 临时目录中的 fake 文件。
    """

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    rollout_path = tmp_path / "rollout.jsonl"
    _write_rollout(rollout_path)
    _create_state_db(codex_home / "state_5.sqlite", rollout_path)

    events = build_codex_app_state_events(codex_home=codex_home)

    assert len(events) == 1
    event = events[0]
    assert event.normalized_type == EventType.INPUT_REQUESTED
    assert event.session_id == "thread-1"
    assert event.thread_id == "thread-1"
    assert event.cwd == "/repo"
    assert event.title == "请求用户选择选项"
    assert event.summary == "请选择一个测试选项"
    assert event.payload["call_id"] == "pending-call"


def test_select_active_codex_app_sessions_filters_and_infers_status(
    tmp_path: Path,
) -> None:
    """验证活动会话选择器过滤旧/测试 thread 并推断未完成工具调用。

    入参：`tmp_path` 提供 fake Codex home、state DB 和多个 rollout JSONL。
    返回：无返回值；断言通过代表 1 小时窗口、排除模式和 running_tool 推断生效。
    错误处理：筛选数量、顺序或状态不符合预期时由 pytest 断言报告。
    副作用：只读扫描 pytest 临时目录中的 fake 文件。
    """

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    active_rollout = tmp_path / "active.jsonl"
    test_rollout = tmp_path / "test.jsonl"
    old_rollout = tmp_path / "old.jsonl"
    _write_tool_rollout(active_rollout)
    _write_tool_rollout(test_rollout)
    _write_tool_rollout(old_rollout, completed=True)
    now_epoch = 1781773200
    _create_state_db_with_threads(
        codex_home / "state_5.sqlite",
        [
            (
                "active-thread",
                active_rollout,
                now_epoch - 30,
                "/Users/kenn/Projects/agent-deck",
                "实现 Codex 状态按钮",
            ),
            (
                "test-thread",
                test_rollout,
                now_epoch - 20,
                "/Users/kenn/Documents/Codex/2026-06-18/codex-a-b-c",
                "请求用户选择选项",
            ),
            (
                "old-thread",
                old_rollout,
                now_epoch - 7200,
                "/repo",
                "两小时前的会话",
            ),
        ],
    )

    report = scan_codex_app_state(codex_home=codex_home, limit=10)
    sessions = select_active_codex_app_sessions(
        report,
        now=datetime.fromtimestamp(now_epoch, tz=UTC),
        active_window_seconds=3600,
        max_sessions=10,
    )

    assert [session.thread_id for session in sessions] == ["active-thread"]
    assert sessions[0].status == AgentStatus.RUNNING_TOOL
    assert sessions[0].reason == "pending tool call: shell"


def test_select_active_codex_app_sessions_excludes_subagent_threads(
    tmp_path: Path,
) -> None:
    """验证 Codex App 活动会话不会把子代理 thread 放进主 agent 列表。

    入参：`tmp_path` 提供 fake Codex home、state DB、主 thread 和子代理 rollout。
    返回：无返回值；断言通过代表 `thread_source=subagent` 被保留在扫描报告但不进入 active sessions。
    错误处理：子代理仍被选中或 metadata 未解析时由 pytest 断言报告。
    副作用：只读扫描 pytest 临时目录中的 fake 文件。
    """

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    parent_rollout = tmp_path / "parent.jsonl"
    child_rollout = tmp_path / "child.jsonl"
    _write_tool_rollout(parent_rollout)
    _write_subagent_rollout(child_rollout, parent_thread_id="parent-thread")
    now_epoch = 1781773200
    _create_state_db_with_threads(
        codex_home / "state_5.sqlite",
        [
            (
                "child-thread",
                child_rollout,
                now_epoch - 10,
                "/repo",
                "The following is the Codex agent history...",
            ),
            (
                "parent-thread",
                parent_rollout,
                now_epoch - 20,
                "/repo",
                "主 agent",
            ),
        ],
    )

    report = scan_codex_app_state(codex_home=codex_home, limit=10)
    sessions = select_active_codex_app_sessions(
        report,
        now=datetime.fromtimestamp(now_epoch, tz=UTC),
        active_window_seconds=3600,
        max_sessions=10,
    )

    child = next(thread for thread in report.threads if thread.thread_id == "child-thread")
    assert child.thread_source == "subagent"
    assert child.parent_thread_id == "parent-thread"
    assert child.is_child_thread is True
    assert [session.thread_id for session in sessions] == ["parent-thread"]


def test_select_active_codex_app_sessions_excludes_cli_source(tmp_path: Path) -> None:
    """验证普通 Codex CLI thread 不会触发 ChatGPT App 任务态覆盖。

    入参：``tmp_path`` 提供一个空 rollout path。
    返回：无；断言 source=cli 被排除而 source=vscode 被保留。
    错误处理：无。
    副作用：只在 pytest 临时目录写空 JSONL。
    """

    rollout = tmp_path / "thread.jsonl"
    rollout.write_text("", encoding="utf-8")
    now_epoch = int(datetime.now(UTC).timestamp())
    report = CodexAppStateReport(
        codex_home=str(tmp_path),
        state_db_path=str(tmp_path / "state.sqlite"),
        threads=(
            CodexAppThreadSnapshot(
                thread_id="cli-thread",
                title="CLI",
                cwd="/repo",
                rollout_path=str(rollout),
                updated_at=now_epoch,
                status="observed",
                thread_source="cli",
            ),
            CodexAppThreadSnapshot(
                thread_id="app-thread",
                title="ChatGPT",
                cwd="/repo",
                rollout_path=str(rollout),
                updated_at=now_epoch - 1,
                status="observed",
                thread_source="vscode",
            ),
        ),
    )

    sessions = select_active_codex_app_sessions(
        report,
        now=datetime.fromtimestamp(now_epoch, tz=UTC),
    )

    assert [session.thread_id for session in sessions] == ["app-thread"]


def test_select_active_codex_app_sessions_accepts_new_user_thread_source(
    tmp_path: Path,
) -> None:
    """验证新版 ChatGPT 顶层 ``thread_source=user`` 仍属于本地 App 会话。

    入参：``tmp_path`` 提供最小 rollout。
    返回：无；断言 user 顶层进入选择，而 subagent/cli 仍不进入。
    错误处理：无。
    副作用：只写 pytest 临时空 JSONL。
    """

    rollout = tmp_path / "thread.jsonl"
    rollout.write_text("", encoding="utf-8")
    now_epoch = int(datetime.now(UTC).timestamp())
    report = CodexAppStateReport(
        codex_home=str(tmp_path),
        state_db_path=str(tmp_path / "state.sqlite"),
        threads=(
            CodexAppThreadSnapshot(
                thread_id="app-user-thread",
                title="ChatGPT 顶层会话",
                cwd="/repo",
                rollout_path=str(rollout),
                updated_at=now_epoch,
                status="observed",
                thread_source="user",
            ),
            CodexAppThreadSnapshot(
                thread_id="cli-thread",
                title="CLI",
                cwd="/repo",
                rollout_path=str(rollout),
                updated_at=now_epoch - 1,
                status="observed",
                thread_source="cli",
            ),
        ),
    )

    sessions = select_active_codex_app_sessions(
        report,
        now=datetime.fromtimestamp(now_epoch, tz=UTC),
    )

    assert [session.thread_id for session in sessions] == ["app-user-thread"]
