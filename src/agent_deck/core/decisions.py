"""In-memory approval decision broker for Agent Deck.

This module owns Task 4's async decision lifecycle: callers register pending
approval decisions, resolve them from local UI/API code, and await the result
from hook or daemon code. It intentionally does not perform network, file,
database, hardware, layout, server, or CLI work; all state is process-local and
non-durable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator

_DEFAULT_TIMEOUT_MESSAGE = "Timed out waiting for Agent Deck decision."


class DecisionBehavior(StrEnum):
    """Represent the executable outcome of an approval decision.

    入参：枚举成员值是 hook、daemon API 和 UI 共用的稳定字符串。
    返回：作为字符串枚举参与 Pydantic 校验、比较和序列化。
    错误处理：未知行为值由 Enum/Pydantic 校验为非法值并报告。
    副作用：无；定义枚举不访问网络、硬件、文件或全局运行状态。
    """

    ALLOW = "allow"
    DENY = "deny"


class DecisionStatus(StrEnum):
    """Represent the lifecycle state of one pending approval.

    入参：枚举成员值是 broker 内部和外部状态快照共用的稳定字符串。
    返回：作为字符串枚举参与 Pydantic 校验、比较和序列化。
    错误处理：未知状态值由 Enum/Pydantic 校验为非法值并报告。
    副作用：无；定义枚举不访问网络、硬件、文件或全局运行状态。
    """

    PENDING = "pending"
    RESOLVED = "resolved"
    TIMED_OUT = "timed_out"


class DecisionResult(BaseModel):
    """Carry the terminal behavior chosen for one decision.

    入参：`behavior` 是 allow/deny 结果；`message` 是可展示给等待方的说明，可为空。
    返回：frozen Pydantic model，可安全保存在 `PendingDecision.result` 中。
    错误处理：非法 behavior 由 Pydantic 校验异常报告。
    副作用：仅保存内存数据；实例化不访问网络、硬件或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    behavior: DecisionBehavior
    message: str = ""


class PendingDecision(BaseModel):
    """Describe one approval decision and its current broker snapshot.

    入参：字段覆盖 decision identity、agent/session/turn/tool 关联信息、创建与过期时间、
    默认行为、当前状态和终态结果；时间字段必须带 timezone。
    返回：frozen Pydantic model，可通过 `model_copy(update=...)` 派生新快照。
    错误处理：非法枚举、缺失字段或 naive datetime 由 Pydantic 报告。
    副作用：仅保存内存数据；实例化不访问网络、硬件或文件系统。
    """

    model_config = ConfigDict(frozen=True)

    decision_id: str
    agent_key: str
    session_id: str
    turn_id: str | None = None
    tool_name: str
    reason: str
    created_at: datetime
    expires_at: datetime
    default_behavior: DecisionBehavior = DecisionBehavior.DENY
    status: DecisionStatus = DecisionStatus.PENDING
    result: DecisionResult | None = None

    @field_validator("created_at", "expires_at")
    @classmethod
    def _ensure_timezone_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes so decision expiry never guesses local time.

        入参：`value` 是 Pydantic 已解析出的创建或过期时间字段。
        返回：原始 timezone-aware datetime，不做时区转换。
        错误处理：当 datetime 没有 tzinfo 或 utcoffset 为 None 时抛出 ValueError。
        副作用：无；只检查内存中的 datetime 字段。
        """

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime fields must be timezone-aware")
        return value


class DecisionBroker:
    """Coordinate in-process approval decisions with asyncio futures.

    入参：构造时不需要外部依赖；后续通过 `create` 注册 pending decision。
    返回：方法返回 frozen `PendingDecision` 或 `DecisionResult` 快照。
    错误处理：未知 decision id 抛 KeyError；无运行中的 asyncio loop 创建有效 decision
    时由 `asyncio.get_running_loop` 抛 RuntimeError；模型校验错误按 Pydantic 语义传播。
    副作用：修改本实例内存 dict，并完成对应 asyncio future；不访问外部 I/O。
    """

    def __init__(self) -> None:
        """Create an empty in-memory decision broker.

        入参：无。
        返回：无显式返回值；初始化后的 broker 可注册和等待 decision。
        错误处理：本方法不主动抛业务异常。
        副作用：仅初始化内存 dict，不访问网络、硬件或文件系统。
        """

        self._decisions: dict[str, PendingDecision] = {}
        self._futures: dict[str, asyncio.Future[DecisionResult]] = {}

    def create(
        self,
        *,
        agent_key: str,
        session_id: str,
        tool_name: str,
        reason: str,
        created_at: datetime,
        timeout: timedelta | float | int,
        turn_id: str | None = None,
        default_behavior: DecisionBehavior = DecisionBehavior.DENY,
    ) -> PendingDecision:
        """Register a pending decision and its asyncio future.

        入参：`agent_key` 是状态聚合 key；`session_id`、`turn_id` 和 `tool_name` 标记来源；
        `reason` 是展示给审批 UI 的原因；`created_at` 必须带 timezone；`timeout` 可为
        正数秒或正 `timedelta`；`default_behavior` 是 wait 超时时返回的行为。
        返回：status 为 pending 的 `PendingDecision` 快照，`expires_at = created_at + timeout`。
        错误处理：非正 timeout 抛 ValueError；naive 时间由 Pydantic ValidationError 报告；
        没有运行中的 asyncio loop 时抛 RuntimeError，避免 future 绑定到不明确 loop。
        副作用：写入本 broker 的内存 decision/future 表；不访问外部 I/O。
        """

        timeout_delta = _coerce_positive_timeout(timeout)
        decision = PendingDecision(
            decision_id=str(uuid4()),
            agent_key=agent_key,
            session_id=session_id,
            turn_id=turn_id,
            tool_name=tool_name,
            reason=reason,
            created_at=created_at,
            expires_at=created_at + timeout_delta,
            default_behavior=default_behavior,
        )
        loop = asyncio.get_running_loop()
        self._decisions[decision.decision_id] = decision
        self._futures[decision.decision_id] = loop.create_future()
        return decision

    def resolve(
        self,
        decision_id: str,
        behavior: DecisionBehavior,
        message: str = "",
    ) -> PendingDecision:
        """Resolve a pending decision or return its existing terminal snapshot.

        入参：`decision_id` 是 `create` 返回的 id；`behavior` 是新的 allow/deny 结果；
        `message` 是可选说明，默认空字符串。
        返回：resolved `PendingDecision`；若该 decision 已 resolved 或 timed_out，则幂等返回
        已保存的终态快照，不改写原结果。
        错误处理：未知 decision id 抛 KeyError；非法 behavior 由 Pydantic 校验报告。
        副作用：首次 resolve 会更新本 broker 内存状态并完成对应 asyncio future；不访问外部 I/O。
        """

        decision = self._require_decision(decision_id)
        if decision.status != DecisionStatus.PENDING:
            return decision

        result = DecisionResult(behavior=behavior, message=message)
        resolved = decision.model_copy(
            update={"status": DecisionStatus.RESOLVED, "result": result}
        )
        self._decisions[decision_id] = resolved
        self._complete_future(decision_id, result)
        return resolved

    async def wait(self, decision_id: str, timeout: float) -> DecisionResult:
        """Wait for a decision result or mark it timed out.

        入参：`decision_id` 是 `create` 返回的 id；`timeout` 是本次等待的最长秒数，交给
        `asyncio.wait_for` 解释。
        返回：resolved result；若等待超时，返回 default behavior 和固定 timeout message。
        错误处理：未知 decision id 抛 KeyError；非法 wait timeout 由 asyncio 报告或立即超时。
        副作用：超时时更新本 broker 内存状态为 timed_out 并完成 future；不访问外部 I/O。
        """

        decision = self._require_decision(decision_id)
        if decision.result is not None:
            return decision.result

        future = self._futures[decision_id]
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            current = self._require_decision(decision_id)
            if current.result is not None:
                return current.result
            result = DecisionResult(
                behavior=current.default_behavior,
                message=_DEFAULT_TIMEOUT_MESSAGE,
            )
            timed_out = current.model_copy(
                update={"status": DecisionStatus.TIMED_OUT, "result": result}
            )
            self._decisions[decision_id] = timed_out
            self._complete_future(decision_id, result)
            return result

    def pending(self) -> list[PendingDecision]:
        """Return pending decisions sorted by creation time.

        入参：无；读取当前 broker 内存状态。
        返回：仅包含 status 为 pending 的 `PendingDecision` 列表，按 `created_at` 升序。
        错误处理：不主动抛业务异常；内部数据若被外部破坏则按 Python 排序语义传播。
        副作用：无；只读取内存并创建列表，不访问外部 I/O。
        """

        return sorted(
            (
                decision
                for decision in self._decisions.values()
                if decision.status == DecisionStatus.PENDING
            ),
            key=lambda decision: decision.created_at,
        )

    def get(self, decision_id: str) -> PendingDecision | None:
        """Return one decision snapshot without changing broker state.

        入参：`decision_id` 是 `create` 返回的 id。
        返回：已存储的 `PendingDecision`；未知 id 返回 None。
        错误处理：本方法不主动抛业务异常。
        副作用：无；只读取内存状态，不访问外部 I/O。
        """

        return self._decisions.get(decision_id)

    def _require_decision(self, decision_id: str) -> PendingDecision:
        """Return a stored decision or raise KeyError.

        入参：`decision_id` 是调用方要读取的 decision id。
        返回：对应 `PendingDecision` 快照。
        错误处理：id 不存在时抛 KeyError，错误消息包含原 id 便于定位。
        副作用：无；只读取内存 dict。
        """

        try:
            return self._decisions[decision_id]
        except KeyError as error:
            raise KeyError(f"unknown decision_id: {decision_id}") from error

    def _complete_future(self, decision_id: str, result: DecisionResult) -> None:
        """Complete the stored future when it has not already completed.

        入参：`decision_id` 是 future 表 key；`result` 是要交付给 waiters 的终态结果。
        返回：无显式返回值。
        错误处理：未知 future 会按 dict 访问抛 KeyError；已完成 future 会被忽略。
        副作用：可能完成一个 asyncio future，唤醒等待中的 coroutine；不访问外部 I/O。
        """

        future = self._futures[decision_id]
        if not future.done():
            future.set_result(result)


def _coerce_positive_timeout(timeout: timedelta | float | int) -> timedelta:
    """Normalize timeout input into a positive timedelta.

    入参：`timeout` 可为 `timedelta`，也可为秒数 float/int。
    返回：等价的正 `timedelta`。
    错误处理：小于等于零时抛 ValueError；不支持的类型会在比较或 `timedelta` 构造时报错。
    副作用：无；只转换内存值，不访问外部 I/O。
    """

    timeout_delta = timeout if isinstance(timeout, timedelta) else timedelta(seconds=timeout)
    if timeout_delta <= timedelta(0):
        raise ValueError("timeout must be positive")
    return timeout_delta
