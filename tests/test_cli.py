"""Tests for Agent Deck command-line entry points.

These tests define Task 9's CLI and Codex hook helper contracts only. They do
not start real uvicorn servers, perform real HTTP I/O, probe hardware, install
Codex hooks, read user configuration, or persist state; all external behavior
is replaced with pytest monkeypatch fakes and Typer's in-process CliRunner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from agent_deck import __version__
from agent_deck import cli
from agent_deck.rendering.asset_builder import CodexVisualAssetBuildResult


runner = CliRunner()


class _FakeResponse:
    """Represent the small httpx response surface used by CLI tests.

    入参：`payload` 是 `.json()` 返回的 JSON-like 对象；`status_code` 是 HTTP 状态码。
    返回：测试 fake response 实例，可被 CLI 按 httpx.Response 的局部接口读取。
    错误处理：`raise_for_status` 在 status_code >= 400 时抛出 `cli.httpx.HTTPStatusError`。
    副作用：仅保存内存字段，不访问网络、文件、硬件或全局状态。
    """

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        """Create a fake response with deterministic JSON data.

        入参：`payload` 是响应 JSON；`status_code` 默认 200，可设置为错误状态。
        返回：无显式返回值；实例提供 `.json()` 与 `.raise_for_status()`。
        错误处理：构造过程不主动抛业务异常。
        副作用：只复制引用到测试内存，不触发任何外部 I/O。
        """

        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        """Return the configured response payload.

        入参：无。
        返回：构造时传入的 dict payload。
        错误处理：本 fake 不模拟 JSON 解码失败。
        副作用：无；只读取内存字段。
        """

        return self._payload

    def raise_for_status(self) -> None:
        """Raise an httpx-style status error for failing status codes.

        入参：无；读取当前 `status_code`。
        返回：状态小于 400 时无返回值。
        错误处理：状态码大于等于 400 时抛出 `cli.httpx.HTTPStatusError`。
        副作用：无；只根据内存状态决定是否抛错。
        """

        if self.status_code >= 400:
            raise cli.httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,
                response=None,
            )


class _FakeClient:
    """Capture HTTP requests issued by the CLI without opening sockets.

    入参：`timeout` 是 CLI 传给 `httpx.Client` 的超时配置。
    返回：context-manager fake client，并把请求记录到类级列表。
    错误处理：可通过类级 `fail` 标志模拟 `httpx.ConnectError`。
    副作用：写入测试进程内的 `requests` 记录，不访问真实网络。
    """

    requests: list[dict[str, Any]] = []
    fail = False

    def __init__(self, timeout: float) -> None:
        """Record the timeout used to create the fake client.

        入参：`timeout` 是本地 HTTP 调用超时秒数。
        返回：无显式返回值。
        错误处理：本 fake 不校验 timeout 类型。
        副作用：保存 timeout 到实例内存，不访问外部 I/O。
        """

        self.timeout = timeout

    def __enter__(self) -> "_FakeClient":
        """Enter the fake context manager.

        入参：无。
        返回：当前 fake client 实例。
        错误处理：本 fake 不在进入阶段抛错。
        副作用：无；不打开网络连接。
        """

        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Exit the fake context manager.

        入参：`*_exc_info` 是 context manager 传入的异常三元组。
        返回：无返回值，让异常按默认规则传播。
        错误处理：本 fake 不吞掉异常。
        副作用：无；不关闭真实连接。
        """

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        """Capture a GET request and return deterministic JSON.

        入参：`url` 是请求 URL；`kwargs` 包含 query 参数等 httpx 选项。
        返回：针对 `/status` 或 `/wait` 的 fake JSON response。
        错误处理：`fail` 为 True 时抛出 `httpx.ConnectError`。
        副作用：把请求写入类级 `requests`，不发起真实网络请求。
        """

        if self.fail:
            raise cli.httpx.ConnectError("daemon unavailable")
        self.requests.append({"method": "GET", "url": url, "kwargs": kwargs})
        if url.endswith("/status"):
            return _FakeResponse({"agents": [], "render_count": 1})
        if url.endswith("/wait"):
            return _FakeResponse({"behavior": "allow", "message": "approved"})
        return _FakeResponse({})

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        """Capture a POST request and return deterministic JSON.

        入参：`url` 是请求 URL；`kwargs` 包含 JSON body 等 httpx 选项。
        返回：针对 `/events`、`/request` 或 `/resolve` 的 fake JSON response。
        错误处理：`fail` 为 True 时抛出 `httpx.ConnectError`。
        副作用：把请求写入类级 `requests`，不发起真实网络请求。
        """

        if self.fail:
            raise cli.httpx.ConnectError("daemon unavailable")
        self.requests.append({"method": "POST", "url": url, "kwargs": kwargs})
        if url.endswith("/decisions/request"):
            return _FakeResponse({"decision_id": "decision-1"})
        if url.endswith("/resolve"):
            return _FakeResponse({"result": kwargs.get("json", {})})
        return _FakeResponse({"ok": True})


def test_version_preserves_existing_output() -> None:
    """Verify `agent-deckctl version` still prints the package version.

    入参：无；测试内通过 CliRunner 调用 control app。
    返回：无返回值；断言通过代表现有 version 行为保持兼容。
    错误处理：退出码或输出不匹配时由 pytest 报告。
    副作用：仅运行 Typer in-process 命令并读取标准输出。
    """

    result = runner.invoke(cli.ctl_app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_status_prints_formatted_json(monkeypatch: Any) -> None:
    """Verify `status` GETs daemon status and formats JSON output.

    入参：`monkeypatch` 替换 CLI 内的 httpx.Client。
    返回：无返回值；断言通过代表 URL、超时和 pretty JSON 契约成立。
    错误处理：命令失败、请求缺失或 JSON 不匹配时由 pytest 报告。
    副作用：只写入 fake request 记录，不访问真实 daemon。
    """

    _install_fake_client(monkeypatch)

    result = runner.invoke(cli.ctl_app, ["status"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"agents": [], "render_count": 1}
    assert "\n  " in result.output
    assert _FakeClient.requests[0]["url"] == f"{cli.DEFAULT_DAEMON_URL}/status"


def test_simulate_posts_normalized_event_body(monkeypatch: Any) -> None:
    """Verify `simulate` builds a Codex normalized event for POST /events.

    入参：`monkeypatch` 替换 CLI 内的 httpx.Client。
    返回：无返回值；断言通过代表基本字段、source 和 normalized type 正确。
    错误处理：命令失败或 POST body 不符合事件契约时由 pytest 报告。
    副作用：只记录 fake HTTP POST，不访问真实网络。
    """

    _install_fake_client(monkeypatch)

    result = runner.invoke(
        cli.ctl_app,
        ["simulate", "--session-id", "demo", "--event-type", "session.started"],
    )

    assert result.exit_code == 0
    assert "sent session.started for demo" in result.output
    request = _FakeClient.requests[0]
    body = request["kwargs"]["json"]
    assert request["url"] == f"{cli.DEFAULT_DAEMON_URL}/events"
    assert body["source"] == "codex"
    assert body["session_id"] == "demo"
    assert body["source_event_type"] == "session.started"
    assert body["normalized_type"] == "session.started"
    assert body["payload"]["simulated"] is True


def test_resolve_rejects_invalid_behavior_without_http(monkeypatch: Any) -> None:
    """Verify `resolve` exits 2 for behavior values outside allow/deny.

    入参：`monkeypatch` 安装 fake HTTP client to detect unexpected requests。
    返回：无返回值；断言通过代表非法 behavior 由 CLI 参数校验拦截。
    错误处理：退出码不是 2 或发起 HTTP 请求时由 pytest 报告。
    副作用：不访问真实网络；fake request 记录应保持为空。
    """

    _install_fake_client(monkeypatch)

    result = runner.invoke(cli.ctl_app, ["resolve", "decision-1", "maybe"])

    assert result.exit_code == 2
    assert _FakeClient.requests == []


def test_notify_daemon_failure_exits_zero(monkeypatch: Any) -> None:
    """Verify Codex notify fails open when the daemon is unavailable.

    入参：`monkeypatch` 安装会抛 ConnectError 的 fake HTTP client。
    返回：无返回值；断言通过代表 stderr 记录原因且 exit 0。
    错误处理：退出码、stderr 或请求行为不符合 fail-open 契约时由 pytest 报告。
    副作用：读取测试 stdin JSON；不访问真实 daemon。
    """

    _install_fake_client(monkeypatch, fail=True)

    result = runner.invoke(
        cli.codex_hook_app,
        ["notify"],
        input=json.dumps({"session_id": "demo", "cwd": "/tmp/demo"}),
    )

    assert result.exit_code == 0
    assert "daemon unavailable" in result.stderr


def test_permission_request_fail_closed_json(monkeypatch: Any) -> None:
    """Verify permission-request emits deny JSON when daemon I/O fails.

    入参：`monkeypatch` 安装会抛 ConnectError 的 fake HTTP client。
    返回：无返回值；断言通过代表 stdout 是 Codex hook deny payload 且 exit 0。
    错误处理：stdout JSON、stderr 或退出码不符合 fail-closed 契约时由 pytest 报告。
    副作用：读取测试 stdin JSON；不访问真实 daemon。
    """

    _install_fake_client(monkeypatch, fail=True)

    result = runner.invoke(
        cli.codex_hook_app,
        ["permission-request"],
        input=json.dumps({"session_id": "demo", "tool_name": "shell"}),
    )

    assert result.exit_code == 0
    body = json.loads(_json_object_text(result.output))
    decision = body["hookSpecificOutput"]["decision"]
    assert body["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert decision["behavior"] == "deny"
    assert "Agent Deck daemon unavailable" in decision["message"]
    assert "daemon unavailable" in result.stderr


def test_daemon_callback_calls_uvicorn_run(monkeypatch: Any) -> None:
    """Verify bare `agent-deckd` starts uvicorn with create_app and defaults.

    入参：`monkeypatch` 替换 `uvicorn.run` 与 `create_app`。
    返回：无返回值；断言通过代表 callback 使用默认 host/port 并传入 app。
    错误处理：命令失败或 uvicorn 参数不匹配时由 pytest 报告。
    副作用：只写入测试内存记录，不打开真实 socket。
    """

    calls: list[dict[str, Any]] = []
    fake_app = object()
    monkeypatch.setattr(cli, "create_app", lambda: fake_app)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda app, host, port: calls.append(
            {"app": app, "host": host, "port": port}
        ),
    )

    result = runner.invoke(cli.daemon_app, [])

    assert result.exit_code == 0
    assert calls == [{"app": fake_app, "host": "127.0.0.1", "port": 8765}]


def test_daemon_rejects_out_of_range_port_before_uvicorn(monkeypatch: Any) -> None:
    """Verify invalid daemon ports fail at CLI parsing instead of uvicorn bind.

    入参：`monkeypatch` 替换 `uvicorn.run`，用于确认解析失败时不会启动 server。
    返回：无返回值；断言通过代表端口范围错误以 exit 2 收敛。
    错误处理：若命令进入 uvicorn 或退出码不是 2，会由 pytest 报告。
    副作用：只运行 Typer in-process 解析，不打开真实 socket。
    """

    calls: list[object] = []
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = runner.invoke(cli.daemon_app, ["--port", "88765"])

    assert result.exit_code == 2
    assert calls == []


def test_generate_codex_assets_uses_default_sources(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify `generate-codex-assets` calls the local asset builder.

    入参：`monkeypatch` 替换 CLI 内的 builder；`tmp_path` 提供输出目录。
    返回：无返回值；断言通过代表默认源素材、尺寸和动画采样参数传递正确。
    错误处理：命令缺失、退出码非 0、参数传递错误或 JSON 输出错误由 pytest 报告。
    副作用：只写入测试内存记录，不读取真实 assets、不生成真实图片。
    """

    output_dir = tmp_path / "codex-preview"
    calls: list[dict[str, Any]] = []

    def fake_build_codex_visual_assets(**kwargs: Any) -> CodexVisualAssetBuildResult:
        """Capture CLI builder arguments and return deterministic metadata.

        入参：`kwargs` 是 CLI 转发给 builder 的命名参数。
        返回：固定 `CodexVisualAssetBuildResult`，用于 CLI JSON 输出断言。
        错误处理：本 fake 不主动抛异常。
        副作用：把调用参数追加到 `calls`。
        """

        calls.append(kwargs)
        return CodexVisualAssetBuildResult(
            output_dir=kwargs["output_dir"],
            preview_path=kwargs["output_dir"] / "preview.png",
            manifest_path=kwargs["output_dir"] / "manifest.json",
            preview_gif_paths={"idle": kwargs["output_dir"] / "idle" / "preview.gif"},
            frame_size=kwargs["key_size"],
            variant_frame_counts={"idle": 1, "offline": 1},
        )

    monkeypatch.setattr(cli, "build_codex_visual_assets", fake_build_codex_visual_assets)

    result = runner.invoke(
        cli.ctl_app,
        ["generate-codex-assets", "--output-dir", str(output_dir)],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0]["source_gif"] == Path("assets/codex/codex.gif")
    assert calls[0]["source_png"] == Path("assets/codex/codex.png")
    assert calls[0]["output_dir"] == output_dir
    assert calls[0]["key_size"] == (112, 112)
    assert calls[0]["target_fps"] == 10
    assert calls[0]["max_duration_ms"] == 5000
    assert calls[0]["max_frames"] is None
    payload = json.loads(result.output)
    assert payload["preview_path"] == str(output_dir / "preview.png")
    assert payload["manifest_path"] == str(output_dir / "manifest.json")
    assert payload["preview_gif_paths"] == {
        "idle": str(output_dir / "idle" / "preview.gif")
    }
    assert payload["variant_frame_counts"] == {"idle": 1, "offline": 1}


def test_codex_quota_command_prints_snapshot(monkeypatch: Any) -> None:
    """Verify `codex-quota` prints the adapter snapshot as JSON.

    入参：`monkeypatch` 替换 CLI 内 quota reader。
    返回：无返回值；断言通过表示 CLI 调用 adapter 并输出 plan 与窗口字段。
    错误处理：命令退出码、JSON 结构或 adapter 调用错误由 pytest 报告。
    副作用：只运行 Typer in-process，不启动真实 Codex app-server。
    """

    calls: list[dict[str, Any]] = []

    class FakeSnapshot:
        """测试用 quota snapshot。

        入参：无。
        返回：fake 对象，提供 CLI 需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 JSON payload。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 quota JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {
                "plan_type": "prolite",
                "plan_short_label": "ProLite",
                "plan_display_name": "ProLite",
                "primary": {"used_percent": 28},
                "secondary": {"used_percent": 8},
            }

    def fake_read_codex_quota(**kwargs: Any) -> FakeSnapshot:
        """捕获 CLI 传给 quota reader 的参数。

        入参：`kwargs` 是 CLI 转发的选项。
        返回：`FakeSnapshot`。
        错误处理：无。
        副作用：把调用参数追加到 `calls`。
        """

        calls.append(kwargs)
        return FakeSnapshot()

    monkeypatch.setattr(cli, "read_codex_quota", fake_read_codex_quota)

    result = runner.invoke(cli.ctl_app, ["codex-quota"])

    assert result.exit_code == 0
    assert calls == [{"timeout_seconds": 10.0}]
    payload = json.loads(result.output)
    assert payload["plan_short_label"] == "ProLite"
    assert payload["plan_display_name"] == "ProLite"
    assert payload["primary"]["used_percent"] == 28


def _install_fake_client(monkeypatch: Any, fail: bool = False) -> None:
    """Install `_FakeClient` as the CLI's httpx.Client replacement.

    入参：`monkeypatch` 是 pytest fixture；`fail` 控制 fake client 是否抛连接错误。
    返回：无返回值。
    错误处理：monkeypatch 失败会由 pytest 抛出异常。
    副作用：清空并重设 fake request 记录，修改当前测试进程内的 CLI 模块属性。
    """

    _FakeClient.requests = []
    _FakeClient.fail = fail
    monkeypatch.setattr(cli.httpx, "Client", _FakeClient)


def _json_object_text(output: str) -> str:
    """Extract the JSON object portion from Typer's mixed test output.

    入参：`output` 是 CliRunner 捕获的 stdout 文本；当前 Typer 测试 runner 可能把 stderr
    混入同一个字符串。
    返回：从首个 `{` 起始的 JSON object 文本。
    错误处理：找不到 `{` 时由断言失败报告。
    副作用：无；只处理内存字符串。
    """

    start = output.find("{")
    assert start >= 0
    return output[start:]
