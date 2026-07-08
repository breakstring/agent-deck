"""本机 URL 快捷动作执行器。

本模块只把已经通过 runtime 映射的 URL intent 转换成受控 macOS `open` 调用。
它不接受任意 shell 字符串、不拼接命令、不执行文本输入，也不读取目标文件夹内容。
URL 第一版仅允许 http/https。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

OpenRunner = Callable[..., subprocess.CompletedProcess[str]]


class LocalTargetActionResult(BaseModel):
    """描述一次本机 URL 快捷动作结果。

    入参：`ok` 表示动作是否成功；`status` 是稳定状态；`target_type` 当前固定为 url；
    `url` 透传目标；`message` 是给 status 的诊断文本。
    返回：frozen Pydantic model，可进入 daemon status。
    错误处理：字段非法由 Pydantic 报告。
    副作用：模型自身不执行动作。
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    status: str
    target_type: str
    url: str | None = None
    message: str


def open_local_url(
    *,
    url: str | None = None,
    runner: OpenRunner = subprocess.run,
) -> LocalTargetActionResult:
    """用系统默认处理器打开一个 http/https URL。

    入参：`url` 是待打开 URL；`runner` 是可注入 subprocess runner。
    返回：动作成功或失败诊断。
    错误处理：缺少 URL、非 http/https scheme、runner 异常或非 0 exit 都返回失败结果。
    副作用：成功路径调用 macOS `open <url>`；不使用 shell。
    """

    cleaned = (url or "").strip()
    if not cleaned:
        return LocalTargetActionResult(
            ok=False,
            status="unsupported",
            target_type="url",
            url=url,
            message="open_url ignored; missing url",
        )
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        return LocalTargetActionResult(
            ok=False,
            status="unsupported",
            target_type="url",
            url=cleaned,
            message=f"open_url ignored; unsupported url scheme: {parsed.scheme or '<none>'}",
        )
    return _run_open_target(["open", cleaned], target_type="url", url=cleaned, runner=runner)


def _run_open_target(
    args: list[str],
    *,
    target_type: str,
    runner: OpenRunner,
    url: str | None = None,
) -> LocalTargetActionResult:
    """执行结构化 `open` 命令并转换为 action 结果。

    入参：`args` 是完整命令参数；`target_type` 当前固定为 url；`runner` 是 subprocess runner；
    `url` 是目标诊断字段。
    返回：成功或失败的 `LocalTargetActionResult`。
    错误处理：runner 抛异常或返回非 0 exit 时返回 failed。
    副作用：调用传入 runner；生产默认会执行 macOS `open`。
    """

    try:
        completed = runner(
            args,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - 系统动作必须转换为可诊断结果。
        return LocalTargetActionResult(
            ok=False,
            status="failed",
            target_type=target_type,
            url=url,
            message=f"open_{target_type} failed: {type(exc).__name__}: {exc}",
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = f"open_{target_type} failed with exit {completed.returncode}"
        if detail:
            message = f"{message}: {detail}"
        return LocalTargetActionResult(
            ok=False,
            status="failed",
            target_type=target_type,
            url=url,
            message=message,
        )
    return LocalTargetActionResult(
        ok=True,
        status="succeeded",
        target_type=target_type,
        url=url,
        message=f"opened {url}",
    )
