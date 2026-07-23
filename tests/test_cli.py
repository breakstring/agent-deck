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

from PIL import Image
from typer.testing import CliRunner

from agent_deck import __version__
from agent_deck import cli
from agent_deck.core.state import AgentStatus
from agent_deck.hardware.streamdock_probe import ProbeResult
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


def test_doctor_json_reports_streamdock_probe_and_occupants(monkeypatch: Any) -> None:
    """验证 doctor JSON 汇总安全探针和疑似占用进程。

    入参：`monkeypatch` 注入 fake probe、fake 进程扫描和 SDK 环境变量。
    返回：无返回值；断言通过代表命令输出稳定 JSON report。
    错误处理：退出码、JSON 结构或字段内容不符合预期时由 pytest 报告。
    副作用：只运行 Typer in-process；不访问真实硬件、不读取真实进程表。
    """

    monkeypatch.setenv("AGENT_DECK_STREAMDOCK_SDK_PATH", "/tmp/StreamDock-Device-SDK/Python-SDK")
    monkeypatch.setattr(
        cli,
        "probe_streamdock_devices",
        lambda: [
            ProbeResult(
                device_type="N4Pro",
                path="DevSrvsID:4295063691",
                can_open=True,
                can_init=False,
                firmware_version="1.0.0",
                serial_number="serial-1",
            )
        ],
    )
    monkeypatch.setattr(
        cli,
        "_scan_hardware_occupants",
        lambda: [
            {
                "pid": 123,
                "ppid": 1,
                "command": "/Users/kenn/Library/boegam/donglemonitor eshow dongle",
                "matched_pattern": "donglemonitor",
            }
        ],
    )

    result = runner.invoke(cli.ctl_app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["streamdock_sdk_path"] == "/tmp/StreamDock-Device-SDK/Python-SDK"
    assert payload["streamdock_probe_error"] is None
    assert payload["streamdock_devices"][0]["device_type"] == "N4Pro"
    assert payload["streamdock_devices"][0]["can_open"] is True
    assert isinstance(payload["keyboard_shortcut_capability"], dict)
    assert "permission_granted" in payload["keyboard_shortcut_capability"]
    assert payload["hardware_occupants"][0]["matched_pattern"] == "donglemonitor"
    assert any("疑似硬件占用进程" in warning for warning in payload["warnings"])


def test_doctor_keeps_running_when_streamdock_probe_fails(monkeypatch: Any) -> None:
    """验证 SDK 探针失败时 doctor 仍输出诊断报告。

    入参：`monkeypatch` 注入会抛错的 fake probe 和空进程扫描。
    返回：无返回值；断言通过代表现场 SDK/import 错误不会中断 doctor。
    错误处理：命令失败或 warning 缺失时由 pytest 报告。
    副作用：只运行 Typer in-process；不访问真实硬件、不读取真实进程表。
    """

    monkeypatch.delenv("AGENT_DECK_STREAMDOCK_SDK_PATH", raising=False)

    def fail_probe() -> list[ProbeResult]:
        """模拟官方 SDK 不可导入或枚举失败。

        入参：无。
        返回：不会返回；总是抛出 RuntimeError。
        错误处理：调用方应把异常收敛到 doctor report。
        副作用：无；不访问真实硬件。
        """

        raise RuntimeError("SDK unavailable")

    monkeypatch.setattr(cli, "probe_streamdock_devices", fail_probe)
    monkeypatch.setattr(cli, "_scan_hardware_occupants", lambda: [])

    result = runner.invoke(cli.ctl_app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["streamdock_sdk_path"] is None
    assert payload["streamdock_probe_error"] == "SDK unavailable"
    assert payload["streamdock_devices"] == []
    assert any("AGENT_DECK_STREAMDOCK_SDK_PATH" in warning for warning in payload["warnings"])
    assert any("StreamDock 只读探针失败" in warning for warning in payload["warnings"])


def test_n4pro_splash_json_writes_default_visible_layer(monkeypatch: Any) -> None:
    """验证 N4 Pro splash 命令会把默认图写到可见触屏层。

    入参：`monkeypatch` 注入 fake dual-device sink。
    返回：无返回值；断言通过代表 CLI 使用 800x480 默认图和 `set_touchscreen_image` 语义。
    错误处理：命令失败、图片尺寸或 JSON 结构错误时由 pytest 报告。
    副作用：只运行 Typer in-process，不访问真实硬件。
    """

    calls: list[Image.Image] = []

    def fake_splash_sink(image: Image.Image) -> cli.StreamDockTouchscreenRenderResult:
        """记录写屏图片并返回成功。

        入参：`image` 是 CLI 渲染出的默认触屏图。
        返回：固定成功结果。
        错误处理：无。
        副作用：追加测试内存列表。
        """

        calls.append(image)
        return cli.StreamDockTouchscreenRenderResult(
            ok=True,
            device_type="StreamDockN4Pro",
            path="DevSrvsID:4295109180",
            background_api="set_touchscreen_image",
            sdk_result="None",
        )

    monkeypatch.setattr(
        cli,
        "render_dual_device_touchscreen_image_to_n4pro",
        fake_splash_sink,
    )

    result = runner.invoke(cli.ctl_app, ["hardware", "n4pro", "splash", "--json"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0].size == (800, 480)
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["target"] == "streamdock:n4pro:visible-touchscreen"
    assert payload["image_size"] == [800, 480]
    assert payload["visible_layer_api"] == "set_touchscreen_image"
    assert payload["result"]["background_api"] == "set_touchscreen_image"


def test_n4pro_splash_exits_1_when_sink_reports_failure(monkeypatch: Any) -> None:
    """验证 splash sink 失败时命令输出结果并返回 exit 1。

    入参：`monkeypatch` 注入失败 fake sink。
    返回：无返回值；断言通过代表手动修屏失败不会被误报为成功。
    错误处理：退出码或 payload 错误时由 pytest 报告。
    副作用：只运行 Typer in-process，不访问真实硬件。
    """

    def fake_splash_sink(_: Image.Image) -> cli.StreamDockTouchscreenRenderResult:
        """返回固定失败结果。

        入参：忽略图片。
        返回：`ok=False` 的写屏结果。
        错误处理：无。
        副作用：无。
        """

        return cli.StreamDockTouchscreenRenderResult(
            ok=False,
            background_api="set_touchscreen_image",
            error="no N4 Pro device found",
        )

    monkeypatch.setattr(
        cli,
        "render_dual_device_touchscreen_image_to_n4pro",
        fake_splash_sink,
    )

    result = runner.invoke(cli.ctl_app, ["hardware", "n4pro", "splash", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["result"]["error"] == "no N4 Pro device found"


def test_hardware_status_json_reports_all_devices_and_n4pro_actions(
    monkeypatch: Any,
) -> None:
    """验证通用 hardware status 会报告所有设备和 N4 Pro 专属动作。

    入参：`monkeypatch` 注入 fake probe 和进程扫描。
    返回：无返回值；断言通过代表通用诊断不绑定单一硬件型号。
    错误处理：JSON 字段或过滤逻辑不符合预期时由 pytest 报告。
    副作用：只运行 Typer in-process；不访问真实硬件或真实进程表。
    """

    monkeypatch.setenv("AGENT_DECK_STREAMDOCK_SDK_PATH", "/tmp/StreamDock-Device-SDK/Python-SDK")
    monkeypatch.setattr(
        cli,
        "probe_streamdock_devices",
        lambda: [
            ProbeResult(
                device_type="StreamDockBiz",
                path="biz",
                can_open=True,
                can_init=False,
            ),
            ProbeResult(
                device_type="StreamDockN4Pro",
                path="n4pro",
                can_open=True,
                can_init=False,
            ),
        ],
    )
    monkeypatch.setattr(cli, "_scan_hardware_occupants", lambda: [])

    result = runner.invoke(cli.ctl_app, ["hardware", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["hardware_families"] == [
        {
            "family": "streamdock",
            "status": "available",
            "device_count": 2,
        }
    ]
    assert len(payload["hardware_devices"]) == 2
    assert payload["hardware_devices"][0]["profile"] == "streamdock.unknown"
    assert payload["hardware_devices"][0]["supported_commands"] == []
    assert payload["hardware_devices"][1]["profile"] == "mirabox.n4pro"
    assert payload["hardware_devices"][1]["can_rewrite_splash"] is True
    assert payload["hardware_devices"][1]["supported_commands"] == [
        "agent-deckctl hardware n4pro splash"
    ]
    assert payload["commands"] == {
        "streamdock:n4pro:splash": "agent-deckctl hardware n4pro splash"
    }
    assert payload["warnings"] == []


def test_n4pro_status_json_is_narrow_view_over_hardware_status(
    monkeypatch: Any,
) -> None:
    """验证 N4 Pro status 是通用 hardware status 的窄视图。

    入参：`monkeypatch` 注入 fake probe 和进程扫描。
    返回：无返回值；断言通过代表兼容入口只返回 N4 Pro 相关设备。
    错误处理：JSON 字段或过滤逻辑不符合预期时由 pytest 报告。
    副作用：只运行 Typer in-process；不访问真实硬件或真实进程表。
    """

    monkeypatch.setattr(
        cli,
        "probe_streamdock_devices",
        lambda: [
            ProbeResult(
                device_type="StreamDockBiz",
                path="biz",
                can_open=True,
                can_init=False,
            ),
            ProbeResult(
                device_type="StreamDockN4Pro",
                path="n4pro",
                can_open=True,
                can_init=False,
            ),
        ],
    )
    monkeypatch.setattr(cli, "_scan_hardware_occupants", lambda: [])

    result = runner.invoke(cli.ctl_app, ["hardware", "n4pro", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["splash_command"] == "agent-deckctl hardware n4pro splash"
    assert payload["visible_layer_api"] == "set_touchscreen_image"
    assert payload["can_rewrite_splash"] is True
    assert len(payload["n4pro_devices"]) == 1
    assert payload["n4pro_devices"][0]["profile"] == "mirabox.n4pro"
    assert payload["n4pro_devices"][0]["device_type"] == "StreamDockN4Pro"
    assert payload["warnings"] == []


def test_scan_hardware_occupants_parses_ps_output(monkeypatch: Any) -> None:
    """验证疑似硬件占用进程扫描能解析 ps 输出。

    入参：`monkeypatch` 替换 `subprocess.run` 和当前进程 pid。
    返回：无返回值；断言通过代表 scanner 不依赖真实系统进程表。
    错误处理：解析结果或过滤逻辑不符合预期时由 pytest 报告。
    副作用：无真实 subprocess 调用，不发送信号、不修改进程。
    """

    class Completed:
        """提供 `_scan_hardware_occupants` 需要的 subprocess 返回值。

        入参：无。
        返回：fake completed process 实例。
        错误处理：构造过程不抛异常。
        副作用：只保存 stdout 字符串。
        """

        stdout = "\n".join(
            [
                "  10   1 /Applications/StreamDock.app/Contents/MacOS/StreamDock",
                "  11   1 /usr/bin/python -m agent_deckd",
                "  12   1 /bin/zsh",
                "  99   1 agent-deckctl doctor",
            ]
        )

    def fake_run(*_args: Any, **_kwargs: Any) -> Completed:
        """返回固定 ps 输出。

        入参：忽略 subprocess.run 的参数。
        返回：包含固定 stdout 的 fake completed process。
        错误处理：不模拟失败。
        副作用：无；不启动子进程。
        """

        return Completed()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.os, "getpid", lambda: 99)

    occupants = cli._scan_hardware_occupants()

    assert occupants == [
        {
            "pid": 10,
            "ppid": 1,
            "command": "/Applications/StreamDock.app/Contents/MacOS/StreamDock",
            "matched_pattern": "streamdock",
        },
        {
            "pid": 11,
            "ppid": 1,
            "command": "/usr/bin/python -m agent_deckd",
            "matched_pattern": "agent-deckd",
        },
    ]


def test_scan_hardware_occupants_ignores_agent_deckctl_wrapper(
    monkeypatch: Any,
) -> None:
    """验证当前 agent-deckctl 诊断包装命令不会被误判为硬件占用。

    入参：`monkeypatch` 替换 `subprocess.run` 和当前进程 pid。
    返回：无返回值；断言通过代表 `uv run agent-deckctl ... streamdock ...` 不产生假阳性。
    错误处理：scanner 误报时由 pytest 报告。
    副作用：无真实 subprocess 调用。
    """

    class Completed:
        """提供包含 agent-deckctl wrapper 的 ps 输出。

        入参：无。
        返回：fake completed process 实例。
        错误处理：构造过程不抛异常。
        副作用：只保存 stdout 字符串。
        """

        stdout = "\n".join(
            [
                "  20   1 uv run agent-deckctl hardware n4pro status --json",
                "  21   1 /Applications/StreamDock.app/Contents/MacOS/StreamDock",
            ]
        )

    def fake_run(*_args: Any, **_kwargs: Any) -> Completed:
        """返回固定 ps 输出。

        入参：忽略 subprocess.run 参数。
        返回：包含固定 stdout 的 fake completed process。
        错误处理：不模拟失败。
        副作用：无。
        """

        return Completed()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.os, "getpid", lambda: 999)

    occupants = cli._scan_hardware_occupants()

    assert occupants == [
        {
            "pid": 21,
            "ppid": 1,
            "command": "/Applications/StreamDock.app/Contents/MacOS/StreamDock",
            "matched_pattern": "streamdock",
        }
    ]


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

        def resolve_cli(
            self, *, agent_pid: int, cwd: str | None = None
        ) -> Any:
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
                execution_host=ExecutionHostContext(
                    kind=ExecutionHostKind.DIRECT_PTY,
                    host_app_name="Otty",
                ),
                activation=ActivationContext(
                    strategy=ActivationStrategy.APP_ACTIVATE_ONLY,
                    confidence=Confidence.MEDIUM,
                ),
                agent_pid=agent_pid,
                observed_at=datetime(2026, 6, 22, 8, 0, tzinfo=UTC),
                confidence=Confidence.MEDIUM,
            )

    monkeypatch.setattr(cli, "_build_codex_host_resolver", lambda: FakeResolver())

    result = runner.invoke(
        cli.ctl_app,
        ["codex-hosts", "--agent-pid", "15010", "--no-include-codex-app", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["sessions"][0]["runtime_kind"] == "codex_cli"
    assert payload["sessions"][0]["agent_pid"] == 15010


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


def test_notify_accepts_codex_json_argument(monkeypatch: Any) -> None:
    """Verify Codex notify JSON argv is forwarded as a normalized event.

    入参：`monkeypatch` 安装 fake HTTP client。
    返回：无返回值；断言通过代表 notify 兼容 Codex 官方单 JSON 参数调用形态。
    错误处理：命令退出码、POST body 或 URL 不符合契约时由 pytest 报告。
    副作用：只运行 Typer in-process，不访问真实 daemon 或 Codex。
    """

    _install_fake_client(monkeypatch)
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "thread-1",
        "turn-id": "turn-1",
        "cwd": "/tmp/project",
    }

    result = runner.invoke(cli.codex_hook_app, ["notify", json.dumps(payload)])

    assert result.exit_code == 0
    request = _FakeClient.requests[0]
    body = request["kwargs"]["json"]
    assert request["url"] == f"{cli.DEFAULT_DAEMON_URL}/events"
    assert body["normalized_type"] == "turn.completed"
    assert body["session_id"] == "thread-1"
    assert body["turn_id"] == "turn-1"
    assert body["cwd"] == "/tmp/project"


def test_permission_request_default_passthrough_lets_codex_decide(
    monkeypatch: Any,
) -> None:
    """Verify permission-request defaults to no decision and no daemon wait.

    入参：`monkeypatch` 安装 fake HTTP client 以检测意外 daemon 访问。
    返回：无返回值；断言通过代表默认 passthrough 不输出 hook decision。
    错误处理：若输出 JSON、访问 daemon 或退出码不为 0，由 pytest 报告。
    副作用：读取测试 stdin JSON；不访问真实 daemon。
    """

    _install_fake_client(monkeypatch)

    result = runner.invoke(
        cli.codex_hook_app,
        ["permission-request"],
        input=json.dumps({"session_id": "demo", "tool_name": "shell"}),
    )

    assert result.exit_code == 0
    assert result.output == ""
    assert result.stderr == ""
    assert _FakeClient.requests == []


def test_permission_request_handle_mode_fail_closed_json(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify handle mode emits deny JSON when daemon I/O fails.

    入参：`monkeypatch` 安装会抛 ConnectError 的 fake HTTP client。
    返回：无返回值；断言通过代表 stdout 是 Codex hook deny payload 且 exit 0。
    错误处理：stdout JSON、stderr 或退出码不符合 fail-closed 契约时由 pytest 报告。
    副作用：读取测试 stdin JSON；不访问真实 daemon。
    """

    _install_fake_client(monkeypatch, fail=True)
    config_path = tmp_path / "agent-deck.toml"
    config_path.write_text(
        "[codex.permission_request]\nmode = \"handle\"\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.codex_hook_app,
        ["permission-request", "--config", str(config_path)],
        input=json.dumps({"session_id": "demo", "tool_name": "shell"}),
    )

    assert result.exit_code == 0
    body = json.loads(_json_object_text(result.output))
    decision = body["hookSpecificOutput"]["decision"]
    assert body["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert decision["behavior"] == "deny"
    assert "Agent Deck daemon unavailable" in decision["message"]
    assert "daemon unavailable" in result.stderr


def test_permission_request_deny_mode_returns_configured_deny(
    tmp_path: Path,
) -> None:
    """Verify deny mode blocks PermissionRequest without contacting daemon.

    入参：`tmp_path` 提供临时 Agent Deck 配置文件。
    返回：无返回值；断言通过代表 deny mode 输出 Codex deny decision。
    错误处理：若行为不是 deny、消息不匹配或退出码不为 0，由 pytest 报告。
    副作用：只写 pytest 临时配置并读取测试 stdin，不访问真实 daemon。
    """

    config_path = tmp_path / "agent-deck.toml"
    config_path.write_text(
        "\n".join(
            [
                "[codex.permission_request]",
                "mode = \"deny\"",
                "deny_message = \"Agent Deck permission handling disabled.\"",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.codex_hook_app,
        ["permission-request", "--config", str(config_path)],
        input=json.dumps({"session_id": "demo", "tool_name": "shell"}),
    )

    assert result.exit_code == 0
    body = json.loads(_json_object_text(result.output))
    decision = body["hookSpecificOutput"]["decision"]
    assert decision == {
        "behavior": "deny",
        "message": "Agent Deck permission handling disabled.",
    }


def test_daemon_callback_calls_uvicorn_run(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify bare `agent-deckd` starts uvicorn with Codex poller defaults.

    入参：`monkeypatch` 替换工作目录、`uvicorn.run` 与 `create_app`；`tmp_path` 提供无项目配置的目录。
    返回：无返回值；断言通过代表 callback 使用默认 host/port，并启用状态与 quota poller。
    错误处理：命令失败、uvicorn 参数或 poller 配置不匹配时由 pytest 报告。
    副作用：临时切换测试进程工作目录并写入内存记录，不打开真实 socket。
    """

    uvicorn_calls: list[dict[str, Any]] = []
    create_app_calls: list[dict[str, Any]] = []
    fake_app = object()
    monkeypatch.chdir(tmp_path)

    def fake_create_app(**kwargs: Any) -> object:
        """捕获 daemon callback 传给 app factory 的配置。

        入参：`kwargs` 是 `create_app` 关键字参数。
        返回：固定 fake ASGI app。
        错误处理：无。
        副作用：把调用参数追加到测试内存列表。
        """

        create_app_calls.append(kwargs)
        return fake_app

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda app, host, port: uvicorn_calls.append(
            {"app": app, "host": host, "port": port}
        ),
    )

    result = runner.invoke(cli.daemon_app, [])

    assert result.exit_code == 0
    assert uvicorn_calls == [{"app": fake_app, "host": "127.0.0.1", "port": 8765}]
    poller_config = create_app_calls[0]["poller_config"]
    assert poller_config.codex_app_state_enabled is True
    assert poller_config.codex_app_state_interval_seconds == 5.0
    assert poller_config.codex_app_state_scan_limit == 80
    assert poller_config.codex_app_active_window_seconds == 3600
    assert poller_config.codex_app_active_session_limit == 10
    assert poller_config.codex_remote_ssh_enabled is True
    assert "codex_remote_ssh_hosts" not in poller_config.model_dump()
    assert poller_config.codex_quota_enabled is True
    assert poller_config.codex_quota_interval_seconds == 300.0
    assert poller_config.codex_quota_timeout_seconds == 10.0
    assert poller_config.codex_token_usage_enabled is True
    assert poller_config.codex_token_usage_interval_seconds == 300.0
    assert poller_config.streamdock_quota_touchscreen_enabled is False
    assert poller_config.streamdock_quota_device == "n4pro"
    assert poller_config.streamdock_n4pro_renderer_enabled is True
    assert poller_config.streamdock_n4pro_render_interval_seconds == 3.0
    assert poller_config.streamdock_n4pro_renderer_fps == 10


def test_daemon_callback_can_disable_codex_pollers(monkeypatch: Any) -> None:
    """Verify daemon CLI can disable Codex polling when needed.

    入参：`monkeypatch` 替换 `uvicorn.run` 与 `create_app`。
    返回：无返回值；断言通过代表两个 disable 选项会传入关闭状态。
    错误处理：命令失败或 poller 配置仍启用时由 pytest 报告。
    副作用：只运行 Typer in-process，不启动真实 daemon。
    """

    create_app_calls: list[dict[str, Any]] = []
    fake_app = object()
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda **kwargs: create_app_calls.append(kwargs) or fake_app,
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli.daemon_app,
        [
            "--disable-codex-app-state-poller",
            "--disable-codex-quota-poller",
            "--disable-codex-token-usage-poller",
            "--disable-streamdock-quota-touchscreen",
            "--disable-hardware-renderer",
        ],
    )

    assert result.exit_code == 0
    poller_config = create_app_calls[0]["poller_config"]
    assert poller_config.codex_app_state_enabled is False
    assert poller_config.codex_quota_enabled is False
    assert poller_config.codex_token_usage_enabled is False
    assert poller_config.streamdock_quota_touchscreen_enabled is False
    assert poller_config.streamdock_n4pro_renderer_enabled is False


def test_daemon_callback_maps_remote_ssh_config(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """验证 daemon 把 SSH Remote 总开关和轮询参数映射到 poller config。

    入参：``monkeypatch`` 隔离 uvicorn/app factory；``tmp_path`` 保存配置。
    返回：无；断言不含 host 名单，轮询、timeout、limit、stale 和完成反馈均透传。
    错误处理：无。
    副作用：只写 pytest 临时 TOML，不启动网络或硬件。
    """

    config_path = tmp_path / "agent-deck.toml"
    config_path.write_text(
        "\n".join(
            (
                "[hardware_renderer]",
                "enabled = false",
                "[codex.remote_ssh]",
                "enabled = true",
                "poll_interval_seconds = 7",
                "timeout_seconds = 9",
                "thread_limit = 42",
                "stale_after_seconds = 23",
                "completed_feedback_seconds = 11",
            )
        ),
        encoding="utf-8",
    )
    create_app_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda **kwargs: create_app_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli.daemon_app,
        ["--config", str(config_path)],
    )

    assert result.exit_code == 0
    remote = create_app_calls[0]["poller_config"]
    assert remote.codex_remote_ssh_enabled is True
    assert "codex_remote_ssh_hosts" not in remote.model_dump()
    assert remote.codex_remote_ssh_interval_seconds == 7
    assert remote.codex_remote_ssh_timeout_seconds == 9
    assert remote.codex_remote_ssh_thread_limit == 42
    assert remote.codex_remote_ssh_stale_after_seconds == 23
    assert remote.codex_remote_ssh_completed_feedback_seconds == 11


def test_daemon_callback_configures_default_unified_n4pro_renderer(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify daemon CLI configures default unified N4 Pro renderer.

    入参：`monkeypatch` 替换 `uvicorn.run` 与 `create_app`；`tmp_path` 提供 fake frame root。
    返回：无返回值；断言通过代表 unified renderer 默认启用、参数进入 poller config，
    并和旧硬件 sink 互斥。
    错误处理：命令失败、配置未启用或互斥失效时由 pytest 报告。
    副作用：只运行 Typer in-process，不启动真实 daemon 或访问硬件。
    """

    create_app_calls: list[dict[str, Any]] = []
    frame_root = tmp_path / "frames"
    fake_app = object()
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda **kwargs: create_app_calls.append(kwargs) or fake_app,
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli.daemon_app,
        [
            "--render-interval-seconds",
            "1.5",
            "--renderer-fps",
            "7",
            "--config",
            str(tmp_path / "missing-agent-deck.toml"),
        ],
    )

    assert result.exit_code == 0
    poller_config = create_app_calls[0]["poller_config"]
    assert poller_config.streamdock_quota_touchscreen_enabled is False
    assert poller_config.streamdock_n4pro_renderer_enabled is True
    assert poller_config.streamdock_n4pro_render_interval_seconds == 1.5
    assert poller_config.streamdock_n4pro_renderer_fps == 7
    assert poller_config.streamdock_n4pro_frame_root == Path(
        "assets/codex/generated/n4pro-key-112-fps10"
    )
    assert poller_config.focus_actions_enabled is True


def test_daemon_callback_reads_hardware_renderer_config(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify daemon CLI reads generic hardware renderer defaults from TOML.

    入参：`monkeypatch` 替换 `uvicorn.run` 与 `create_app`；`tmp_path` 提供 fake config。
    返回：无返回值；断言通过代表通用配置文件映射到当前 N4 Pro renderer 实现参数。
    错误处理：命令失败或配置未进入 poller config 时由 pytest 报告。
    副作用：只写 pytest 临时 TOML，不启动真实 daemon 或访问硬件。
    """

    create_app_calls: list[dict[str, Any]] = []
    config_path = tmp_path / "agent-deck.toml"
    frame_root = tmp_path / "frames"
    config_path.write_text(
        "\n".join(
            [
                "[hardware_renderer]",
                "enabled = true",
                'device_profile = "n4pro"',
                "render_interval_seconds = 3.5",
                "fps = 8",
                f'frame_root = "{frame_root}"',
                "",
                "[actions.focus]",
                "enabled = false",
                "",
                "[codex.pet]",
                "enabled = true",
                "refresh_interval_seconds = 2.5",
                "panel_fps = 6",
                'motion = "reduced"',
            ]
        ),
        encoding="utf-8",
    )
    fake_app = object()
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda **kwargs: create_app_calls.append(kwargs) or fake_app,
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    result = runner.invoke(cli.daemon_app, ["--config", str(config_path)])

    assert result.exit_code == 0
    poller_config = create_app_calls[0]["poller_config"]
    assert poller_config.streamdock_quota_device == "n4pro"
    assert poller_config.streamdock_n4pro_renderer_enabled is True
    assert poller_config.streamdock_n4pro_render_interval_seconds == 3.5
    assert poller_config.streamdock_n4pro_renderer_fps == 8
    assert poller_config.streamdock_n4pro_frame_root == frame_root
    assert poller_config.focus_actions_enabled is False
    assert poller_config.codex_pet_enabled is True
    assert poller_config.codex_pet_refresh_interval_seconds == 2.5
    assert poller_config.codex_pet_panel_fps == 6
    assert poller_config.codex_pet_motion.value == "reduced"


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


def test_codex_app_state_command_prints_scan_report(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify `codex-app-state` prints the local Codex App scan report.

    入参：`monkeypatch` 替换 CLI 内的 Codex App scanner；`tmp_path` 提供 fake Codex home。
    返回：无返回值；断言通过代表 CLI 转发路径和 limit，并输出 report JSON。
    错误处理：命令退出码、参数转发或 JSON 输出不符合契约时由 pytest 报告。
    副作用：只运行 Typer in-process，不读取真实 `~/.codex`。
    """

    calls: list[dict[str, Any]] = []
    codex_home = tmp_path / ".codex"

    class FakeReport:
        """测试用 Codex App scan report。

        入参：无。
        返回：fake 对象，提供 CLI 需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 scan JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 Codex App scan report JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {
                "codex_home": str(codex_home),
                "state_db_path": str(codex_home / "state_5.sqlite"),
                "threads": [{"thread_id": "thread-1", "status": "waiting_user"}],
            }

    def fake_scan_codex_app_state(**kwargs: Any) -> FakeReport:
        """捕获 CLI 传给 Codex App scanner 的参数。

        入参：`kwargs` 是 CLI 转发的选项。
        返回：`FakeReport`。
        错误处理：无。
        副作用：把调用参数追加到 `calls`。
        """

        calls.append(kwargs)
        return FakeReport()

    monkeypatch.setattr(cli, "scan_codex_app_state", fake_scan_codex_app_state)

    result = runner.invoke(
        cli.ctl_app,
        ["codex-app-state", "--codex-home", str(codex_home), "--limit", "3"],
    )

    assert result.exit_code == 0
    assert calls == [{"codex_home": codex_home, "state_db_path": None, "limit": 3}]
    payload = json.loads(result.output)
    assert payload["threads"][0]["status"] == "waiting_user"


def test_codex_app_state_sync_posts_pending_events(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify `codex-app-state --sync` posts generated events to the daemon.

    入参：`monkeypatch` 替换 scanner、event builder 和 HTTP client；`tmp_path` 提供 fake DB。
    返回：无返回值；断言通过代表 sync 只发送 adapter 生成的 normalized event。
    错误处理：命令退出码、POST URL/body 或 JSON 输出不符合契约时由 pytest 报告。
    副作用：只写入 fake HTTP request 记录，不访问真实 daemon 或 Codex。
    """

    _install_fake_client(monkeypatch)
    state_db_path = tmp_path / "state_5.sqlite"
    build_calls: list[object] = []

    class FakeReport:
        """测试用 Codex App scan report。

        入参：无。
        返回：fake 对象，提供 CLI 需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 scan JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 Codex App scan report JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {"threads": [{"thread_id": "thread-1", "status": "waiting_user"}]}

    class FakeEvent:
        """测试用 normalized event。

        入参：无。
        返回：fake 对象，提供 CLI 需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 event JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 `input.requested` event JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {
                "source": "codex",
                "session_id": "thread-1",
                "normalized_type": "input.requested",
            }

    report = FakeReport()
    monkeypatch.setattr(cli, "scan_codex_app_state", lambda **_: report)

    def fake_build_codex_app_state_events_from_report(report_arg: object) -> tuple[FakeEvent]:
        """捕获 CLI 传给 event builder 的 report。

        入参：`report_arg` 是 scanner 返回对象。
        返回：包含一个 fake event 的 tuple。
        错误处理：无。
        副作用：把 report 参数追加到 `build_calls`。
        """

        build_calls.append(report_arg)
        return (FakeEvent(),)

    monkeypatch.setattr(
        cli,
        "build_codex_app_state_events_from_report",
        fake_build_codex_app_state_events_from_report,
    )

    result = runner.invoke(
        cli.ctl_app,
        [
            "codex-app-state",
            "--state-db-path",
            str(state_db_path),
            "--sync",
            "--daemon-url",
            "http://127.0.0.1:9999",
        ],
    )

    assert result.exit_code == 0
    assert build_calls == [report]
    request = _FakeClient.requests[0]
    assert request["url"] == "http://127.0.0.1:9999/events"
    assert request["kwargs"]["json"]["normalized_type"] == "input.requested"
    payload = json.loads(result.output)
    assert payload["synced_events"] == 1


def test_codex_sessions_preview_renders_sessions_and_quota(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify `codex-sessions-preview` wires active sessions, quota and N4 Pro sink.

    入参：`monkeypatch` 替换 scanner、quota reader、quota renderer 和硬件动画 sink；
    `tmp_path` 提供 fake Codex home 和 generated frame root。
    返回：无返回值；断言通过代表 CLI 使用活动会话状态帧，并同时传递 quota 背景。
    错误处理：退出码、参数转发、帧路径或 JSON 输出不符合契约时由 pytest 报告。
    副作用：只写 pytest 临时 PNG 帧，不访问真实 Codex、daemon 或 N4 Pro。
    """

    codex_home = tmp_path / ".codex"
    frame_root = tmp_path / "frames"
    working_dir = frame_root / "working"
    working_dir.mkdir(parents=True)
    frame_path = working_dir / "frame_000.png"
    Image.new("RGB", (112, 112), (8, 9, 10)).save(frame_path)
    calls: dict[str, Any] = {}

    class FakeReport:
        """测试用 Codex App scan report。

        入参：无。
        返回：fake 对象，仅用于透传给 active session selector。
        错误处理：无。
        副作用：无。
        """

    class FakeSession:
        """测试用活动 Codex 会话。

        入参：无。
        返回：提供 CLI 需要的 `status` 和 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        status = AgentStatus.RUNNING_TOOL

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定活动会话 JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 session JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {"thread_id": "thread-1", "status": "running_tool"}

    class FakeQuota:
        """测试用 quota snapshot。

        入参：无。
        返回：提供 CLI 输出需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 quota JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 quota JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {"plan_short_label": "ProLite"}

    class FakeRenderResult:
        """测试用 N4 Pro 动画结果。

        入参：无。
        返回：提供 CLI 输出需要的 `ok` 和 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        ok = True

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定动画结果 JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 render JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {"ok": True, "frames_rendered": 2}

    report = FakeReport()
    session = FakeSession()
    quota = FakeQuota()

    def fake_scan_codex_app_state(**kwargs: Any) -> FakeReport:
        """捕获 CLI 传给 Codex App scanner 的参数。

        入参：`kwargs` 是 CLI 转发的 scanner 选项。
        返回：固定 fake report。
        错误处理：无。
        副作用：记录 scanner 调用参数。
        """

        calls["scan"] = kwargs
        return report

    def fake_select_active_codex_app_sessions(
        report_arg: object,
        **kwargs: Any,
    ) -> tuple[FakeSession]:
        """捕获 CLI 传给活动会话选择器的参数。

        入参：`report_arg` 是 scanner 返回对象；`kwargs` 是过滤选项。
        返回：一个 running_tool 会话。
        错误处理：无。
        副作用：记录 selector 调用参数。
        """

        calls["select"] = {"report": report_arg, **kwargs}
        return (session,)

    def fake_read_codex_quota(**kwargs: Any) -> FakeQuota:
        """捕获 CLI 传给 quota reader 的参数。

        入参：`kwargs` 是 quota 超时配置。
        返回：固定 fake quota。
        错误处理：无。
        副作用：记录 quota 调用参数。
        """

        calls["quota"] = kwargs
        return quota

    def fake_render_quota_touchscreen(snapshot: object) -> Image.Image:
        """捕获 CLI 传给 quota touchscreen renderer 的 snapshot。

        入参：`snapshot` 是 quota reader 返回对象。
        返回：固定 800x480 背景图。
        错误处理：无。
        副作用：记录 renderer 输入。
        """

        calls["quota_snapshot"] = snapshot
        return Image.new("RGB", (800, 480), (1, 2, 3))

    def fake_animate_key_images_on_n4pro(**kwargs: Any) -> FakeRenderResult:
        """捕获 CLI 传给真实硬件动画 sink 的参数。

        入参：`kwargs` 包含背景图、按键帧路径、时长和 fps。
        返回：固定成功结果。
        错误处理：无。
        副作用：记录硬件 sink 参数。
        """

        calls["animate"] = kwargs
        return FakeRenderResult()

    monkeypatch.setattr(cli, "scan_codex_app_state", fake_scan_codex_app_state)
    monkeypatch.setattr(
        cli,
        "select_active_codex_app_sessions",
        fake_select_active_codex_app_sessions,
    )
    monkeypatch.setattr(cli, "read_codex_quota", fake_read_codex_quota)
    monkeypatch.setattr(cli, "render_quota_touchscreen", fake_render_quota_touchscreen)
    monkeypatch.setattr(
        cli,
        "animate_key_images_on_n4pro",
        fake_animate_key_images_on_n4pro,
    )

    result = runner.invoke(
        cli.ctl_app,
        [
            "codex-sessions-preview",
            "--codex-home",
            str(codex_home),
            "--frame-root",
            str(frame_root),
            "--duration-seconds",
            "0.2",
            "--fps",
            "10",
        ],
    )

    assert result.exit_code == 0
    assert calls["scan"] == {
        "codex_home": codex_home,
        "state_db_path": None,
        "limit": 80,
    }
    assert calls["select"]["report"] is report
    assert calls["select"]["active_window_seconds"] == 3600
    assert calls["select"]["max_sessions"] == 10
    assert calls["quota"] == {"timeout_seconds": 10.0}
    assert calls["quota_snapshot"] is quota
    assert calls["animate"]["key_frame_paths"] == {1: (frame_path.resolve(),)}
    assert calls["animate"]["duration_seconds"] == 0.2
    assert calls["animate"]["fps"] == 10
    payload = json.loads(result.output)
    assert payload["sessions"] == [{"thread_id": "thread-1", "status": "running_tool"}]
    assert payload["key_count"] == 1
    assert payload["render"]["ok"] is True


def test_codex_detect_enable_integration_prints_report(monkeypatch: Any) -> None:
    """Verify `codex-detect --enable-integration` prints the adapter report.

    入参：`monkeypatch` 替换 CLI 内的 Codex detection report builder。
    返回：无返回值；断言通过代表 CLI 转发 integration 选项并输出 JSON。
    错误处理：命令退出码、参数转发或 JSON 输出不符合契约时由 pytest 报告。
    副作用：只运行 Typer in-process，不读取真实 Codex 配置、不写文件、不启动子进程。
    """

    calls: list[dict[str, Any]] = []

    class FakeReport:
        """测试用 Codex detection report。

        入参：无。
        返回：fake 对象，提供 CLI 需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 JSON report。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 detection report JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {
                "product": "codex",
                "integration": {
                    "writes_files": False,
                    "notify_toml": 'notify = ["agent-deck-codex-hook", "notify"]',
                },
            }

    def fake_build_codex_detection_report(**kwargs: Any) -> FakeReport:
        """捕获 CLI 传给 detection builder 的参数。

        入参：`kwargs` 是 CLI 转发的选项。
        返回：`FakeReport`。
        错误处理：无。
        副作用：把调用参数追加到 `calls`。
        """

        calls.append(kwargs)
        return FakeReport()

    monkeypatch.setattr(
        cli,
        "build_codex_detection_report",
        fake_build_codex_detection_report,
    )

    result = runner.invoke(
        cli.ctl_app,
        [
            "codex-detect",
            "--enable-integration",
            "--daemon-url",
            "http://127.0.0.1:9999",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "enable_integration": True,
            "daemon_url": "http://127.0.0.1:9999",
            "codex_home": None,
            "app_path": None,
        }
    ]
    payload = json.loads(result.output)
    assert payload["product"] == "codex"
    assert payload["integration"]["writes_files"] is False


def test_codex_install_defaults_to_dry_run(monkeypatch: Any) -> None:
    """Verify `codex-install` defaults to a non-writing dry-run.

    入参：`monkeypatch` 替换 CLI 内的 Codex installer。
    返回：无返回值；断言通过代表 CLI 默认传入 apply=False 并输出 JSON。
    错误处理：命令退出码、参数转发或 JSON 输出不符合契约时由 pytest 报告。
    副作用：只运行 Typer in-process，不读取或写入真实 Codex 配置。
    """

    calls: list[dict[str, Any]] = []

    class FakeInstallResult:
        """测试用 Codex install result。

        入参：无。
        返回：fake 对象，提供 CLI 需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 dry-run JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 install result JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {"applied": False, "writes_files": False, "written_paths": []}

    def fake_install_codex_integration(**kwargs: Any) -> FakeInstallResult:
        """捕获 CLI 传给 installer 的参数。

        入参：`kwargs` 是 CLI 转发的选项。
        返回：`FakeInstallResult`。
        错误处理：无。
        副作用：把调用参数追加到 `calls`。
        """

        calls.append(kwargs)
        return FakeInstallResult()

    monkeypatch.setattr(
        cli,
        "install_codex_integration",
        fake_install_codex_integration,
    )

    result = runner.invoke(cli.ctl_app, ["codex-install"])

    assert result.exit_code == 0
    assert calls == [
        {
            "apply": False,
            "daemon_url": cli.DEFAULT_DAEMON_URL,
            "codex_home": None,
            "app_path": None,
            "mode": "user",
            "system_requirements_path": None,
            "managed_hooks_dir": None,
        }
    ]
    assert json.loads(result.output)["writes_files"] is False


def test_codex_install_apply_forwards_apply_flag(monkeypatch: Any) -> None:
    """Verify `codex-install --apply` enables writing in the installer.

    入参：`monkeypatch` 替换 CLI 内的 Codex installer。
    返回：无返回值；断言通过代表 CLI 只在显式 `--apply` 时传入 apply=True。
    错误处理：命令退出码、参数转发或 JSON 输出不符合契约时由 pytest 报告。
    副作用：只运行 Typer in-process，不读取或写入真实 Codex 配置。
    """

    calls: list[dict[str, Any]] = []

    class FakeInstallResult:
        """测试用 Codex apply result。

        入参：无。
        返回：fake 对象，提供 CLI 需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 apply JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 install result JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {"applied": True, "writes_files": True, "written_paths": ["x"]}

    def fake_install_codex_integration(**kwargs: Any) -> FakeInstallResult:
        """捕获 CLI 传给 installer 的参数。

        入参：`kwargs` 是 CLI 转发的选项。
        返回：`FakeInstallResult`。
        错误处理：无。
        副作用：把调用参数追加到 `calls`。
        """

        calls.append(kwargs)
        return FakeInstallResult()

    monkeypatch.setattr(
        cli,
        "install_codex_integration",
        fake_install_codex_integration,
    )

    result = runner.invoke(
        cli.ctl_app,
        ["codex-install", "--apply", "--daemon-url", "http://127.0.0.1:9999"],
    )

    assert result.exit_code == 0
    assert calls[0]["apply"] is True
    assert calls[0]["daemon_url"] == "http://127.0.0.1:9999"
    assert json.loads(result.output)["applied"] is True


def test_codex_install_managed_system_forwards_system_paths(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify `codex-install --managed-system` forwards system install options.

    入参：`monkeypatch` 替换 CLI 内的 Codex installer；`tmp_path` 提供 fake 系统路径。
    返回：无返回值；断言通过代表 CLI 能把 managed-system 选项转发到 installer。
    错误处理：命令退出码、参数转发或 JSON 输出不符合契约时由 pytest 报告。
    副作用：只运行 Typer in-process，不读取或写入真实系统配置。
    """

    calls: list[dict[str, Any]] = []

    class FakeInstallResult:
        """测试用 managed-system install result。

        入参：无。
        返回：fake 对象，提供 CLI 需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 managed-system JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 install result JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {"mode": "managed-system", "applied": True}

    def fake_install_codex_integration(**kwargs: Any) -> FakeInstallResult:
        """捕获 CLI 传给 installer 的参数。

        入参：`kwargs` 是 CLI 转发的选项。
        返回：`FakeInstallResult`。
        错误处理：无。
        副作用：把调用参数追加到 `calls`。
        """

        calls.append(kwargs)
        return FakeInstallResult()

    monkeypatch.setattr(
        cli,
        "install_codex_integration",
        fake_install_codex_integration,
    )
    requirements_path = tmp_path / "requirements.toml"
    hooks_dir = tmp_path / "hooks"

    result = runner.invoke(
        cli.ctl_app,
        [
            "codex-install",
            "--managed-system",
            "--apply",
            "--system-requirements-path",
            str(requirements_path),
            "--managed-hooks-dir",
            str(hooks_dir),
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["apply"] is True
    assert calls[0]["mode"] == "managed-system"
    assert calls[0]["system_requirements_path"] == requirements_path
    assert calls[0]["managed_hooks_dir"] == hooks_dir
    assert json.loads(result.output)["mode"] == "managed-system"


def test_codex_install_managed_system_validate_only_uses_validator(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Verify `codex-install --managed-system --validate-only` runs read-only validation.

    入参：`monkeypatch` 替换 CLI 内的 validator；`tmp_path` 提供 fake 系统路径。
    返回：无返回值；断言通过代表 CLI 不调用 installer，且完整转发 validate 参数。
    错误处理：命令退出码、参数转发或 JSON 输出不符合契约时由 pytest 报告。
    副作用：只运行 Typer in-process，不读取或写入真实 Codex/系统配置。
    """

    calls: list[dict[str, Any]] = []

    class FakeValidationResult:
        """测试用 managed-system validation result。

        入参：无。
        返回：fake 对象，提供 CLI 需要的 `model_dump`。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 validation JSON。

            入参：`mode` 是 Pydantic 兼容参数，测试要求为 `json`。
            返回：固定 validation result JSON object。
            错误处理：mode 非 json 时断言失败。
            副作用：无。
            """

            assert mode == "json"
            return {"mode": "managed-system", "ok": True, "checks": []}

    def fake_validate_codex_managed_system_integration(
        **kwargs: Any,
    ) -> FakeValidationResult:
        """捕获 CLI 传给 managed-system validator 的参数。

        入参：`kwargs` 是 CLI 转发的选项。
        返回：`FakeValidationResult`。
        错误处理：无。
        副作用：把调用参数追加到 `calls`。
        """

        calls.append(kwargs)
        return FakeValidationResult()

    def fail_install_codex_integration(**_: Any) -> None:
        """确保 validate-only 不会落入 installer 写入路径。

        入参：忽略所有参数。
        返回：无；若被调用则直接断言失败。
        错误处理：被调用时抛 AssertionError。
        副作用：无。
        """

        raise AssertionError("validate-only must not call installer")

    monkeypatch.setattr(
        cli,
        "validate_codex_managed_system_integration",
        fake_validate_codex_managed_system_integration,
    )
    monkeypatch.setattr(cli, "install_codex_integration", fail_install_codex_integration)
    requirements_path = tmp_path / "requirements.toml"
    hooks_dir = tmp_path / "hooks"

    result = runner.invoke(
        cli.ctl_app,
        [
            "codex-install",
            "--managed-system",
            "--validate-only",
            "--daemon-url",
            "http://127.0.0.1:9999",
            "--system-requirements-path",
            str(requirements_path),
            "--managed-hooks-dir",
            str(hooks_dir),
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "daemon_url": "http://127.0.0.1:9999",
            "codex_home": None,
            "app_path": None,
            "system_requirements_path": requirements_path,
            "managed_hooks_dir": hooks_dir,
        }
    ]
    assert json.loads(result.output)["ok"] is True


def test_codex_event_hook_maps_session_start_to_event(monkeypatch: Any) -> None:
    """Verify generic Codex lifecycle hooks become normalized daemon events.

    入参：`monkeypatch` 安装 fake HTTP client。
    返回：无返回值；断言通过代表 `SessionStart` 被映射为 `session.started`。
    错误处理：退出码、POST URL 或 event body 不符合契约时由 pytest 报告。
    副作用：读取测试 stdin JSON；不访问真实 daemon 或 Codex。
    """

    _install_fake_client(monkeypatch)

    result = runner.invoke(
        cli.codex_hook_app,
        ["event"],
        input=json.dumps(
            {
                "hookEventName": "SessionStart",
                "session_id": "session-1",
                "cwd": "/tmp/project",
            }
        ),
    )

    assert result.exit_code == 0
    request = _FakeClient.requests[0]
    body = request["kwargs"]["json"]
    assert request["url"] == f"{cli.DEFAULT_DAEMON_URL}/events"
    assert body["source"] == "codex"
    assert body["session_id"] == "session-1"
    assert body["source_event_type"] == "SessionStart"
    assert body["normalized_type"] == "session.started"
    assert body["cwd"] == "/tmp/project"


def test_codex_event_hook_records_agent_pid_in_payload(monkeypatch: Any) -> None:
    """Verify lifecycle hooks can attach the Codex parent process id.

    入参：`monkeypatch` 安装 fake HTTP client。
    返回：无返回值；断言通过代表 `--agent-pid` 被记录到 normalized event payload。
    错误处理：退出码、POST body 或 payload 字段不符合契约时由 pytest 报告。
    副作用：读取测试 stdin JSON；不访问真实 daemon 或 Codex。
    """

    _install_fake_client(monkeypatch)

    result = runner.invoke(
        cli.codex_hook_app,
        ["event", "--agent-pid", "4242"],
        input=json.dumps(
            {
                "hookEventName": "SessionStart",
                "session_id": "session-1",
            }
        ),
    )

    assert result.exit_code == 0
    body = _FakeClient.requests[0]["kwargs"]["json"]
    assert body["payload"]["agent_pid"] == "4242"


def test_codex_event_hook_maps_user_prompt_submit_to_turn_started(
    monkeypatch: Any,
) -> None:
    """Verify Codex user prompts become session-scoped turn start events.

    入参：`monkeypatch` 安装 fake HTTP client。
    返回：无返回值；断言通过代表 `UserPromptSubmit` 被映射到同一 session 的 `turn.started`。
    错误处理：退出码、POST body、session 或 turn 映射不符合契约时由 pytest 报告。
    副作用：读取测试 stdin JSON；不访问真实 daemon 或 Codex。
    """

    _install_fake_client(monkeypatch)

    result = runner.invoke(
        cli.codex_hook_app,
        ["event"],
        input=json.dumps(
            {
                "hookEventName": "UserPromptSubmit",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "cwd": "/tmp/project",
            }
        ),
    )

    assert result.exit_code == 0
    request = _FakeClient.requests[0]
    body = request["kwargs"]["json"]
    assert request["url"] == f"{cli.DEFAULT_DAEMON_URL}/events"
    assert body["source"] == "codex"
    assert body["session_id"] == "session-1"
    assert body["turn_id"] == "turn-1"
    assert body["source_event_type"] == "UserPromptSubmit"
    assert body["normalized_type"] == "turn.started"
    assert body["cwd"] == "/tmp/project"


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


def test_codex_remote_state_command_prints_sanitized_snapshot(monkeypatch: Any) -> None:
    """远端诊断命令应传递 host/limit/timeout，并只输出 observer 的安全快照。

    入参：pytest ``monkeypatch`` 替换真实 SSH observer。
    返回：无；断言命令成功、关闭连接且 JSON 不含 prompt。
    错误处理：无。
    副作用：仅修改测试进程内 CLI 符号，不访问 SSH。
    """

    captured: dict[str, Any] = {}

    class FakeSnapshot:
        """提供 CLI 所需 model_dump 的最小安全快照。

        入参：无。
        返回：固定诊断 dict。
        错误处理：无。
        副作用：无。
        """

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            """返回固定 JSON-safe 诊断。

            入参：``mode`` 应为 json。
            返回：不含 preview/prompt 的 dict。
            错误处理：无。
            副作用：记录 mode 供断言。
            """

            captured["mode"] = mode
            return {"host": "minibox", "sessions": [], "status_counts": {"idle": 1}}

    class FakeObserver:
        """记录 CLI 构造参数并模拟 context-managed observer。

        入参：host 和关键字配置。
        返回：实例可 read/close。
        错误处理：无。
        副作用：只写 captured dict。
        """

        def __init__(self, host: str, **kwargs: Any) -> None:
            """记录 host 和配置。

            入参：CLI 传入的 observer 参数。
            返回：无。
            错误处理：无。
            副作用：更新 captured。
            """

            captured["host"] = host
            captured["kwargs"] = kwargs

        def __enter__(self) -> FakeObserver:
            """返回自身。

            入参：无。
            返回：当前 fake。
            错误处理：无。
            副作用：无。
            """

            return self

        def __exit__(self, *_args: object) -> None:
            """记录 context 已关闭。

            入参：标准 context manager 参数。
            返回：无。
            错误处理：无。
            副作用：设置 closed。
            """

            captured["closed"] = True

        def read_snapshot(self) -> FakeSnapshot:
            """返回固定安全快照。

            入参：无。
            返回：``FakeSnapshot``。
            错误处理：无。
            副作用：无。
            """

            return FakeSnapshot()

    monkeypatch.setattr(cli, "CodexRemoteSshObserver", FakeObserver)
    result = runner.invoke(
        cli.ctl_app,
        [
            "codex-remote-state",
            "--host",
            "minibox",
            "--timeout-seconds",
            "7",
            "--limit",
            "32",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(_json_object_text(result.stdout))["host"] == "minibox"
    assert captured["host"] == "minibox"
    assert captured["kwargs"]["timeout_seconds"] == 7.0
    assert captured["kwargs"]["thread_limit"] == 32
    assert captured["kwargs"]["completed_feedback_seconds"] == 0
    assert captured["closed"] is True
