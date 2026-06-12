"""In-memory approval decision broker for Agent Deck.

This module owns Task 4's async decision lifecycle: callers register pending
approval decisions, resolve them from local UI/API code, and await the result
from hook or daemon code. It intentionally does not perform network, file,
database, hardware, layout, server, or CLI work; all state is process-local and
non-durable.
"""

from __future__ import annotations

import asyncio
import threading
import time
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
    """Coordinate in-process approval decisions across threads and loops.

    入参：构造时不需要外部依赖；后续通过 `create` 注册 pending decision。
    返回：方法返回 frozen `PendingDecision` 或 `DecisionResult` 快照。
    错误处理：未知 decision id 抛 KeyError；模型校验错误按 Pydantic 语义传播。
    副作用：在 `threading.Condition` 保护下修改本实例内存 dict，并通知等待线程；
    不绑定 asyncio event loop，也不访问外部 I/O。
    """

    def __init__(self) -> None:
        """Create an empty in-memory decision broker.

        入参：无。
        返回：无显式返回值；初始化后的 broker 可注册和等待 decision。
        错误处理：本方法不主动抛业务异常。
        副作用：仅初始化内存 dict 和线程条件变量，不访问网络、硬件或文件系统。
        """

        self._decisions: dict[str, PendingDecision] = {}
        self._condition = threading.Condition()

    def create(
        self,
        agent_key: str,
        session_id: str,
        tool_name: str,
        reason: str,
        created_at: datetime,
        timeout: timedelta | float | int,
        turn_id: str | None = None,
        default_behavior: DecisionBehavior = DecisionBehavior.DENY,
    ) -> PendingDecision:
        """Register a pending decision without binding it to an asyncio loop.

        入参：`agent_key` 是状态聚合 key；`session_id`、`turn_id` 和 `tool_name` 标记来源；
        `reason` 是展示给审批 UI 的原因；`created_at` 必须带 timezone；`timeout` 可为
        正数秒或正 `timedelta`；`default_behavior` 是 wait 超时时返回的行为。
        返回：status 为 pending 的 `PendingDecision` 快照，`expires_at = created_at + timeout`。
        错误处理：非正 timeout 抛 ValueError；naive 时间由 Pydantic ValidationError 报告。
        副作用：在 condition 锁内写入本 broker 的内存 decision 表；不创建 asyncio future，
        不访问外部 I/O。
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
        with self._condition:
            self._decisions[decision.decision_id] = decision
            self._condition.notify_all()
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
        副作用：首次 resolve 会在 condition 锁内更新本 broker 内存状态并通知 waiters；
        不访问外部 I/O。
        """

        with self._condition:
            decision = self._require_decision_locked(decision_id)
            if decision.status != DecisionStatus.PENDING:
                return decision

            result = DecisionResult(behavior=behavior, message=message)
            resolved = decision.model_copy(
                update={"status": DecisionStatus.RESOLVED, "result": result}
            )
            self._decisions[decision_id] = resolved
            self._condition.notify_all()
            return resolved

    async def wait(self, decision_id: str, timeout: float) -> DecisionResult:
        """Wait for a decision result or mark it timed out without loop affinity.

        入参：`decision_id` 是 `create` 返回的 id；`timeout` 是本次等待的最长秒数。
        返回：resolved result；若等待超时，返回 default behavior 和固定 timeout message。
        错误处理：未知 decision id 抛 KeyError；非正 timeout 会按立即超时处理。
        副作用：通过 `asyncio.to_thread` 在 condition 上等待；超时时在同一锁内仅一次更新
        timed_out 状态并通知其他 waiters；取消当前 coroutine 不会取消共享 decision 状态。
        """

        return await asyncio.to_thread(self._wait_blocking, decision_id, timeout)

    def pending(self) -> list[PendingDecision]:
        """Return pending decisions sorted by creation time.

        入参：无；读取当前 broker 内存状态。
        返回：仅包含 status 为 pending 的 `PendingDecision` 列表，按 `created_at` 升序。
        错误处理：不主动抛业务异常；内部数据若被外部破坏则按 Python 排序语义传播。
        副作用：无；在 condition 锁内只读取内存并创建列表，不访问外部 I/O。
        """

        with self._condition:
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
        副作用：无；在 condition 锁内只读取内存状态，不访问外部 I/O。
        """

        with self._condition:
            return self._decisions.get(decision_id)

    def _wait_blocking(self, decision_id: str, timeout: float) -> DecisionResult:
        """Block on the condition until a decision resolves or times out.

        入参：`decision_id` 是调用方要等待的 decision id；`timeout` 是最长等待秒数。
        返回：已保存的 terminal result；超时时返回并保存默认 result。
        错误处理：未知 id 抛 KeyError；无法转为 float 的 timeout 按 Python 异常传播。
        副作用：可能在 condition 锁内把 pending decision 标记为 timed_out 并通知 waiters。
        """

        deadline = time.monotonic() + float(timeout)
        with self._condition:
            decision = self._require_decision_locked(decision_id)
            if decision.result is not None:
                return decision.result

            while decision.result is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._timeout_decision_locked(decision_id)
                self._condition.wait(timeout=remaining)
                decision = self._require_decision_locked(decision_id)

            return decision.result

    def _timeout_decision_locked(self, decision_id: str) -> DecisionResult:
        """Mark a pending decision timed out while holding the condition lock.

        入参：`decision_id` 是要 timeout 的 decision id；调用方必须已经持有 condition 锁。
        返回：existing result 或新建的 default timeout result。
        错误处理：未知 id 抛 KeyError。
        副作用：首次 timeout 会更新 `_decisions` 并通知所有 condition waiters。
        """

        current = self._require_decision_locked(decision_id)
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
        self._condition.notify_all()
        return result

    def _require_decision_locked(self, decision_id: str) -> PendingDecision:
        """Return a stored decision while the condition lock is held.

        入参：`decision_id` 是调用方要读取的 decision id；调用方必须持有 condition 锁。
        返回：对应 `PendingDecision` 快照。
        错误处理：id 不存在时抛 KeyError，错误消息包含原 id 便于定位。
        副作用：无；只读取内存 dict。
        """

        try:
            return self._decisions[decision_id]
        except KeyError as error:
            raise KeyError(f"unknown decision_id: {decision_id}") from error


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
