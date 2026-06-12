"""Tests for the Agent Deck in-memory decision broker.

These tests define the Task 4 approval decision contract only. They do not
start servers, touch hardware, read user files, or persist state; their only
side effects are local asyncio scheduling and pytest assertion reporting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_deck.core.decisions import (
    DecisionBehavior,
    DecisionBroker,
    DecisionStatus,
)

BASE_TIME = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)


def test_create_without_running_loop_succeeds() -> None:
    """Verify create can be called from a synchronous no-loop context.

    入参：无；测试内在普通同步 pytest test 中调用 `DecisionBroker.create`。
    返回：无返回值；断言通过代表 broker 不把 creation 绑定到当前 asyncio loop。
    错误处理：若 create 依赖 `asyncio.get_running_loop` 并抛 RuntimeError，会由 pytest 报告。
    副作用：仅修改测试内 broker 的内存状态，不创建 asyncio future 或外部资源。
    """

    broker = DecisionBroker()

    decision = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="shell",
        reason="needs approval",
        created_at=BASE_TIME,
        timeout=timedelta(seconds=30),
    )

    assert decision.agent_key == "codex:session-1"
    assert decision.status == DecisionStatus.PENDING
    assert [pending.decision_id for pending in broker.pending()] == [
        decision.decision_id
    ]


async def test_create_accepts_first_six_arguments_positionally() -> None:
    """Verify create supports the positional API shape from the task spec.

    入参：无；测试内按 spec 位置参数顺序调用 `DecisionBroker.create`。
    返回：无返回值；断言通过代表前六个业务参数可位置传入且字段正确保存。
    错误处理：签名不兼容、模型校验失败或字段不匹配会由 pytest 报告。
    副作用：仅修改测试内 broker 的内存状态，不绑定 asyncio event loop。
    """

    broker = DecisionBroker()

    decision = broker.create(
        "codex:session-1",
        "session-1",
        "shell",
        "reason",
        BASE_TIME,
        timedelta(seconds=30),
    )

    assert decision.agent_key == "codex:session-1"
    assert decision.session_id == "session-1"
    assert decision.tool_name == "shell"
    assert decision.reason == "reason"
    assert decision.created_at == BASE_TIME
    assert decision.expires_at == BASE_TIME + timedelta(seconds=30)
    assert decision.default_behavior == DecisionBehavior.DENY
    assert decision.status == DecisionStatus.PENDING


async def test_create_resolve_allow_and_wait_returns_allow() -> None:
    """Verify a pending decision can be resolved to allow and awaited.

    入参：无；测试内创建 `DecisionBroker` 并注册 shell 工具决策。
    返回：无返回值；断言通过代表 create/resolve/wait 的基本成功路径成立。
    错误处理：未知 id、模型校验失败或 wait 未收到 allow 会由 pytest 报告。
    副作用：仅修改测试内 broker 的内存状态并通过 async wait 读取 result。
    """

    broker = DecisionBroker()
    created_at = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)
    decision = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_name="shell",
        reason="needs approval",
        created_at=created_at,
        timeout=timedelta(seconds=30),
    )

    resolved = broker.resolve(
        decision.decision_id,
        DecisionBehavior.ALLOW,
        message="approved",
    )
    result = await broker.wait(decision.decision_id, timeout=0.01)

    assert decision.status == DecisionStatus.PENDING
    assert decision.expires_at == created_at + timedelta(seconds=30)
    assert resolved.status == DecisionStatus.RESOLVED
    assert resolved.result is not None
    assert result.behavior == DecisionBehavior.ALLOW
    assert result.message == "approved"


async def test_wait_timeout_returns_default_deny_and_removes_from_pending() -> None:
    """Verify wait timeout records a timed-out deny result.

    入参：无；测试内创建默认 deny 的 pending decision，并用很短 wait timeout 等待。
    返回：无返回值；断言通过代表超时会返回默认行为并从 pending 列表消失。
    错误处理：wait 未超时、结果不是 deny 或状态未改为 timed_out 会由 pytest 报告。
    副作用：仅修改测试内 broker 的内存状态，并写入 timeout 结果。
    """

    broker = DecisionBroker()
    decision = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="shell",
        reason="needs approval",
        created_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
        timeout=timedelta(seconds=30),
    )

    result = await broker.wait(decision.decision_id, timeout=0.01)
    timed_out = broker.resolve(decision.decision_id, DecisionBehavior.ALLOW)

    assert result.behavior == DecisionBehavior.DENY
    assert result.message == "Timed out waiting for Agent Deck decision."
    assert timed_out.status == DecisionStatus.TIMED_OUT
    assert timed_out.result == result
    assert broker.pending() == []


async def test_cancelled_waiter_does_not_cancel_shared_decision() -> None:
    """Verify cancelling one waiter keeps the decision pending and resolvable.

    入参：无；测试内创建 decision，启动长时间 wait task，随后取消该 task。
    返回：无返回值；断言通过代表取消外部 waiter 不会取消 broker 共享 decision。
    错误处理：若 task 未抛 CancelledError、decision 丢失 pending 或后续 wait 不能返回 allow，
    会由 pytest 报告。
    副作用：仅修改测试内 broker 的内存状态并取消一个测试创建的 asyncio task。
    """

    broker = DecisionBroker()
    decision = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="shell",
        reason="needs approval",
        created_at=BASE_TIME,
        timeout=timedelta(seconds=30),
    )
    task = asyncio.create_task(broker.wait(decision.decision_id, timeout=10))
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert [pending.decision_id for pending in broker.pending()] == [
        decision.decision_id
    ]

    broker.resolve(decision.decision_id, DecisionBehavior.ALLOW, "approved")
    result = await broker.wait(decision.decision_id, timeout=0.1)

    assert result.behavior == DecisionBehavior.ALLOW
    assert result.message == "approved"


async def test_unknown_resolve_and_wait_raise_key_error() -> None:
    """Verify unknown decision ids are rejected consistently.

    入参：无；测试内对空 broker 调用 resolve 和 wait。
    返回：无返回值；断言通过代表未知 decision id 不会被静默创建或默认拒绝。
    错误处理：若未抛 KeyError 或抛出其他异常，会由 pytest 报告。
    副作用：仅读取测试内空 broker，不访问外部 I/O。
    """

    broker = DecisionBroker()

    with pytest.raises(KeyError):
        broker.resolve("missing", DecisionBehavior.ALLOW)

    with pytest.raises(KeyError):
        await broker.wait("missing", timeout=0.01)


async def test_pending_sorts_by_created_at_and_filters_resolved_and_timed_out() -> None:
    """Verify pending snapshots are ordered and exclude terminal decisions.

    入参：无；测试内创建三个不同创建时间的 decision，并分别 resolve/timeout 两个。
    返回：无返回值；断言通过代表 `pending()` 只返回仍 pending 的决策且按创建时间升序。
    错误处理：排序错误、终态未过滤或 wait 超时失败会由 pytest 报告。
    副作用：仅修改测试内 broker 的内存状态并通过 async wait 写入 timeout 结果。
    """

    broker = DecisionBroker()
    latest = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="shell",
        reason="latest",
        created_at=datetime(2026, 6, 12, 8, 2, tzinfo=UTC),
        timeout=timedelta(seconds=30),
    )
    earliest = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="python",
        reason="earliest",
        created_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
        timeout=timedelta(seconds=30),
    )
    middle = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="git",
        reason="middle",
        created_at=datetime(2026, 6, 12, 8, 1, tzinfo=UTC),
        timeout=timedelta(seconds=30),
    )

    broker.resolve(earliest.decision_id, DecisionBehavior.ALLOW)
    await broker.wait(middle.decision_id, timeout=0.01)

    assert [decision.decision_id for decision in broker.pending()] == [
        latest.decision_id
    ]


async def test_resolve_after_timeout_does_not_replace_timed_out_result() -> None:
    """Verify terminal timed-out decisions cannot later become allowed.

    入参：无；测试内让 decision 先通过 wait 超时，再调用 resolve allow。
    返回：无返回值；断言通过代表 timeout 后 resolve 幂等返回既有 timed_out snapshot。
    错误处理：若后续 resolve 改写状态、行为或 message，会由 pytest 报告。
    副作用：仅修改测试内 broker 的内存状态并通过 async wait 写入 timeout 结果。
    """

    broker = DecisionBroker()
    decision = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="shell",
        reason="needs approval",
        created_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
        timeout=timedelta(seconds=30),
    )
    timed_out = await broker.wait(decision.decision_id, timeout=0.01)

    resolved = broker.resolve(decision.decision_id, DecisionBehavior.ALLOW, "late allow")

    assert timed_out.behavior == DecisionBehavior.DENY
    assert resolved.status == DecisionStatus.TIMED_OUT
    assert resolved.result == timed_out
    assert resolved.result is not None
    assert resolved.result.behavior == DecisionBehavior.DENY
    assert resolved.result.message == "Timed out waiting for Agent Deck decision."


def test_create_rejects_naive_created_at() -> None:
    """Verify decision creation rejects naive timestamps.

    入参：无；测试内传入没有 timezone 的 `created_at`。
    返回：无返回值；断言通过代表 broker 不会猜测本地时区。
    错误处理：若创建成功或抛出非 Pydantic ValidationError，会由 pytest 报告。
    副作用：仅尝试创建内存模型，不访问网络、硬件或文件系统。
    """

    broker = DecisionBroker()

    with pytest.raises(ValidationError):
        broker.create(
            agent_key="codex:session-1",
            session_id="session-1",
            tool_name="shell",
            reason="needs approval",
            created_at=datetime(2026, 6, 12, 8, 0),
            timeout=timedelta(seconds=30),
        )


def test_create_rejects_non_positive_timeout() -> None:
    """Verify decision creation requires a positive timeout.

    入参：无；测试内传入零秒 timeout。
    返回：无返回值；断言通过代表无效 timeout 会在写入 broker 状态前被拒绝。
    错误处理：若创建成功或抛出非 ValueError，会由 pytest 报告。
    副作用：仅尝试创建内存模型，不访问网络、硬件或文件系统。
    """

    broker = DecisionBroker()

    with pytest.raises(ValueError):
        broker.create(
            agent_key="codex:session-1",
            session_id="session-1",
            tool_name="shell",
            reason="needs approval",
            created_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            timeout=timedelta(seconds=0),
        )


async def test_duplicate_resolve_returns_existing_resolved_snapshot() -> None:
    """Verify resolving an already resolved decision is idempotent.

    入参：无；测试内先 resolve allow，再尝试 resolve deny。
    返回：无返回值；断言通过代表重复 resolve 返回首次 resolved snapshot。
    错误处理：若第二次 resolve 改写结果或抛出异常，会由 pytest 报告。
    副作用：仅修改测试内 broker 的内存状态并通知 condition waiters。
    """

    broker = DecisionBroker()
    decision = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="shell",
        reason="needs approval",
        created_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
        timeout=timedelta(seconds=30),
    )

    first = broker.resolve(decision.decision_id, DecisionBehavior.ALLOW, "approved")
    second = broker.resolve(decision.decision_id, DecisionBehavior.DENY, "late deny")

    assert second == first
    assert second.result is not None
    assert second.result.behavior == DecisionBehavior.ALLOW
    assert second.result.message == "approved"
