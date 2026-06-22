"""Codex app-server quota 适配器。

本模块通过 `codex -s read-only -a untrusted app-server` 的行分隔 JSON-RPC 2.0
接口读取账号 rate limit 信息，并转换为 Agent Deck 可展示的稳定模型。它不读取或
修改 Codex 配置，不发送 prompt，不执行工具，不连接 Agent Deck daemon，也不访问
StreamDock 硬件。外部副作用仅限用户显式调用读取函数时启动一个短生命周期 Codex
子进程，并在超时或完成后终止该进程。
"""

from __future__ import annotations

import json
import select
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from agent_deck.adapters.codex_plan import (
    PLAN_TYPE_SOURCE_URL,
    describe_codex_plan,
    display_plan_name,
)

DEFAULT_CODEX_APP_SERVER_COMMAND: Final[tuple[str, ...]] = (
    "codex",
    "-s",
    "read-only",
    "-a",
    "untrusted",
    "app-server",
)

class CodexQuotaWindow(BaseModel):
    """Codex quota 的单个时间窗口。

    入参：`used_percent` 是窗口内已使用百分比；`window_duration_mins` 是窗口长度；
    `resets_at` 是本地时区下的重置时间。
    返回：Pydantic model，可供触屏渲染或 API 输出。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；仅保存解析结果。
    """

    model_config = ConfigDict(frozen=True)

    used_percent: int = Field(ge=0)
    window_duration_mins: int = Field(gt=0)
    resets_at: datetime

    def reset_label(self, *, include_date: bool) -> str:
        """格式化重置时间为触屏短标签。

        入参：`include_date` 控制是否包含月日；5 小时窗口通常只需要 `HH:MM`，
        weekly 窗口需要 `MM-DD HH:MM`。
        返回：短时间字符串。
        错误处理：datetime 格式化异常按 Python 标准异常传播。
        副作用：无；只格式化内存时间。
        """

        return self.resets_at.strftime("%m-%d %H:%M" if include_date else "%H:%M")


class CodexQuotaSnapshot(BaseModel):
    """Codex 当前 quota 快照。

    入参：`plan_type` 是 Codex app-server 返回的标准 plan type；`plan_short_label`
    是 N4 Pro 小屏主标签；`plan_display_name` 是 Agent Deck 完整展示名；`primary`
    是 5 小时窗口；`secondary` 是 weekly 窗口；`credits_balance` 是可选 credits 文本；
    `reset_credits_available` 是账号当前可用的 earned reset 数量；`raw` 保留原始 result
    子集用于调试。
    返回：Pydantic model，可被 renderer 和 CLI 复用。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；仅保存解析结果。
    """

    model_config = ConfigDict(frozen=True)

    plan_type: str | None
    plan_short_label: str | None = None
    plan_display_name: str
    primary: CodexQuotaWindow
    secondary: CodexQuotaWindow
    credits_balance: str | None = None
    reset_credits_available: int | None = Field(default=None, ge=0)
    raw: dict[str, Any] = Field(default_factory=dict)

    def primary_reset_label(self) -> str:
        """返回 5 小时窗口重置时间标签。

        入参：无。
        返回：`HH:MM` 格式的本地时间。
        错误处理：时间格式化异常按 Python 标准异常传播。
        副作用：无。
        """

        return self.primary.reset_label(include_date=False)

    def secondary_reset_label(self) -> str:
        """返回 weekly 窗口重置时间标签。

        入参：无。
        返回：`MM-DD HH:MM` 格式的本地时间。
        错误处理：时间格式化异常按 Python 标准异常传播。
        副作用：无。
        """

        return self.secondary.reset_label(include_date=True)

def parse_rate_limits_response(
    response: dict[str, Any],
    *,
    timezone: ZoneInfo | None = None,
) -> CodexQuotaSnapshot:
    """解析 `account/rateLimits/read` 的 JSON-RPC 响应。

    入参：`response` 是 app-server 对应 request id 的响应 object；`timezone` 是展示时区，
    默认使用 `Asia/Shanghai`。
    返回：`CodexQuotaSnapshot`。
    错误处理：响应里存在 JSON-RPC error 或必要字段缺失时抛 ValueError/KeyError；
    时间戳非法时由 datetime 抛异常。
    副作用：无；不启动进程、不访问文件。
    """

    if "error" in response:
        raise ValueError(f"Codex app-server returned error: {response['error']}")
    result = _require_mapping(response.get("result"), "result")
    rate_limits = _require_mapping(result.get("rateLimits"), "result.rateLimits")
    primary = _parse_window(rate_limits.get("primary"), "primary", timezone=timezone)
    secondary = _parse_window(rate_limits.get("secondary"), "secondary", timezone=timezone)
    plan_type = _optional_str(rate_limits.get("planType"))
    plan_display = describe_codex_plan(plan_type)
    credits = rate_limits.get("credits")
    credits_balance = None
    if isinstance(credits, dict):
        credits_balance = _optional_str(credits.get("balance"))
    reset_credits_available = _parse_reset_credits_available(
        result.get("rateLimitResetCredits")
    )

    return CodexQuotaSnapshot(
        plan_type=plan_type,
        plan_short_label=plan_display.short_label,
        plan_display_name=plan_display.display_name,
        primary=primary,
        secondary=secondary,
        credits_balance=credits_balance,
        reset_credits_available=reset_credits_available,
        raw=result,
    )


def read_codex_quota(
    *,
    command: tuple[str, ...] = DEFAULT_CODEX_APP_SERVER_COMMAND,
    timeout_seconds: float = 10.0,
    timezone: ZoneInfo | None = None,
    client_name: str = "agent-deck",
    client_version: str = "0.1.0",
) -> CodexQuotaSnapshot:
    """通过 Codex app-server 读取当前账号 quota。

    入参：`command` 是 app-server 启动命令；`timeout_seconds` 是初始化和读取响应的总超时；
    `timezone` 是重置时间展示时区；`client_name`/`client_version` 会写入 initialize
    的 `clientInfo`。
    返回：`CodexQuotaSnapshot`。
    错误处理：Codex CLI 不存在、进程启动失败、超时、JSON 解析失败或 app-server 返回错误时
    抛出对应异常；调用方负责降级为 unknown 或向用户展示错误。
    副作用：启动短生命周期 Codex 子进程，向 stdin 写入两行 JSON-RPC，请求后终止进程。
    """

    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        _send_json_rpc(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": client_name,
                        "version": client_version,
                    }
                },
            },
        )
        _read_until_id(process, 1, timeout_seconds=timeout_seconds)
        _send_json_rpc(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {},
            },
        )
        response = _read_until_id(process, 2, timeout_seconds=timeout_seconds)
        return parse_rate_limits_response(response, timezone=timezone)
    finally:
        _terminate_process(process)


def _parse_window(
    payload: object,
    name: str,
    *,
    timezone: ZoneInfo | None,
) -> CodexQuotaWindow:
    """解析单个 quota window。

    入参：`payload` 是 window object；`name` 用于错误消息；`timezone` 是时间转换时区。
    返回：`CodexQuotaWindow`。
    错误处理：字段缺失或类型不符合预期时抛 KeyError/ValueError。
    副作用：无。
    """

    data = _require_mapping(payload, name)
    tz = timezone or ZoneInfo("Asia/Shanghai")
    return CodexQuotaWindow(
        used_percent=int(data["usedPercent"]),
        window_duration_mins=int(data["windowDurationMins"]),
        resets_at=datetime.fromtimestamp(int(data["resetsAt"]), tz),
    )


def _require_mapping(value: object, name: str) -> dict[str, Any]:
    """要求某个 JSON 字段是 object。

    入参：`value` 是待检查字段；`name` 是字段路径。
    返回：类型收窄后的 dict。
    错误处理：不是 dict 时抛 ValueError。
    副作用：无。
    """

    if not isinstance(value, dict):
        raise ValueError(f"expected object at {name}")
    return value


def _optional_str(value: object) -> str | None:
    """把可选 JSON 值转换成字符串。

    入参：`value` 是 JSON 字段值。
    返回：None 或字符串化结果。
    错误处理：无。
    副作用：无。
    """

    if value is None:
        return None
    return str(value)


def _parse_reset_credits_available(payload: object) -> int | None:
    """解析可用 Codex earned reset 数量。

    入参：`payload` 是 app-server `rateLimitResetCredits` object，可能为空或缺失。
    返回：非负整数或 None；None 表示服务端未返回该字段。
    错误处理：字段存在但不是 object 或 `availableCount` 不能转成整数时抛异常。
    副作用：无。
    """

    if payload is None:
        return None
    data = _require_mapping(payload, "result.rateLimitResetCredits")
    value = data.get("availableCount")
    if value is None:
        return None
    return max(0, int(value))


def _send_json_rpc(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    """向 app-server stdin 写入一行 JSON-RPC。

    入参：`process` 是已启动的 Codex 子进程；`payload` 是 JSON-RPC object。
    返回：无返回值。
    错误处理：stdin 不可用时抛 RuntimeError；写入失败由底层 pipe 异常传播。
    副作用：写入子进程 stdin。
    """

    if process.stdin is None:
        raise RuntimeError("Codex app-server stdin is not available")
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_until_id(
    process: subprocess.Popen[str],
    target_id: int,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """读取 app-server stdout，直到出现指定 JSON-RPC id 的响应。

    入参：`process` 是 Codex 子进程；`target_id` 是目标 request id；`timeout_seconds`
    是最长等待时间。
    返回：匹配 id 的 JSON object；期间收到的 notification 会被忽略。
    错误处理：stdout 不可用、超时、JSON 非 object 或进程提前退出时抛异常。
    副作用：从子进程 stdout 消费若干行。
    """

    if process.stdout is None:
        raise RuntimeError("Codex app-server stdout is not available")
    deadline = time.monotonic() + timeout_seconds
    last_line: str | None = None
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([process.stdout], [], [], min(remaining, 1.0))
        if not ready:
            if process.poll() is not None:
                stderr = _read_stderr(process)
                raise RuntimeError(f"Codex app-server exited early: {stderr}")
            continue
        line = process.stdout.readline()
        if not line:
            break
        last_line = line.rstrip("\n")
        payload = json.loads(last_line)
        if not isinstance(payload, dict):
            raise ValueError("Codex app-server emitted non-object JSON")
        if payload.get("id") == target_id:
            return payload
    raise TimeoutError(
        f"timed out waiting for Codex app-server response id={target_id}; last={last_line!r}"
    )


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """终止 Codex app-server 子进程。

    入参：`process` 是待清理子进程。
    返回：无返回值。
    错误处理：terminate 后 3 秒仍未退出则 kill；kill 后异常按 subprocess 传播。
    副作用：向子进程发送终止信号。
    """

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _read_stderr(process: subprocess.Popen[str]) -> str:
    """读取子进程 stderr 作为错误上下文。

    入参：`process` 是 Codex 子进程。
    返回：stderr 文本；不可读取时为空字符串。
    错误处理：底层读取异常会被吞掉，避免覆盖主错误。
    副作用：可能消费 stderr pipe。
    """

    if process.stderr is None:
        return ""
    try:
        return process.stderr.read()
    except Exception:
        return ""


def write_plan_type_reference(path: Path) -> None:
    """写入 Codex PlanType 来源记录。

    入参：`path` 是目标 markdown 路径。
    返回：无返回值。
    错误处理：目录不可写时文件系统异常传播。
    副作用：创建父目录并写入 markdown 文件。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Codex App-Server Quota Notes",
                "",
                "Codex app-server 的 `account/rateLimits/read` 返回 `planType`。",
                f"标准 PlanType 类型来源：{PLAN_TYPE_SOURCE_URL}",
                "",
                "当前 Agent Deck 暂定展示映射：",
                "",
                "- `prolite` -> `ProLite`",
                "- `business` / `self_serve_business_usage_based` -> `Biz`",
                "- `enterprise` / `enterprise_cbp_usage_based` -> `Ent`",
                "",
            ]
        ),
        encoding="utf-8",
    )
