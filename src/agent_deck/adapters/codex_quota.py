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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    入参：`window_id` 是 Agent Deck 生成的稳定窗口标识；`limit_id`/`limit_name` 描述所属
    服务端限额；`source_slot` 是该限额内部的原始字段名；其余字段来自 app-server。
    返回：Pydantic model，可供触屏渲染或 API 输出。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；仅保存解析结果。
    """

    model_config = ConfigDict(frozen=True)

    window_id: str = "legacy:primary"
    limit_id: str = "legacy"
    limit_name: str | None = None
    presentation_label: str | None = None
    source_slot: str = "primary"
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

    def display_period_label(self) -> str:
        """根据 app-server 返回的窗口时长生成稳定的短周期标签。

        入参：无；使用当前窗口的 `window_duration_mins`。
        返回：适合 N4 Pro 按键和 touch bar 的 `5H`、`WEEK`、`MONTH` 等短标签。
        错误处理：时长由模型约束为正整数；未知粒度降级为分钟标签。
        副作用：无；只读取内存字段。
        """

        return quota_window_period_label(self.window_duration_mins)

    def display_reset_label(self, *, now: datetime | None = None) -> str:
        """按“当天显示时间，其他日期显示日期和时间”的规则格式化重置时间。

        入参：`now` 可用于测试或预览中固定当前时间；未传时使用窗口时区下的当前时间。
        返回：`HH:MM` 或 `MM-DD HH:MM` 格式的短文本。
        错误处理：窗口时间由 adapter 保证带时区；底层时间格式化异常按标准语义传播。
        副作用：无；不读取 quota 之外的外部状态。
        """

        reference = now or datetime.now(self.resets_at.tzinfo)
        include_date = reference.astimezone(self.resets_at.tzinfo).date() != self.resets_at.date()
        return self.reset_label(include_date=include_date)

    def display_label(self) -> str:
        """返回包含可选限额名称和周期的触屏短标签。

        入参：无。
        返回：主限额仅返回周期，例如 `WEEK`；具名额外限额返回 `名称 · WEEK`。
        错误处理：无；无名称时安全回退为周期标签。
        副作用：无；只读取内存字段。
        """

        period = self.display_period_label()
        label = self.presentation_label or self.limit_name
        return f"{label} · {period}" if label else period


class CodexQuotaSnapshot(BaseModel):
    """Codex 当前 quota 快照。

    入参：`plan_type` 是 Codex app-server 返回的标准 plan type；`plan_short_label`
    是 N4 Pro 小屏主标签；`plan_display_name` 是 Agent Deck 完整展示名；`windows` 是任意
    数量的实际限额窗口集合，不把 app-server 当前的 primary/secondary 传输字段当成领域模型；
    `credits_balance` 是可选 credits 文本；
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
    windows: tuple[CodexQuotaWindow, ...] = ()
    credits_balance: str | None = None
    reset_credits_available: int | None = Field(default=None, ge=0)
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_window_fields(cls, value: object) -> object:
        """把旧测试、缓存或调用方的 primary/secondary 字段迁移为窗口集合。

        入参：`value` 是 Pydantic 即将验证的原始 object。
        返回：已有 `windows` 时原样返回；否则从旧槽位构造泛化窗口列表。
        错误处理：非 mapping 值交由 Pydantic 后续标准错误处理。
        副作用：只复制输入 mapping，不修改调用方对象。
        """

        if not isinstance(value, dict) or "windows" in value:
            return value
        migrated = dict(value)
        limit_id = str(migrated.get("limit_id") or "codex")
        windows: list[dict[str, object]] = []
        for slot in ("primary", "secondary"):
            payload = migrated.pop(slot, None)
            if isinstance(payload, dict):
                windows.append(
                    {
                        **payload,
                        "window_id": f"{limit_id}:{slot}",
                        "limit_id": limit_id,
                        "source_slot": slot,
                    }
                )
        migrated["windows"] = windows
        return migrated

    def available_windows(self) -> tuple[CodexQuotaWindow, ...]:
        """返回当前快照实际可展示的 quota 窗口，保持服务端稳定排序。

        入参：无。
        返回：由窗口模型组成的非空元组，按主 limit 后额外 limit、原始槽位顺序排列。
        错误处理：模型构造阶段已保证至少一个窗口存在，因此不会返回空元组。
        副作用：无；只读取内存字段。
        """

        return self.windows

    def window_for_id(self, window_id: str) -> CodexQuotaWindow | None:
        """按稳定 window_id 查找一个实际 quota 窗口。

        入参：`window_id` 是配置与渲染路径保存的窗口标识。
        返回：匹配窗口或 None。
        错误处理：无；未知 id 作为正常兼容场景处理。
        副作用：无；只读取内存集合。
        """

        return next((item for item in self.windows if item.window_id == window_id), None)

    def resolved_window(self, selection: str | None) -> CodexQuotaWindow:
        """按配置选择实际窗口，并兼容旧 primary/secondary 配置值。

        入参：`selection` 是 `auto`、稳定 window_id 或旧槽位名。
        返回：命中的窗口；未知或缺失选择回退到当前最紧张窗口。
        错误处理：模型构造阶段保证至少存在一个窗口。
        副作用：无；不覆盖用户保存的原始配置。
        """

        if selection and selection != "auto":
            matched = self.window_for_id(selection)
            if matched is not None:
                return matched
            legacy = next(
                (item for item in self.windows if item.source_slot == selection),
                None,
            )
            if legacy is not None:
                return legacy
        return max(self.windows, key=lambda item: item.used_percent)

    @model_validator(mode="after")
    def _validate_at_least_one_window(self) -> CodexQuotaSnapshot:
        """拒绝没有任何可展示 quota 窗口的 app-server 响应。

        入参：当前已解析的快照模型。
        返回：当前模型实例。
        错误处理：primary 与 secondary 同时缺失时抛 ValueError。
        副作用：无。
        """

        if not self.windows:
            raise ValueError("Codex rate limits did not include a usable quota window")
        window_ids = [item.window_id for item in self.windows]
        if len(window_ids) != len(set(window_ids)):
            raise ValueError("Codex rate limit window ids must be unique")
        return self

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
    windows = list(_parse_limit_windows(rate_limits, timezone=timezone))
    limits_by_id = result.get("rateLimitsByLimitId")
    if isinstance(limits_by_id, dict):
        for limit_key, payload in limits_by_id.items():
            if not isinstance(payload, dict):
                continue
            limit_id = str(payload.get("limitId") or limit_key)
            if any(item.limit_id == limit_id for item in windows):
                continue
            windows.extend(_parse_limit_windows(payload, timezone=timezone))
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
        windows=tuple(windows),
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


def _parse_limit_windows(
    payload: dict[str, Any],
    *,
    timezone: ZoneInfo | None,
) -> tuple[CodexQuotaWindow, ...]:
    """从一个 app-server rate limit object 提取任意数量的窗口字段。

    入参：`payload` 是 `rateLimits` 或 `rateLimitsByLimitId` 中的一项；`timezone` 是展示时区。
    返回：按原始字段顺序排列的 quota 窗口元组；可为空。
    错误处理：候选字段看似窗口但缺少必要字段时抛 ValueError，避免静默展示错误配额。
    副作用：无；只读取 JSON object。
    """

    limit_id = str(payload.get("limitId") or "codex")
    limit_name = _optional_str(payload.get("limitName"))
    windows: list[CodexQuotaWindow] = []
    for source_slot, candidate in payload.items():
        if not _looks_like_quota_window(candidate):
            continue
        data = _require_mapping(candidate, f"rate limit {limit_id}.{source_slot}")
        tz = timezone or ZoneInfo("Asia/Shanghai")
        windows.append(
            CodexQuotaWindow(
                window_id=f"{limit_id}:{source_slot}",
                limit_id=limit_id,
                limit_name=limit_name,
                source_slot=source_slot,
                used_percent=int(data["usedPercent"]),
                window_duration_mins=int(data["windowDurationMins"]),
                resets_at=datetime.fromtimestamp(int(data["resetsAt"]), tz),
            )
        )
    return tuple(windows)


def _looks_like_quota_window(value: object) -> bool:
    """判断一个 JSON 字段是否具备 quota window 所需的最小结构。

    入参：`value` 是 rate-limit object 内某个字段的值。
    返回：同时包含已用比例、窗口长度和重置时间字段时返回 True。
    错误处理：无；非 mapping 安全返回 False。
    副作用：无。
    """

    return isinstance(value, dict) and {
        "usedPercent",
        "windowDurationMins",
        "resetsAt",
    }.issubset(value)


def quota_window_period_label(window_duration_mins: int) -> str:
    """把服务端窗口长度转换成设备上可读的周期标签。

    入参：`window_duration_mins` 是 app-server 返回的正整数分钟数。
    返回：优先使用 `H`、`DAY`、`WEEK`、`MONTH`；非规则时长降级为 `N M`。
    错误处理：非正整数抛 ValueError，保护直接调用方不产生无意义标签。
    副作用：无；纯格式化函数。
    """

    if window_duration_mins <= 0:
        raise ValueError("quota window duration must be positive")
    minutes_per_hour = 60
    minutes_per_day = 24 * minutes_per_hour
    minutes_per_week = 7 * minutes_per_day
    if 28 * minutes_per_day <= window_duration_mins <= 31 * minutes_per_day:
        return "MONTH"
    if window_duration_mins % minutes_per_week == 0:
        weeks = window_duration_mins // minutes_per_week
        return "WEEK" if weeks == 1 else f"{weeks}W"
    if window_duration_mins % minutes_per_day == 0:
        days = window_duration_mins // minutes_per_day
        return "DAY" if days == 1 else f"{days}D"
    if window_duration_mins % minutes_per_hour == 0:
        return f"{window_duration_mins // minutes_per_hour}H"
    return f"{window_duration_mins}M"


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
