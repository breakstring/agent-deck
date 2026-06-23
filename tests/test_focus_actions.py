"""focus action executor 测试。

这些测试只注入 fake subprocess runner，验证 focus target 到系统命令参数的转换；不会执行
真实 AppleScript、不会激活窗口、不会访问 tmux 或终端。
"""

from __future__ import annotations

import subprocess
from typing import Any

from agent_deck.actions.focus import focus_agent_target


def test_focus_agent_target_activates_app_with_osascript_runner() -> None:
    """`app:<name>` focus target 应通过 osascript 激活对应 App。

    入参：无；测试内注入 fake runner 捕获命令。
    返回：无返回值；断言通过代表 executor 不经 shell 且参数稳定。
    错误处理：命令参数或结果映射错误时由 pytest 报告。
    副作用：只记录 fake runner 调用。
    """

    calls: list[list[str]] = []

    def fake_runner(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        """记录命令并返回成功。

        入参：`args` 是 executor 传入的命令参数。
        返回：成功的 `CompletedProcess`。
        错误处理：无。
        副作用：写入测试内存列表。
        """

        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    result = focus_agent_target("app:Codex", runner=fake_runner)

    assert result.ok is True
    assert result.status == "succeeded"
    assert result.focus_target == "app:Codex"
    assert result.message == "activated app Codex"
    assert len(calls) == 2
    assert calls[0] == ["osascript", "-e", 'tell application "Codex" to activate']
    assert "miniaturized" in calls[1][2]
    assert "System Events" in calls[1][2]


def test_focus_agent_target_rejects_unsupported_target() -> None:
    """非 app target 暂不执行真实 focus。

    入参：无；传入 tmux target。
    返回：无返回值；断言通过代表第一版不会误执行 tmux attach/select。
    错误处理：unsupported target 被错误执行时由 pytest 报告。
    副作用：无。
    """

    result = focus_agent_target("tmux:%7")

    assert result.ok is False
    assert result.status == "unsupported"
    assert result.message == "unsupported focus target: tmux:%7"


def test_focus_agent_target_activates_app_and_reports_thread_level_limit() -> None:
    """`codex-app:<thread_id>` target 先激活 App，再返回 thread 级别未支持的诊断。

    入参：无；通过 fake runner 捕获 osascript 调用。
    返回：无返回值；断言通过代表线程级定位状态可观测但未误导。
    错误处理：命令参数或状态错误时由 pytest 报告。
    副作用：无。
    """

    calls: list[list[str]] = []

    def fake_runner(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    result = focus_agent_target("codex-app:thread-1", runner=fake_runner)

    assert result.ok is True
    assert result.status == "app_activated_only"
    assert result.focus_target == "codex-app:thread-1"
    assert result.message == (
        "activated app Codex; thread-level focus is not supported yet for thread-1"
    )
    assert len(calls) == 2
    assert calls[0] == ["osascript", "-e", 'tell application "Codex" to activate']
    assert "miniaturized" in calls[1][2]
    assert "System Events" in calls[1][2]


def test_focus_agent_target_reports_restore_warning() -> None:
    """窗口恢复命令失败时，返回 message 中保留 warning，不影响 App 激活成功判定。"""

    def fake_runner(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        script = args[2]
        if "System Events" in script:
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="",
                stderr="not allowed",
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    result = focus_agent_target("app:Codex", runner=fake_runner)

    assert result.ok is True
    assert result.status == "succeeded"
    assert result.focus_target == "app:Codex"
    assert result.message.startswith("activated app Codex;")
    assert "unable to restore minimized windows" in result.message
