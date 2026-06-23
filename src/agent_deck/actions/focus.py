"""focus_agent 外部动作执行器。

本模块把受信任 runtime 传入的 focus target 转换成 macOS/tmux 等外部动作。当前支持
显式 `app:<AppName>` target，以及只读识别出来的 `codex-app:<thread_id>` target。后者
只激活 Codex App，并明确报告 thread-level focus 暂不支持；不执行 shell 拼接、不输入文本、
不 attach tmux、不通过 Accessibility 点击 App 内部 UI。调用方必须在配置开启后才调用本模块。
"""

from __future__ import annotations

import platform
import textwrap
import subprocess
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

PLATFORM_KEY = "darwin"
FocusRunner = Callable[..., subprocess.CompletedProcess[str]]


class FocusActionResult(BaseModel):
    """描述一次 focus action 执行结果。

    入参：`ok` 表示是否成功；`status` 支持 `succeeded`、`failed`、`unsupported`、
    `app_activated_only`；`focus_target`
    是调用方传入的目标；`message` 是可进入 status 的诊断说明。
    返回：frozen Pydantic model。
    错误处理：字段非法由 Pydantic 报告。
    副作用：模型自身不执行动作。
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    status: str
    focus_target: str
    message: str


def focus_agent_target(
    focus_target: str,
    *,
    runner: FocusRunner = subprocess.run,
) -> FocusActionResult:
    """执行一个显式 focus target。

    入参：`focus_target` 支持 `app:<AppName>` 和 `codex-app:<thread_id>`；`runner` 是可注入
    subprocess runner。
    返回：`FocusActionResult`，说明是否激活成功。
    错误处理：unsupported target 不执行外部命令并返回 unsupported；runner 抛异常或返回非 0
    时返回 failed。
    副作用：支持的 target 会调用 `osascript` 激活 App；不会操作 App 内部 thread。
    """

    normalized = focus_target.strip()
    if normalized.startswith("codex-app:"):
        return _focus_codex_app_thread(normalized, focus_target, runner=runner)
    if not normalized.startswith("app:"):
        return FocusActionResult(
            ok=False,
            status="unsupported",
            focus_target=focus_target,
            message=f"unsupported focus target: {focus_target}",
        )
    app_name = normalized.removeprefix("app:").strip()
    if not app_name:
        return FocusActionResult(
            ok=False,
            status="unsupported",
            focus_target=focus_target,
            message="unsupported focus target: empty app name",
        )
    return _focus_app(app_name, focus_target, runner=runner)


def _focus_app(
    app_name: str,
    focus_target: str,
    *,
    runner: FocusRunner,
) -> FocusActionResult:
    """按当前操作系统执行 app focus。

    入参：`app_name` 为目标 App；`focus_target` 为原始 target；`runner` 为可注入 subprocess runner。
    返回：平台支持时返回具体执行结果；平台不支持时返回 unsupported。
    错误处理：非支持平台不会抛异常，返回 clear unsupported 诊断，避免将不完整动作当成功。
    副作用：仅在 macOS 执行 osascript；Windows/Linux 当前返回 unsupported stub。
    """

    platform_name = platform.system().lower()
    if platform_name == PLATFORM_KEY:
        return _activate_app_macos(app_name, focus_target, runner=runner)
    return _unsupported_platform(focus_target, platform_name)


def _unsupported_platform(focus_target: str, platform_name: str) -> FocusActionResult:
    """返回跨平台未实现的统一诊断。

    入参：`focus_target` 当前执行目标；`platform_name` 为 `platform.system()`。
    返回：`ok=False` + `unsupported`，并提示未来支持计划。
    副作用：无。
    """

    return FocusActionResult(
        ok=False,
        status="unsupported",
        focus_target=focus_target,
        message=f"focus execution on {platform_name} is currently unsupported; planned",
    )


def _focus_codex_app_thread(
    normalized: str,
    original: str,
    *,
    runner: FocusRunner,
) -> FocusActionResult:
    """激活 Codex App，并保留目标 thread 的诊断语义。

    入参：`normalized` 是去空白后的 `codex-app:<thread_id>`；`original` 是调用方原始 target；
    `runner` 是可注入 subprocess runner。
    返回：`FocusActionResult`；激活 App 成功时状态为 `app_activated_only`。
    错误处理：缺少 thread id 时返回 unsupported；激活 App 失败时透传失败诊断。
    副作用：只调用 AppleScript 激活 Codex App，不点击或选择 App 内 thread。
    """

    thread_id = normalized.removeprefix("codex-app:").strip()
    if not thread_id:
        return FocusActionResult(
            ok=False,
            status="unsupported",
            focus_target=original,
            message="unsupported focus target: empty Codex App thread id",
        )
    result = _focus_app("Codex", original, runner=runner)
    if not result.ok:
        return result
    warning_suffix = ""
    if "unable to restore" in result.message.lower():
        warning_suffix = "; " + result.message.removeprefix("activated app Codex; ").removeprefix(
            "activated app Codex"
        )
    return FocusActionResult(
        ok=True,
        status="app_activated_only",
        focus_target=original,
        message=(
            "activated app Codex; thread-level focus is not supported yet "
            f"for {thread_id}"
            f"{warning_suffix}"
        ),
    )


def _activate_app_macos(
    app_name: str,
    focus_target: str,
    *,
    runner: FocusRunner,
) -> FocusActionResult:
    """通过 macOS AppleScript 激活指定 App。

    入参：`app_name` 是 macOS App 名称；`focus_target` 是用于诊断回传的原始目标；
    `runner` 是可注入 subprocess runner。
    返回：`FocusActionResult`，成功状态为 `succeeded`。
    错误处理：runner 异常或退出码非 0 会转换为 failed。
    副作用：调用 `osascript` 激活 App。
    """

    script = f"tell application {_applescript_string(app_name)} to activate"
    activation_result = _run_osascript(script, focus_target, runner=runner)
    if not activation_result.ok:
        return activation_result

    restore_warning = _restore_minimized_windows(app_name, runner=runner)
    message = f"activated app {app_name}"
    if restore_warning:
        message = f"{message}; {restore_warning}"
    return FocusActionResult(
        ok=True,
        status="succeeded",
        focus_target=focus_target,
        message=message,
    )


def _restore_minimized_windows(
    app_name: str,
    *,
    runner: FocusRunner,
) -> str | None:
    """尝试恢复目标应用的最小化窗口。

    入参：`app_name` 是目标 macOS 应用；`runner` 是可注入 subprocess runner。
    返回：`None` 表示命令执行成功或未检测到可恢复窗口；否则返回简短失败说明。
    错误处理：脚本执行失败不会阻断主流程，改为返回 warning 消息供上层拼接。
    副作用：调用 `osascript` 与 `System Events` 恢复 `miniaturized` 状态；可能受无
    Accessibility 权限影响。
    """

    script = textwrap.dedent(
        f'''
        try
            tell application "System Events"
                tell process {_applescript_string(app_name)}
                    set frontmost to true
                    repeat with w in windows
                        try
                            set miniaturized of w to false
                        end try
                        try
                            set value of attribute "AXMinimized" of w to false
                        end try
                    end repeat
                end tell
            end tell
        end try
        '''
    ).strip()

    result = _run_osascript(script, f"app:{app_name}", runner=runner)
    if result.ok:
        return None
    if "activation failed" in result.message.lower():
        return result.message
    return f"unable to restore minimized windows: {result.message}"


def _run_osascript(
    script: str,
    focus_target: str,
    *,
    runner: FocusRunner,
) -> FocusActionResult:
    """执行一段 AppleScript。

    入参：`script` 是待执行的 AppleScript 文本，`focus_target` 仅用于日志和返回体；
    `runner` 是可注入 subprocess runner。
    返回：执行成功返回 `ok=True, status="succeeded"`，失败返回 `ok=False` 并带 stderr/stdout
    诊断信息。
    错误处理：捕获 runner 抛错并转换为 failed status。
    副作用：调用 `osascript` 子进程。
    """

    try:
        completed = runner(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - action 层要把系统错误转成 status 诊断。
        return FocusActionResult(
            ok=False,
            status="failed",
            focus_target=focus_target,
            message=f"focus failed: {type(exc).__name__}: {exc}",
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = f"focus failed with exit {completed.returncode}"
        if detail:
            message = f"{message}: {detail}"
        return FocusActionResult(
            ok=False,
            status="failed",
            focus_target=focus_target,
            message=message,
        )
    return FocusActionResult(
        ok=True,
        status="succeeded",
        focus_target=focus_target,
        message="",
    )


def _applescript_string(value: str) -> str:
    """把字符串转换成 AppleScript 字符串字面量。

    入参：`value` 是 App 名称。
    返回：带双引号的 AppleScript string literal。
    错误处理：无。
    副作用：无。
    """

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
