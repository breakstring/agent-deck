"""Codex token usage 的 ccusage 适配器。

本模块把 `ccusage codex daily --compact --json` 的结构化输出转换为 Agent Deck 可展示的
token usage snapshot。默认读取函数会执行 `bunx ccusage ...`，但核心解析和测试路径通过
可注入 runner 工作，不读取 Codex 日志、不访问网络、不连接硬件，也不修改用户配置。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

DEFAULT_CCUSAGE_CODEX_DAILY_COMMAND: Final[tuple[str, ...]] = (
    "bunx",
    "ccusage",
    "codex",
    "daily",
    "--compact",
    "--json",
)


class CodexTokenPeriod(StrEnum):
    """描述 token usage 面板可切换的统计周期。

    入参：枚举值是稳定字符串，用于 panel 和输入 intent 切换。
    返回：作为 snapshot 字典 key 和展示周期约束。
    错误处理：未知字符串由 Enum/Pydantic 校验拒绝。
    副作用：无；定义枚举不访问外部环境。
    """

    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    ALL = "all"


class CodexTokenUsageStats(BaseModel):
    """某个统计周期内的 Codex token 和费用汇总。

    入参：字段来自 ccusage JSON，包括 input/output/reasoning/cache read/total tokens 和 USD cost。
    返回：frozen Pydantic model，并提供适合小屏展示的格式化标签属性。
    错误处理：负 token 或负 cost 由 Pydantic 校验拒绝。
    副作用：仅保存内存数据，不访问外部命令。
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)

    @property
    def input_tokens_label(self) -> str:
        """返回 input tokens 的紧凑展示标签。

        入参：无。
        返回：如 `6.47M` 的短字符串。
        错误处理：无。
        副作用：无。
        """

        return format_token_count(self.input_tokens)

    @property
    def output_tokens_label(self) -> str:
        """返回 output tokens 的紧凑展示标签。

        入参：无。
        返回：如 `437K` 的短字符串。
        错误处理：无。
        副作用：无。
        """

        return format_token_count(self.output_tokens)

    @property
    def reasoning_output_tokens_label(self) -> str:
        """返回 reasoning tokens 的紧凑展示标签。

        入参：无。
        返回：如 `110K` 的短字符串。
        错误处理：无。
        副作用：无。
        """

        return format_token_count(self.reasoning_output_tokens)

    @property
    def cache_read_tokens_label(self) -> str:
        """返回 cache read tokens 的紧凑展示标签。

        入参：无。
        返回：如 `111M` 的短字符串。
        错误处理：无。
        副作用：无。
        """

        return format_token_count(self.cache_read_tokens)

    @property
    def total_tokens_label(self) -> str:
        """返回 total tokens 的紧凑展示标签。

        入参：无。
        返回：如 `118M` 的短字符串。
        错误处理：无。
        副作用：无。
        """

        return format_token_count(self.total_tokens)

    @property
    def cost_label(self) -> str:
        """返回 cost 的美元展示标签。

        入参：无。
        返回：如 `$100.98` 的短字符串。
        错误处理：无。
        副作用：无。
        """

        return format_cost_usd(self.cost_usd)


class CodexTokenUsageSnapshot(BaseModel):
    """Codex token usage 的四周期快照。

    入参：`periods` 必须包含 today/week/month/all；`updated_at` 是本次读取时间；
    `raw` 保留 ccusage 原始 JSON 子集用于调试。
    返回：frozen Pydantic model，可供 logical panel 或 daemon status 使用。
    错误处理：缺少任一周期时抛 ValueError。
    副作用：模型自身不执行 ccusage。
    """

    model_config = ConfigDict(frozen=True)

    periods: Mapping[CodexTokenPeriod, CodexTokenUsageStats]
    updated_at: datetime
    raw: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: datetime) -> datetime:
        """校验快照更新时间必须带时区。

        入参：`value` 是调用方提供的更新时间。
        返回：原始 timezone-aware datetime。
        错误处理：naive datetime 抛 ValueError。
        副作用：无。
        """

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("token usage updated_at must be timezone-aware")
        return value

    @field_serializer("periods")
    def _serialize_periods(
        self,
        value: Mapping[CodexTokenPeriod, CodexTokenUsageStats],
    ) -> dict[str, Any]:
        """把枚举 key 转成 JSON-safe 字符串 key。

        入参：`value` 是当前 periods mapping。
        返回：字符串 key 的 dict。
        错误处理：内部 model dump 异常按 Pydantic 语义传播。
        副作用：无；只复制内存结构。
        """

        return {period.value: stats.model_dump() for period, stats in value.items()}

    def model_post_init(self, __context: Any) -> None:
        """校验四个统计周期都存在。

        入参：Pydantic 传入的上下文对象，当前不使用。
        返回：无。
        错误处理：缺少周期时抛 ValueError。
        副作用：无。
        """

        missing = set(CodexTokenPeriod) - set(self.periods)
        if missing:
            joined = ", ".join(sorted(period.value for period in missing))
            raise ValueError(f"missing token usage periods: {joined}")


CcusageRunner = Callable[[tuple[str, ...], float], str]
TokenUsageReader = Callable[[], CodexTokenUsageSnapshot]
Clock = Callable[[], datetime]


class CodexTokenUsageCache:
    """缓存 Codex token usage 快照，避免频繁执行 ccusage。

    入参：`reader` 是读取快照的函数；`ttl_seconds` 是缓存有效期，建议分钟级；
    `clock` 可注入以便测试控制时间。
    返回：普通 Python 对象，通过 `get()` 返回最新或缓存的 `CodexTokenUsageSnapshot`。
    错误处理：TTL 非正数或 clock 返回 naive datetime 时抛 ValueError；reader 异常按原样传播。
    副作用：缓存 miss、过期或强制刷新时会调用 reader，reader 可能执行外部 ccusage 命令。
    """

    def __init__(
        self,
        *,
        reader: TokenUsageReader | None = None,
        ttl_seconds: float = 300.0,
        clock: Clock | None = None,
    ) -> None:
        """初始化 token usage cache。

        入参：`reader` 是读取函数；`ttl_seconds` 是缓存秒数；`clock` 默认返回 UTC 当前时间。
        返回：无。
        错误处理：TTL 小于等于 0 时抛 ValueError。
        副作用：只保存依赖，不立即执行 reader。
        """

        if ttl_seconds <= 0:
            raise ValueError("token usage cache ttl must be positive")
        self._reader = reader or read_codex_token_usage
        self._ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._snapshot: CodexTokenUsageSnapshot | None = None
        self._last_refresh_at: datetime | None = None

    @property
    def snapshot(self) -> CodexTokenUsageSnapshot | None:
        """返回当前缓存快照。

        入参：无。
        返回：若尚未成功读取则为 None，否则为最近一次快照。
        错误处理：无。
        副作用：无；不会触发刷新。
        """

        return self._snapshot

    @property
    def last_refresh_at(self) -> datetime | None:
        """返回最近一次刷新时间。

        入参：无。
        返回：timezone-aware datetime，若尚未刷新则为 None。
        错误处理：无。
        副作用：无；不会触发刷新。
        """

        return self._last_refresh_at

    def get(self, *, force_refresh: bool = False) -> CodexTokenUsageSnapshot:
        """返回 token usage 快照，必要时刷新缓存。

        入参：`force_refresh` 为 True 时忽略 TTL 立即调用 reader。
        返回：最新可用的 `CodexTokenUsageSnapshot`。
        错误处理：clock 返回 naive datetime 时抛 ValueError；reader 异常按原样传播且不覆盖旧缓存。
        副作用：可能调用 reader，reader 可能执行外部 ccusage 命令。
        """

        now = self._aware_now()
        if (
            force_refresh
            or self._snapshot is None
            or self._last_refresh_at is None
            or (now - self._last_refresh_at).total_seconds() >= self._ttl_seconds
        ):
            snapshot = self._reader()
            self._snapshot = snapshot
            self._last_refresh_at = now
        return self._snapshot

    def _aware_now(self) -> datetime:
        """读取当前时间并校验 timezone-aware。

        入参：无。
        返回：clock 提供的 timezone-aware datetime。
        错误处理：naive datetime 抛 ValueError。
        副作用：调用注入的 clock。
        """

        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("token usage cache clock must return timezone-aware datetime")
        return value


def parse_ccusage_codex_daily_json(
    payload: Mapping[str, Any],
    *,
    reference_date: date | None = None,
    updated_at: datetime | None = None,
) -> CodexTokenUsageSnapshot:
    """解析 ccusage Codex daily JSON 并聚合成 today/week/month/all。

    入参：`payload` 是 `ccusage codex daily --compact --json` 的 JSON object；
    `reference_date` 决定 today/week/month 的边界，默认使用本地当天；`updated_at` 是读取时间。
    返回：`CodexTokenUsageSnapshot`。
    错误处理：顶层字段缺失、daily row 日期非法或数值非法时抛 ValueError/KeyError。
    副作用：无；不执行外部命令。
    """

    ref = reference_date or date.today()
    rows = payload.get("daily", ())
    if not isinstance(rows, list):
        raise ValueError("ccusage payload daily must be a list")

    today = _zero_stats()
    week = _zero_stats()
    month = _zero_stats()
    for row in rows:
        stats = _stats_from_mapping(_require_mapping(row, "daily row"))
        row_date = date.fromisoformat(str(_require_mapping(row, "daily row")["date"]))
        if row_date == ref:
            today = _add_stats(today, stats)
        if row_date.isocalendar()[:2] == ref.isocalendar()[:2]:
            week = _add_stats(week, stats)
        if row_date.year == ref.year and row_date.month == ref.month:
            month = _add_stats(month, stats)

    totals_payload = payload.get("totals")
    all_stats = (
        _stats_from_mapping(_require_mapping(totals_payload, "totals"))
        if isinstance(totals_payload, Mapping)
        else _aggregate_rows(rows)
    )
    return CodexTokenUsageSnapshot(
        periods={
            CodexTokenPeriod.TODAY: today,
            CodexTokenPeriod.WEEK: week,
            CodexTokenPeriod.MONTH: month,
            CodexTokenPeriod.ALL: all_stats,
        },
        updated_at=updated_at or datetime.now(UTC),
        raw=dict(payload),
    )


def read_codex_token_usage(
    *,
    command: tuple[str, ...] = DEFAULT_CCUSAGE_CODEX_DAILY_COMMAND,
    timeout_seconds: float = 10.0,
    reference_date: date | None = None,
    runner: CcusageRunner | None = None,
) -> CodexTokenUsageSnapshot:
    """通过 ccusage 读取 Codex token usage。

    入参：`command` 默认执行 `bunx ccusage codex daily --compact --json`；`timeout_seconds`
    是外部命令超时；`reference_date` 控制周期聚合；`runner` 可注入以便测试不执行真实命令。
    返回：`CodexTokenUsageSnapshot`。
    错误处理：外部命令失败、超时或 JSON 非法时抛 ValueError。
    副作用：无 runner 时会启动一个外部 `bunx ccusage ...` 进程，只读扫描 ccusage 支持的数据源。
    """

    active_runner = runner or _run_ccusage_command
    try:
        raw_output = active_runner(command, timeout_seconds)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        raise ValueError(f"ccusage command failed: {exc}") from exc
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ccusage returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("ccusage JSON output must be an object")
    return parse_ccusage_codex_daily_json(payload, reference_date=reference_date)


def format_token_count(value: int) -> str:
    """把 token 数格式化为适合小屏的紧凑单位。

    入参：`value` 是非负 token 数。
    返回：最多约三位有效数字的字符串，例如 `999`、`1.2K`、`6.47M`、`118M`。
    错误处理：负值抛 ValueError。
    副作用：无。
    """

    if value < 0:
        raise ValueError("token count must not be negative")
    units = (("", 1), ("K", 1_000), ("M", 1_000_000), ("B", 1_000_000_000))
    label = ""
    divisor = 1
    for candidate_label, candidate_divisor in units:
        if value >= candidate_divisor:
            label = candidate_label
            divisor = candidate_divisor
    if divisor == 1:
        return str(value)
    scaled = value / divisor
    if scaled >= 100:
        text = f"{scaled:.0f}"
    elif scaled >= 10:
        text = f"{scaled:.1f}"
    else:
        text = f"{scaled:.2f}".rstrip("0").rstrip(".")
    return f"{text}{label}"


def format_cost_usd(value: float) -> str:
    """把美元费用格式化为适合小屏的金额字符串。

    入参：`value` 是非负 USD 金额。
    返回：大于等于 1 美元时保留两位小数；小于 1 美元时保留足够精度但去掉末尾 0。
    错误处理：负值抛 ValueError。
    副作用：无。
    """

    if value < 0:
        raise ValueError("cost must not be negative")
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.01:
        return f"${value:.3f}".rstrip("0").rstrip(".")
    return f"${value:.4f}".rstrip("0").rstrip(".")


def _run_ccusage_command(command: tuple[str, ...], timeout_seconds: float) -> str:
    """执行 ccusage 命令并返回 stdout。

    入参：`command` 是完整命令元组；`timeout_seconds` 是 subprocess 超时。
    返回：stdout 文本。
    错误处理：非 0 退出码、超时或启动失败由 subprocess/OSError 抛出。
    副作用：启动外部进程，只读扫描 ccusage 支持的数据源。
    """

    result = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=True,
    )
    return result.stdout


def _stats_from_mapping(payload: Mapping[str, Any]) -> CodexTokenUsageStats:
    """从 ccusage row 或 totals object 解析 token usage stats。

    入参：`payload` 是 daily row 或 totals mapping。
    返回：`CodexTokenUsageStats`。
    错误处理：字段缺失或类型非法时抛 KeyError/ValueError/Pydantic 校验异常。
    副作用：无。
    """

    return CodexTokenUsageStats(
        input_tokens=int(payload.get("inputTokens", 0)),
        output_tokens=int(payload.get("outputTokens", 0)),
        reasoning_output_tokens=int(payload.get("reasoningOutputTokens", 0)),
        cache_read_tokens=int(payload.get("cacheReadTokens", 0)),
        total_tokens=int(payload.get("totalTokens", 0)),
        cost_usd=float(payload.get("costUSD", payload.get("totalCost", 0.0))),
    )


def _zero_stats() -> CodexTokenUsageStats:
    """返回一个全 0 token usage stats。

    入参：无。
    返回：`CodexTokenUsageStats`。
    错误处理：无。
    副作用：无。
    """

    return CodexTokenUsageStats(
        input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        cache_read_tokens=0,
        total_tokens=0,
        cost_usd=0.0,
    )


def _add_stats(
    left: CodexTokenUsageStats,
    right: CodexTokenUsageStats,
) -> CodexTokenUsageStats:
    """合并两个 token usage stats。

    入参：`left` 和 `right` 是两个统计片段。
    返回：字段逐项相加后的新 `CodexTokenUsageStats`。
    错误处理：Pydantic 校验异常按原语义传播。
    副作用：无；不修改入参。
    """

    return CodexTokenUsageStats(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        reasoning_output_tokens=(
            left.reasoning_output_tokens + right.reasoning_output_tokens
        ),
        cache_read_tokens=left.cache_read_tokens + right.cache_read_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        cost_usd=left.cost_usd + right.cost_usd,
    )


def _aggregate_rows(rows: list[object]) -> CodexTokenUsageStats:
    """聚合 daily rows 为总统计。

    入参：`rows` 是 ccusage daily list。
    返回：所有合法 row 的统计和。
    错误处理：row 非 mapping 时抛 ValueError。
    副作用：无。
    """

    total = _zero_stats()
    for row in rows:
        total = _add_stats(total, _stats_from_mapping(_require_mapping(row, "daily row")))
    return total


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    """要求 JSON 字段为 object。

    入参：`value` 是待检查字段；`name` 用于错误消息。
    返回：mapping 值。
    错误处理：不是 mapping 时抛 ValueError。
    副作用：无。
    """

    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value
