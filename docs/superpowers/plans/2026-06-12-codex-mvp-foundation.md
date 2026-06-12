# Codex MVP Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first runnable Agent Deck vertical slice for macOS + Codex using fake hardware: ingest events, reduce agent state, produce layout plans, handle pending decisions, expose daemon APIs, and provide CLI/hook entry points.

**Architecture:** This plan implements the hardware-independent core before touching the real N4 Pro driver or user Codex configuration. The daemon keeps an in-memory store, exposes local HTTP endpoints, and renders into a fake surface so reducer, decision, and layout behavior can be tested without hardware. Later plans can swap the fake surface for StreamDock N4 Pro and add installer/doctor behavior.

**Tech Stack:** Python 3.11+, uv, Typer, FastAPI, Uvicorn, Pydantic, HTTPX, pytest.

---

## Scope

This plan covers P1 foundation plus a minimal real-device probe:

- Python package skeleton.
- Core event/state/decision models.
- State reducer.
- DeckMode and hardware-neutral layout plan.
- Fake hardware surface and render sink.
- Local daemon HTTP API.
- Codex hook helper with fail-closed PermissionRequest behavior.
- `agent-deckctl simulate/status/decision` commands.
- Real N4 Pro diagnostic probe, used only for detection/open/init/close in this slice.

Current hardware evidence on `/Users/kenn/Projects/agent-deck`:

- `/Applications/StreamDock.app` is running.
- StreamDock plugin launch metadata lists `N4Pro`.
- With official StreamDock still running, the official Python SDK can enumerate one `StreamDockN4Pro`.
- The same SDK can `open()`, `init()`, read firmware `V4.N4 Pro.02.010`, read serial `8370D0780F17`, and `close()`.
- Therefore the official app is not an absolute blocker for open/init on this machine, but full rendering can still race with official scene redraws.

This plan does not cover:

- N4 Pro image generation.
- Codex config installer.
- macOS focus actions.
- LaunchAgent/menu bar packaging.

Those should follow as separate implementation plans after this vertical slice is working.

## File Structure

- Create `src/agent_deck/__init__.py`: package metadata.
- Create `src/agent_deck/core/events.py`: normalized event model and event constructors.
- Create `src/agent_deck/core/state.py`: agent state model and reducer.
- Create `src/agent_deck/core/decisions.py`: pending decision model and async broker.
- Create `src/agent_deck/core/modes.py`: deck mode, selected agent, and interaction intents.
- Create `src/agent_deck/rendering/layout.py`: hardware-neutral layout plan generation.
- Create `src/agent_deck/hardware/fake.py`: fake hardware surface for tests and local simulation.
- Create `src/agent_deck/hardware/streamdock_probe.py`: real N4 Pro diagnostic probe that opens and closes devices without rendering.
- Create `src/agent_deck/server/app.py`: FastAPI daemon application factory.
- Create `src/agent_deck/cli.py`: `agent-deckd`, `agent-deckctl`, and `agent-deck-codex-hook` command implementations.
- Create `tests/test_events.py`: event model tests.
- Create `tests/test_state.py`: reducer tests.
- Create `tests/test_decisions.py`: decision broker tests.
- Create `tests/test_layout.py`: layout tests.
- Create `tests/test_fake_hardware.py`: fake hardware tests.
- Create `tests/test_streamdock_probe.py`: probe tests with injected fake SDK manager.
- Create `tests/test_server.py`: HTTP API tests.
- Modify `pyproject.toml`: package mode, dependencies, console scripts, pytest config.

### Task 1: Project Package Skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `src/agent_deck/__init__.py`
- Create: `src/agent_deck/core/__init__.py`
- Create: `src/agent_deck/hardware/__init__.py`
- Create: `src/agent_deck/rendering/__init__.py`
- Create: `src/agent_deck/server/__init__.py`

- [ ] **Step 1: Update project metadata and dependencies**

Replace `pyproject.toml` with:

```toml
[project]
name = "agent-deck"
version = "0.1.0"
description = "Local hardware console bridge for AI agent status and actions."
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115.0",
  "httpx>=0.28.0",
  "pydantic>=2.7.0",
  "streamdock @ git+https://github.com/MiraboxSpace/StreamDock-Device-SDK.git#subdirectory=Python-SDK",
  "typer>=0.12.0",
  "uvicorn>=0.30.0",
]

[project.scripts]
agent-deckd = "agent_deck.cli:daemon_app"
agent-deckctl = "agent_deck.cli:ctl_app"
agent-deck-codex-hook = "agent_deck.cli:codex_hook_app"

[dependency-groups]
dev = [
  "pytest>=8.2.0",
  "pytest-asyncio>=0.23.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.uv]
package = true
```

- [ ] **Step 2: Create package directories and init files**

Create `src/agent_deck/__init__.py`:

```python
"""Agent Deck package metadata.

This package contains the local daemon, core state models, rendering plans, and
hardware adapters for Agent Deck. Importing the package must not open devices,
start network listeners, or mutate user configuration; side effects belong in
CLI entry points and daemon startup code.
"""

__version__ = "0.1.0"
```

Create each subpackage `__init__.py` with this content:

```python
"""Subpackage marker for Agent Deck implementation modules.

This file intentionally exposes no public API. Importing it must remain free of
network, filesystem, and hardware side effects so tests can import modules in
any order.
"""
```

- [ ] **Step 3: Lock dependencies**

Run:

```bash
uv lock
```

Expected: command exits 0 and updates `uv.lock`.

- [ ] **Step 4: Verify console script import path**

Run:

```bash
uv run python -c "import agent_deck; print(agent_deck.__version__)"
```

Expected: prints `0.1.0`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/agent_deck
git commit -m "chore: 建立 Agent Deck Python 包骨架"
```

### Task 2: Normalized Events

**Files:**
- Create: `src/agent_deck/core/events.py`
- Create: `tests/test_events.py`

- [ ] **Step 1: Write failing event tests**

Create `tests/test_events.py`:

```python
"""Tests for normalized event construction.

These tests keep raw Agent payload handling out of the reducer. They do not
exercise network ingress or Codex-specific parsing; those are adapter concerns.
"""

from datetime import UTC, datetime

from agent_deck.core.events import AgentSource, EventType, NormalizedEvent


def test_normalized_event_builds_stable_event_id() -> None:
    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="SessionStart",
        normalized_type=EventType.SESSION_STARTED,
        agent_id="codex",
        session_id="session-1",
        occurred_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
        summary="started",
    )

    assert event.event_id == "codex:SessionStart:session-1:2026-06-12T08:00:00+00:00"
    assert event.agent_key == "codex:session-1"
    assert event.payload == {}


def test_normalized_event_redacts_sensitive_payload_keys() -> None:
    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="PreToolUse",
        normalized_type=EventType.TOOL_STARTED,
        agent_id="codex",
        session_id="session-1",
        occurred_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
        payload={
            "authorization": "Bearer secret",
            "nested": {"api_key": "secret", "safe": "ok"},
        },
    )

    assert event.payload["authorization"] == "[REDACTED]"
    assert event.payload["nested"]["api_key"] == "[REDACTED]"
    assert event.payload["nested"]["safe"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_events.py -q
```

Expected: FAIL with `ModuleNotFoundError` or missing `events`.

- [ ] **Step 3: Implement events module**

Create `src/agent_deck/core/events.py`:

```python
"""Normalized event types shared by Agent Deck adapters.

This module converts adapter-level inputs into a small internal event shape. It
does not parse Codex, Claude, or hardware payloads directly, and it performs no
network or filesystem I/O. The only side effect is generating timestamps when a
caller omits `received_at`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field


SENSITIVE_KEY_PARTS = ("token", "secret", "authorization", "api_key", "apikey", "password")


class AgentSource(StrEnum):
    """Known agent event sources.

    Values are stable wire identifiers used in API payloads and event IDs.
    Unknown future sources should be added explicitly instead of accepted as
    arbitrary strings so downstream reducers can keep source-specific behavior
    auditable.
    """

    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    GENERIC = "generic"


class EventType(StrEnum):
    """Hardware-independent event taxonomy consumed by the reducer.

    The enum models observable agent lifecycle changes only. It intentionally
    excludes rendering and hardware input concepts, which belong in mode and
    intent modules.
    """

    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    APPROVAL_REQUESTED = "approval.requested"
    INPUT_REQUESTED = "input.requested"
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_COMPLETED = "subagent.completed"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


def redact_payload(value: Any) -> Any:
    """Return `value` with sensitive dictionary entries replaced.

    Args:
        value: JSON-like data from an adapter. Dictionaries and lists are walked
            recursively; primitive values are returned unchanged.

    Returns:
        A JSON-like value with keys containing token, secret, authorization,
        api_key, apikey, or password redacted.

    Errors:
        This function does not raise for unsupported values; non-container
        objects are returned unchanged. Callers should still avoid passing
        non-serializable values into API models.

    Side effects:
        None. A new structure is returned for containers.
    """

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redact_payload(item)
        return result
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


class NormalizedEvent(BaseModel):
    """Single normalized agent lifecycle event.

    Args:
        event_id: Stable identifier used to deduplicate repeated adapter
            deliveries.
        source: Agent family that produced the event.
        source_event_type: Original event name from the source integration.
        normalized_type: Internal lifecycle taxonomy value.
        agent_id: Adapter-level agent identifier.
        session_id: Source session identifier; required for the first slice.
        thread_id: Optional thread identifier when the source exposes one.
        turn_id: Optional turn identifier.
        cwd: Optional working directory, stored only when provided by source.
        title: Optional user-facing title.
        tool_name: Optional currently relevant tool.
        severity: Source severity string such as info, warning, or error.
        summary: Short sanitized text safe for hardware display.
        payload: Sanitized source details required for later decisions.
        occurred_at: Source event time.
        received_at: Local receive time.

    Returns:
        Pydantic model instances; validation errors are raised for invalid enum
        values or missing required fields.

    Side effects:
        None after construction. Use `build` to generate IDs and redaction.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    source: AgentSource
    source_event_type: str
    normalized_type: EventType
    agent_id: str
    session_id: str
    thread_id: str | None = None
    turn_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    tool_name: str | None = None
    severity: str = "info"
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    received_at: datetime

    @property
    def agent_key(self) -> str:
        """Return the stable UI key for this event's agent instance.

        Args:
            None.

        Returns:
            A source-qualified key using session granularity.

        Errors:
            None.

        Side effects:
            None.
        """

        return f"{self.source}:{self.session_id}"

    @classmethod
    def build(
        cls,
        *,
        source: AgentSource,
        source_event_type: str,
        normalized_type: EventType,
        agent_id: str,
        session_id: str,
        occurred_at: datetime,
        received_at: datetime | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        cwd: str | None = None,
        title: str | None = None,
        tool_name: str | None = None,
        severity: str = "info",
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Self:
        """Create a normalized event with deterministic ID and redacted payload.

        Args:
            source: Agent family that produced the event.
            source_event_type: Original source event name.
            normalized_type: Internal lifecycle event type.
            agent_id: Adapter-level agent identifier.
            session_id: Required session identifier.
            occurred_at: Source timestamp. Naive values are accepted but should
                be avoided by adapters.
            received_at: Optional local receive timestamp; defaults to UTC now.
            thread_id: Optional source thread identifier.
            turn_id: Optional source turn identifier.
            cwd: Optional source working directory.
            title: Optional display title.
            tool_name: Optional tool name.
            severity: Source severity string.
            summary: Sanitized display summary.
            payload: Optional JSON-like source details; sensitive keys are
                redacted recursively.

        Returns:
            A validated immutable `NormalizedEvent`.

        Errors:
            Pydantic validation errors are raised for invalid input types.

        Side effects:
            Reads current UTC time only when `received_at` is omitted.
        """

        event_id = f"{source}:{source_event_type}:{session_id}:{occurred_at.isoformat()}"
        return cls(
            event_id=event_id,
            source=source,
            source_event_type=source_event_type,
            normalized_type=normalized_type,
            agent_id=agent_id,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=cwd,
            title=title,
            tool_name=tool_name,
            severity=severity,
            summary=summary,
            payload=redact_payload(payload or {}),
            occurred_at=occurred_at,
            received_at=received_at or datetime.now(UTC),
        )
```

- [ ] **Step 4: Run event tests**

Run:

```bash
uv run pytest tests/test_events.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_deck/core/events.py tests/test_events.py
git commit -m "feat: 定义统一 Agent 事件模型"
```

### Task 3: State Reducer

**Files:**
- Create: `src/agent_deck/core/state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write failing reducer tests**

Create `tests/test_state.py`:

```python
"""Tests for reducing normalized events into agent state.

The reducer must consider active tools and pending approvals instead of blindly
mapping the last event to a status.
"""

from datetime import UTC, datetime, timedelta

from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.core.state import AgentStatus, AgentStateStore


BASE_TIME = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)


def make_event(event_type: EventType, *, seconds: int = 0, tool_name: str | None = None) -> NormalizedEvent:
    return NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type=event_type.value,
        normalized_type=event_type,
        agent_id="codex",
        session_id="session-1",
        occurred_at=BASE_TIME + timedelta(seconds=seconds),
        tool_name=tool_name,
        summary=event_type.value,
    )


def test_reducer_tracks_running_tool_then_completion() -> None:
    store = AgentStateStore()

    store.apply(make_event(EventType.SESSION_STARTED))
    store.apply(make_event(EventType.TURN_STARTED, seconds=1))
    store.apply(make_event(EventType.TOOL_STARTED, seconds=2, tool_name="shell"))

    state = store.get("codex:session-1")
    assert state is not None
    assert state.status == AgentStatus.RUNNING_TOOL
    assert state.active_tool == "shell"

    store.apply(make_event(EventType.TOOL_COMPLETED, seconds=3, tool_name="shell"))
    state = store.get("codex:session-1")
    assert state is not None
    assert state.status == AgentStatus.THINKING
    assert state.active_tool is None


def test_reducer_preserves_approval_needed_until_resolved_by_decision_layer() -> None:
    store = AgentStateStore()
    store.apply(make_event(EventType.SESSION_STARTED))
    store.apply(make_event(EventType.APPROVAL_REQUESTED, seconds=1, tool_name="shell"))
    store.apply(make_event(EventType.TOOL_COMPLETED, seconds=2, tool_name="shell"))

    state = store.get("codex:session-1")
    assert state is not None
    assert state.status == AgentStatus.APPROVAL_NEEDED
    assert state.pending_decision_count == 1


def test_offline_snapshot_marks_stale_agents_offline() -> None:
    store = AgentStateStore(idle_ttl=timedelta(seconds=10))
    store.apply(make_event(EventType.SESSION_STARTED))

    snapshot = store.snapshot(now=BASE_TIME + timedelta(seconds=20))

    assert snapshot[0].status == AgentStatus.OFFLINE
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_state.py -q
```

Expected: FAIL with missing `state` module.

- [ ] **Step 3: Implement state reducer**

Create `src/agent_deck/core/state.py`:

```python
"""Agent state reducer and in-memory state store.

This module turns normalized lifecycle events into display-ready agent states.
It performs no filesystem, network, or hardware I/O. The store is intentionally
in-memory for the first vertical slice; persistence belongs in a later plan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agent_deck.core.events import EventType, NormalizedEvent


class AgentStatus(StrEnum):
    """Display status values understood by layout and render layers.

    Values are deliberately coarse. Adapters should emit more detailed events,
    while reducers collapse those events into these states for hardware display.
    """

    OFFLINE = "offline"
    IDLE = "idle"
    THINKING = "thinking"
    RUNNING_TOOL = "running_tool"
    WAITING_USER = "waiting_user"
    APPROVAL_NEEDED = "approval_needed"
    ERROR = "error"
    COMPLETED_RECENTLY = "completed_recently"


class AgentState(BaseModel):
    """Current reduced state for one agent instance.

    Args:
        agent_key: Source-qualified stable key.
        source: Source identifier string.
        display_name: Human-readable label for hardware slots.
        cwd: Optional working directory.
        status: Reduced display status.
        status_since: Timestamp when current status began.
        last_event_at: Timestamp of the last applied event.
        last_summary: Sanitized summary from the latest relevant event.
        active_tool: Current tool name when one is active.
        pending_decision_count: Number of unresolved decisions known to reducer.
        slot_id: Optional assigned slot; layout can assign this later.
        focus_target: Optional focus target identifier.
        muted: Whether this agent should suppress non-critical display alerts.

    Returns:
        Immutable Pydantic model instances.

    Errors:
        Pydantic validation errors for invalid field values.

    Side effects:
        None.
    """

    model_config = ConfigDict(frozen=True)

    agent_key: str
    source: str
    display_name: str
    cwd: str | None = None
    status: AgentStatus
    status_since: datetime
    last_event_at: datetime
    last_summary: str = ""
    active_tool: str | None = None
    pending_decision_count: int = 0
    slot_id: int | None = None
    focus_target: str | None = None
    muted: bool = False


class AgentStateStore:
    """In-memory reducer store keyed by agent instance.

    Args:
        idle_ttl: Duration after which a non-ended agent is presented as
            offline in snapshots. The stored value is not mutated by snapshot.

    Returns:
        Store object with apply/get/snapshot operations.

    Errors:
        No custom exceptions are raised; invalid model input propagates
        Pydantic validation errors.

    Side effects:
        Mutates only this store's private dictionaries.
    """

    def __init__(self, *, idle_ttl: timedelta = timedelta(minutes=30)) -> None:
        self._idle_ttl = idle_ttl
        self._states: dict[str, AgentState] = {}
        self._active_tools: dict[str, set[str]] = {}

    def apply(self, event: NormalizedEvent) -> AgentState:
        """Apply one event and return the updated state.

        Args:
            event: A sanitized normalized event for one agent session.

        Returns:
            The updated immutable state.

        Errors:
            Propagates Pydantic validation errors if state construction fails.

        Side effects:
            Updates this store's in-memory state and active tool tracking.
        """

        previous = self._states.get(event.agent_key)
        current_status = previous.status if previous else AgentStatus.IDLE
        pending_count = previous.pending_decision_count if previous else 0
        active_tool = previous.active_tool if previous else None
        tool_set = self._active_tools.setdefault(event.agent_key, set())

        next_status = current_status

        if event.normalized_type == EventType.SESSION_STARTED:
            next_status = AgentStatus.IDLE
        elif event.normalized_type == EventType.SESSION_ENDED:
            next_status = AgentStatus.OFFLINE
            tool_set.clear()
            pending_count = 0
            active_tool = None
        elif event.normalized_type == EventType.TURN_STARTED:
            next_status = AgentStatus.THINKING
        elif event.normalized_type == EventType.TURN_COMPLETED:
            next_status = AgentStatus.COMPLETED_RECENTLY if pending_count == 0 else AgentStatus.APPROVAL_NEEDED
            active_tool = None if pending_count == 0 else active_tool
        elif event.normalized_type == EventType.TOOL_STARTED:
            if event.tool_name:
                tool_set.add(event.tool_name)
            active_tool = event.tool_name
            next_status = AgentStatus.RUNNING_TOOL
        elif event.normalized_type == EventType.TOOL_COMPLETED:
            if event.tool_name:
                tool_set.discard(event.tool_name)
            active_tool = next(iter(tool_set), None)
            next_status = AgentStatus.APPROVAL_NEEDED if pending_count else AgentStatus.THINKING
        elif event.normalized_type == EventType.TOOL_FAILED:
            if event.tool_name:
                tool_set.discard(event.tool_name)
            active_tool = None
            next_status = AgentStatus.ERROR
        elif event.normalized_type == EventType.APPROVAL_REQUESTED:
            pending_count += 1
            active_tool = event.tool_name or active_tool
            next_status = AgentStatus.APPROVAL_NEEDED
        elif event.normalized_type == EventType.INPUT_REQUESTED:
            next_status = AgentStatus.WAITING_USER
        elif event.normalized_type == EventType.ERROR:
            next_status = AgentStatus.ERROR
        elif event.normalized_type == EventType.HEARTBEAT:
            next_status = current_status

        status_since = event.occurred_at
        if previous and previous.status == next_status:
            status_since = previous.status_since

        state = AgentState(
            agent_key=event.agent_key,
            source=str(event.source),
            display_name=event.title or event.session_id,
            cwd=event.cwd or (previous.cwd if previous else None),
            status=next_status,
            status_since=status_since,
            last_event_at=event.occurred_at,
            last_summary=event.summary,
            active_tool=active_tool,
            pending_decision_count=pending_count,
            slot_id=previous.slot_id if previous else None,
            focus_target=previous.focus_target if previous else None,
            muted=previous.muted if previous else False,
        )
        self._states[event.agent_key] = state
        return state

    def mark_decision_resolved(self, agent_key: str, *, resolved_at: datetime | None = None) -> AgentState | None:
        """Decrease pending decision count for one agent.

        Args:
            agent_key: Source-qualified agent key.
            resolved_at: Optional timestamp for status transition. Defaults to
                UTC now when a transition is needed.

        Returns:
            Updated state, or None when the agent key is unknown.

        Errors:
            None beyond Pydantic validation failures.

        Side effects:
            Mutates this store's in-memory state for `agent_key`.
        """

        state = self._states.get(agent_key)
        if state is None:
            return None
        next_count = max(0, state.pending_decision_count - 1)
        next_status = AgentStatus.THINKING if next_count == 0 and state.status == AgentStatus.APPROVAL_NEEDED else state.status
        updated = state.model_copy(
            update={
                "pending_decision_count": next_count,
                "status": next_status,
                "status_since": resolved_at or datetime.now(UTC),
            }
        )
        self._states[agent_key] = updated
        return updated

    def get(self, agent_key: str) -> AgentState | None:
        """Return the current stored state for an agent.

        Args:
            agent_key: Source-qualified agent key.

        Returns:
            The immutable stored state or None.

        Errors:
            None.

        Side effects:
            None.
        """

        return self._states.get(agent_key)

    def snapshot(self, *, now: datetime | None = None) -> list[AgentState]:
        """Return display states with stale agents projected offline.

        Args:
            now: Optional comparison time. Defaults to UTC now.

        Returns:
            A list of immutable state models sorted by last event time.

        Errors:
            None.

        Side effects:
            None. Offline projection does not mutate the stored state.
        """

        current_time = now or datetime.now(UTC)
        states: list[AgentState] = []
        for state in self._states.values():
            if state.status != AgentStatus.OFFLINE and current_time - state.last_event_at > self._idle_ttl:
                states.append(
                    state.model_copy(
                        update={
                            "status": AgentStatus.OFFLINE,
                            "status_since": state.last_event_at + self._idle_ttl,
                        }
                    )
                )
            else:
                states.append(state)
        return sorted(states, key=lambda item: item.last_event_at, reverse=True)
```

- [ ] **Step 4: Run reducer tests**

Run:

```bash
uv run pytest tests/test_state.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_deck/core/state.py tests/test_state.py
git commit -m "feat: 实现 Agent 状态归约"
```

### Task 4: Decision Broker

**Files:**
- Create: `src/agent_deck/core/decisions.py`
- Create: `tests/test_decisions.py`

- [ ] **Step 1: Write failing decision tests**

Create `tests/test_decisions.py`:

```python
"""Tests for permission decision lifecycle.

Decision broker behavior must be deterministic because hook helpers rely on it
for fail-closed approval handling.
"""

from datetime import UTC, datetime, timedelta

import pytest

from agent_deck.core.decisions import DecisionBehavior, DecisionBroker, DecisionStatus


BASE_TIME = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)


async def test_decision_broker_allows_request() -> None:
    broker = DecisionBroker()
    pending = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="shell",
        reason="needs approval",
        created_at=BASE_TIME,
        timeout=timedelta(seconds=30),
    )

    assert pending.status == DecisionStatus.PENDING

    broker.resolve(pending.decision_id, DecisionBehavior.ALLOW, message="ok")
    result = await broker.wait(pending.decision_id, timeout=0.1)

    assert result.behavior == DecisionBehavior.ALLOW
    assert result.message == "ok"


async def test_decision_broker_timeout_returns_default_deny() -> None:
    broker = DecisionBroker()
    pending = broker.create(
        agent_key="codex:session-1",
        session_id="session-1",
        tool_name="shell",
        reason="needs approval",
        created_at=BASE_TIME,
        timeout=timedelta(milliseconds=1),
    )

    result = await broker.wait(pending.decision_id, timeout=0.01)

    assert result.behavior == DecisionBehavior.DENY
    assert result.message == "Timed out waiting for Agent Deck decision."


def test_decision_broker_rejects_unknown_decision() -> None:
    broker = DecisionBroker()

    with pytest.raises(KeyError):
        broker.resolve("missing", DecisionBehavior.ALLOW)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_decisions.py -q
```

Expected: FAIL with missing `decisions` module.

- [ ] **Step 3: Implement decision broker**

Create `src/agent_deck/core/decisions.py`:

```python
"""Async decision broker for agent approval requests.

The broker owns pending permission decisions created by local hook helpers. It
does not talk to Codex directly and does not perform network I/O; HTTP handlers
and hardware input routers call into it. Pending decisions default to deny on
timeout to keep approval hooks fail-closed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict


class DecisionBehavior(StrEnum):
    """Final behavior returned to an agent approval hook."""

    ALLOW = "allow"
    DENY = "deny"


class DecisionStatus(StrEnum):
    """Lifecycle status for a pending or resolved decision."""

    PENDING = "pending"
    RESOLVED = "resolved"
    TIMED_OUT = "timed_out"


class DecisionResult(BaseModel):
    """Resolved decision returned to hook helpers.

    Args:
        behavior: Allow or deny behavior.
        message: Optional user-facing reason.

    Returns:
        Immutable Pydantic model.

    Errors:
        Pydantic validation errors for invalid values.

    Side effects:
        None.
    """

    model_config = ConfigDict(frozen=True)

    behavior: DecisionBehavior
    message: str = ""


class PendingDecision(BaseModel):
    """Decision request waiting for user input or timeout.

    Args:
        decision_id: Stable ID for API and hardware intents.
        agent_key: Source-qualified agent key.
        session_id: Source session ID.
        turn_id: Optional source turn ID.
        tool_name: Tool needing approval.
        reason: Sanitized reason safe for display.
        created_at: Creation timestamp.
        expires_at: Timeout timestamp.
        default_behavior: Behavior used on timeout.
        status: Current decision lifecycle status.
        result: Optional resolved result.

    Returns:
        Immutable Pydantic model snapshots.

    Errors:
        Pydantic validation errors for invalid values.

    Side effects:
        None.
    """

    model_config = ConfigDict(frozen=True)

    decision_id: str
    agent_key: str
    session_id: str
    turn_id: str | None = None
    tool_name: str = ""
    reason: str = ""
    created_at: datetime
    expires_at: datetime
    default_behavior: DecisionBehavior = DecisionBehavior.DENY
    status: DecisionStatus = DecisionStatus.PENDING
    result: DecisionResult | None = None


class DecisionBroker:
    """In-memory async broker for pending decisions.

    Args:
        None.

    Returns:
        Broker with create, resolve, wait, and list operations.

    Errors:
        `resolve` and `wait` raise KeyError for unknown decision IDs.

    Side effects:
        Mutates private in-memory maps and completes asyncio futures.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingDecision] = {}
        self._futures: dict[str, asyncio.Future[DecisionResult]] = {}

    def create(
        self,
        *,
        agent_key: str,
        session_id: str,
        tool_name: str,
        reason: str,
        created_at: datetime,
        timeout: timedelta,
        turn_id: str | None = None,
        default_behavior: DecisionBehavior = DecisionBehavior.DENY,
    ) -> PendingDecision:
        """Create and register a pending decision.

        Args:
            agent_key: Source-qualified agent key.
            session_id: Source session ID.
            tool_name: Tool requiring approval.
            reason: Sanitized display reason.
            created_at: Creation timestamp.
            timeout: Time until default behavior is returned.
            turn_id: Optional source turn ID.
            default_behavior: Timeout behavior, defaulting to deny.

        Returns:
            Immutable pending decision snapshot.

        Errors:
            RuntimeError if no event loop is available.

        Side effects:
            Registers a future in this broker.
        """

        decision_id = str(uuid4())
        pending = PendingDecision(
            decision_id=decision_id,
            agent_key=agent_key,
            session_id=session_id,
            turn_id=turn_id,
            tool_name=tool_name,
            reason=reason,
            created_at=created_at,
            expires_at=created_at + timeout,
            default_behavior=default_behavior,
        )
        self._pending[decision_id] = pending
        self._futures[decision_id] = asyncio.get_running_loop().create_future()
        return pending

    def resolve(self, decision_id: str, behavior: DecisionBehavior, *, message: str = "") -> PendingDecision:
        """Resolve a pending decision.

        Args:
            decision_id: Decision ID returned by `create`.
            behavior: Final allow or deny behavior.
            message: Optional reason returned to the hook helper.

        Returns:
            Updated immutable decision snapshot.

        Errors:
            KeyError when `decision_id` is unknown.

        Side effects:
            Completes the waiting future if it is not already done.
        """

        if decision_id not in self._pending:
            raise KeyError(decision_id)
        result = DecisionResult(behavior=behavior, message=message)
        updated = self._pending[decision_id].model_copy(
            update={"status": DecisionStatus.RESOLVED, "result": result}
        )
        self._pending[decision_id] = updated
        future = self._futures[decision_id]
        if not future.done():
            future.set_result(result)
        return updated

    async def wait(self, decision_id: str, *, timeout: float) -> DecisionResult:
        """Wait for a decision or return its default timeout behavior.

        Args:
            decision_id: Decision ID returned by `create`.
            timeout: Maximum wait in seconds for this caller. This should be
                shorter than the upstream hook timeout.

        Returns:
            Decision result. Timeout produces a deny result by default.

        Errors:
            KeyError when `decision_id` is unknown.

        Side effects:
            Marks the decision timed out and completes its future when timeout
            occurs.
        """

        if decision_id not in self._pending:
            raise KeyError(decision_id)
        future = self._futures[decision_id]
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            pending = self._pending[decision_id]
            result = DecisionResult(
                behavior=pending.default_behavior,
                message="Timed out waiting for Agent Deck decision.",
            )
            updated = pending.model_copy(update={"status": DecisionStatus.TIMED_OUT, "result": result})
            self._pending[decision_id] = updated
            if not future.done():
                future.set_result(result)
            return result

    def pending(self) -> list[PendingDecision]:
        """Return all currently pending decisions.

        Args:
            None.

        Returns:
            Pending decision snapshots sorted by creation time.

        Errors:
            None.

        Side effects:
            None.
        """

        return sorted(
            [decision for decision in self._pending.values() if decision.status == DecisionStatus.PENDING],
            key=lambda decision: decision.created_at,
        )
```

- [ ] **Step 4: Run decision tests**

Run:

```bash
uv run pytest tests/test_decisions.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_deck/core/decisions.py tests/test_decisions.py
git commit -m "feat: 实现审批决策 broker"
```

### Task 5: Deck Mode and Layout Plan

**Files:**
- Create: `src/agent_deck/core/modes.py`
- Create: `src/agent_deck/rendering/layout.py`
- Create: `tests/test_layout.py`

- [ ] **Step 1: Write failing layout tests**

Create `tests/test_layout.py`:

```python
"""Tests for hardware-neutral layout plan generation.

Layout tests avoid image generation and StreamDock SDK calls so the first slice
can be verified without physical hardware.
"""

from datetime import UTC, datetime

from agent_deck.core.decisions import PendingDecision
from agent_deck.core.modes import DeckMode, DeckSelection
from agent_deck.core.state import AgentState, AgentStatus
from agent_deck.rendering.layout import build_layout_plan


NOW = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)


def make_state(key: str, status: AgentStatus) -> AgentState:
    return AgentState(
        agent_key=key,
        source="codex",
        display_name=key.split(":")[-1],
        status=status,
        status_since=NOW,
        last_event_at=NOW,
        last_summary=status.value,
    )


def test_overview_layout_assigns_agent_slots() -> None:
    plan = build_layout_plan(
        states=[make_state("codex:a", AgentStatus.IDLE), make_state("codex:b", AgentStatus.RUNNING_TOOL)],
        decisions=[],
        selection=DeckSelection(mode=DeckMode.OVERVIEW, selected_agent_key="codex:b"),
    )

    assert plan.mode == DeckMode.OVERVIEW
    assert plan.keys[0].agent_key == "codex:b"
    assert plan.keys[0].status == AgentStatus.RUNNING_TOOL
    assert plan.touchscreen.title == "b"


def test_decision_layout_overrides_context_keys() -> None:
    decision = PendingDecision(
        decision_id="decision-1",
        agent_key="codex:a",
        session_id="a",
        tool_name="shell",
        reason="Run shell command",
        created_at=NOW,
        expires_at=NOW,
    )

    plan = build_layout_plan(
        states=[make_state("codex:a", AgentStatus.APPROVAL_NEEDED)],
        decisions=[decision],
        selection=DeckSelection(mode=DeckMode.OVERVIEW, selected_agent_key="codex:a"),
    )

    assert plan.mode == DeckMode.DECISION
    assert plan.keys[10].intent == "approve_request"
    assert plan.keys[11].intent == "deny_request"
    assert plan.touchscreen.title == "Approval needed"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_layout.py -q
```

Expected: FAIL with missing `modes` or `layout`.

- [ ] **Step 3: Implement modes**

Create `src/agent_deck/core/modes.py`:

```python
"""Deck runtime modes and hardware interaction intents.

DeckMode represents Agent Deck's internal dynamic UI mode. It is not the same
as the official Stream Dock scene concept. This module performs no I/O and only
defines small immutable state models used by layout and input routing.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class DeckMode(StrEnum):
    """Internal runtime mode for the hardware surface."""

    OVERVIEW = "overview"
    AGENT_DETAIL = "agent_detail"
    DECISION = "decision"
    QUICK_PROMPT = "quick_prompt"
    SETTINGS = "settings"


class DeckSelection(BaseModel):
    """Current deck selection and mode.

    Args:
        mode: Internal runtime mode.
        selected_agent_key: Optional selected agent key.
        selected_decision_id: Optional selected decision ID.

    Returns:
        Immutable Pydantic model.

    Errors:
        Pydantic validation errors for invalid enum values.

    Side effects:
        None.
    """

    model_config = ConfigDict(frozen=True)

    mode: DeckMode = DeckMode.OVERVIEW
    selected_agent_key: str | None = None
    selected_decision_id: str | None = None
```

- [ ] **Step 4: Implement layout plan**

Create `src/agent_deck/rendering/layout.py`:

```python
"""Hardware-neutral layout plan generation.

The layout layer converts reduced agent state plus pending decisions into a
logical surface plan. It does not generate images and does not call hardware
SDKs; renderers adapt this plan to concrete devices later.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent_deck.core.decisions import PendingDecision
from agent_deck.core.modes import DeckMode, DeckSelection
from agent_deck.core.state import AgentState, AgentStatus


class KeyPlan(BaseModel):
    """One logical key on a hardware surface.

    Args:
        index: Zero-based key index.
        label: Short display label.
        status: Optional agent status color source.
        agent_key: Optional agent bound to this key.
        intent: Optional interaction intent emitted when pressed.
        decision_id: Optional decision bound to this key.

    Returns:
        Immutable Pydantic model.

    Errors:
        Pydantic validation errors for invalid values.

    Side effects:
        None.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    label: str
    status: AgentStatus | None = None
    agent_key: str | None = None
    intent: str | None = None
    decision_id: str | None = None


class TouchscreenPlan(BaseModel):
    """Logical touchscreen content for renderers.

    Args:
        title: Primary display title.
        lines: Sanitized display lines.
        selected_agent_key: Optional selected agent key.
        selected_decision_id: Optional selected decision ID.

    Returns:
        Immutable Pydantic model.

    Errors:
        Pydantic validation errors for invalid values.

    Side effects:
        None.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    lines: list[str] = Field(default_factory=list)
    selected_agent_key: str | None = None
    selected_decision_id: str | None = None


class LayoutPlan(BaseModel):
    """Complete logical plan for an N4 Pro-like surface.

    Args:
        mode: Effective mode after decision priority is applied.
        keys: Fifteen logical key plans for N4 Pro foundation layout.
        touchscreen: Touchscreen content.
        led_color: Aggregate LED color name.

    Returns:
        Immutable Pydantic model.

    Errors:
        Pydantic validation errors for malformed plans.

    Side effects:
        None.
    """

    model_config = ConfigDict(frozen=True)

    mode: DeckMode
    keys: list[KeyPlan]
    touchscreen: TouchscreenPlan
    led_color: str


def _sort_states(states: list[AgentState], selected_agent_key: str | None) -> list[AgentState]:
    """Sort states with selected and active agents first.

    Args:
        states: Current agent snapshots.
        selected_agent_key: Optional selected agent key.

    Returns:
        Sorted list without mutating input.

    Errors:
        None.

    Side effects:
        None.
    """

    priority = {
        AgentStatus.APPROVAL_NEEDED: 0,
        AgentStatus.WAITING_USER: 1,
        AgentStatus.RUNNING_TOOL: 2,
        AgentStatus.THINKING: 3,
        AgentStatus.ERROR: 4,
        AgentStatus.COMPLETED_RECENTLY: 5,
        AgentStatus.IDLE: 6,
        AgentStatus.OFFLINE: 7,
    }
    return sorted(
        states,
        key=lambda state: (
            0 if state.agent_key == selected_agent_key else 1,
            priority[state.status],
            -state.last_event_at.timestamp(),
        ),
    )


def _led_color(states: list[AgentState]) -> str:
    """Return aggregate LED color name for current states.

    Args:
        states: Current agent snapshots.

    Returns:
        `red`, `yellow`, `blue`, `green`, or `off`.

    Errors:
        None.

    Side effects:
        None.
    """

    statuses = {state.status for state in states}
    if AgentStatus.ERROR in statuses:
        return "red"
    if AgentStatus.APPROVAL_NEEDED in statuses or AgentStatus.WAITING_USER in statuses:
        return "yellow"
    if AgentStatus.RUNNING_TOOL in statuses or AgentStatus.THINKING in statuses:
        return "blue"
    if AgentStatus.IDLE in statuses or AgentStatus.COMPLETED_RECENTLY in statuses:
        return "green"
    return "off"


def build_layout_plan(
    *,
    states: list[AgentState],
    decisions: list[PendingDecision],
    selection: DeckSelection,
) -> LayoutPlan:
    """Build the effective layout plan for current runtime state.

    Args:
        states: Reduced agent states.
        decisions: Pending approval decisions.
        selection: Current deck mode and selection.

    Returns:
        A hardware-neutral plan with 15 logical key slots.

    Errors:
        Pydantic validation errors if invalid plan data is produced.

    Side effects:
        None.
    """

    effective_mode = DeckMode.DECISION if decisions else selection.mode
    sorted_states = _sort_states(states, selection.selected_agent_key)
    keys = [
        KeyPlan(index=index, label="", intent=None)
        for index in range(15)
    ]

    for index, state in enumerate(sorted_states[:10]):
        keys[index] = KeyPlan(
            index=index,
            label=state.display_name[:12],
            status=state.status,
            agent_key=state.agent_key,
            intent="select_agent",
        )

    selected_state = next((state for state in sorted_states if state.agent_key == selection.selected_agent_key), None)
    if selected_state is None and sorted_states:
        selected_state = sorted_states[0]

    if effective_mode == DeckMode.DECISION and decisions:
        decision = decisions[0]
        keys[10] = KeyPlan(index=10, label="ALLOW", intent="approve_request", decision_id=decision.decision_id)
        keys[11] = KeyPlan(index=11, label="DENY", intent="deny_request", decision_id=decision.decision_id)
        keys[12] = KeyPlan(index=12, label="DETAIL", intent="open_details", decision_id=decision.decision_id)
        keys[13] = KeyPlan(index=13, label="BACK", intent="back")
        touchscreen = TouchscreenPlan(
            title="Approval needed",
            lines=[decision.tool_name, decision.reason],
            selected_agent_key=decision.agent_key,
            selected_decision_id=decision.decision_id,
        )
    else:
        keys[10] = KeyPlan(index=10, label="FOCUS", intent="focus_agent", agent_key=selected_state.agent_key if selected_state else None)
        keys[11] = KeyPlan(index=11, label="MUTE", intent="toggle_mute", agent_key=selected_state.agent_key if selected_state else None)
        keys[12] = KeyPlan(index=12, label="PROMPT", intent="quick_prompt", agent_key=selected_state.agent_key if selected_state else None)
        keys[13] = KeyPlan(index=13, label="DETAIL", intent="open_details", agent_key=selected_state.agent_key if selected_state else None)
        keys[14] = KeyPlan(index=14, label="MODE", intent="cycle_mode")
        if selected_state:
            touchscreen = TouchscreenPlan(
                title=selected_state.display_name,
                lines=[selected_state.status.value, selected_state.last_summary],
                selected_agent_key=selected_state.agent_key,
            )
        else:
            touchscreen = TouchscreenPlan(title="Agent Deck", lines=["No agents online"])

    return LayoutPlan(
        mode=effective_mode,
        keys=keys,
        touchscreen=touchscreen,
        led_color=_led_color(states),
    )
```

- [ ] **Step 5: Run layout tests**

Run:

```bash
uv run pytest tests/test_layout.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/agent_deck/core/modes.py src/agent_deck/rendering/layout.py tests/test_layout.py
git commit -m "feat: 生成硬件无关布局计划"
```

### Task 6: Fake Hardware Surface

**Files:**
- Create: `src/agent_deck/hardware/fake.py`
- Create: `tests/test_fake_hardware.py`

- [ ] **Step 1: Write failing fake hardware tests**

Create `tests/test_fake_hardware.py`:

```python
"""Tests for fake hardware render and input behavior.

The fake surface is the default test double for daemon and layout work. It must
record renders and allow synthetic inputs without needing StreamDock hardware.
"""

from datetime import UTC, datetime

from agent_deck.core.modes import DeckMode
from agent_deck.hardware.fake import FakeHardwareSurface, HardwareInput
from agent_deck.rendering.layout import LayoutPlan, TouchscreenPlan


def test_fake_surface_records_rendered_plan() -> None:
    surface = FakeHardwareSurface()
    plan = LayoutPlan(
        mode=DeckMode.OVERVIEW,
        keys=[],
        touchscreen=TouchscreenPlan(title="Agent Deck"),
        led_color="green",
    )

    surface.render(plan)

    assert surface.last_plan == plan
    assert surface.render_count == 1


def test_fake_surface_queues_input() -> None:
    surface = FakeHardwareSurface()
    event = HardwareInput(kind="key", index=1, occurred_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC))

    surface.emit_input(event)

    assert surface.drain_inputs() == [event]
    assert surface.drain_inputs() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_fake_hardware.py -q
```

Expected: FAIL with missing `fake`.

- [ ] **Step 3: Implement fake hardware**

Create `src/agent_deck/hardware/fake.py`:

```python
"""Fake hardware surface for tests and local simulation.

This adapter records logical layout plans and queued inputs in memory. It does
not open HID devices, write files, or start background threads, making it safe
as the default daemon surface before real StreamDock integration exists.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent_deck.rendering.layout import LayoutPlan


class HardwareInput(BaseModel):
    """Synthetic hardware input event.

    Args:
        kind: Input family, currently key, knob, touch, or swipe.
        index: Optional key or knob index.
        value: Optional value such as rotation delta or touch coordinate text.
        occurred_at: Event timestamp.

    Returns:
        Immutable Pydantic model.

    Errors:
        Pydantic validation errors for invalid literal values.

    Side effects:
        None.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["key", "knob", "touch", "swipe"]
    index: int | None = None
    value: str | None = None
    occurred_at: datetime


class FakeHardwareSurface:
    """In-memory hardware surface for rendering and input simulation.

    Args:
        None.

    Returns:
        Surface object with render and input queue operations.

    Errors:
        None.

    Side effects:
        Mutates only this instance's in-memory render and input records.
    """

    def __init__(self) -> None:
        self.last_plan: LayoutPlan | None = None
        self.render_count = 0
        self._inputs: list[HardwareInput] = []

    def render(self, plan: LayoutPlan) -> None:
        """Record a rendered layout plan.

        Args:
            plan: Logical layout plan to record.

        Returns:
            None.

        Errors:
            None.

        Side effects:
            Updates `last_plan` and increments `render_count`.
        """

        self.last_plan = plan
        self.render_count += 1

    def emit_input(self, event: HardwareInput) -> None:
        """Queue a synthetic input event.

        Args:
            event: Synthetic hardware input event.

        Returns:
            None.

        Errors:
            None.

        Side effects:
            Appends the event to this surface's input queue.
        """

        self._inputs.append(event)

    def drain_inputs(self) -> list[HardwareInput]:
        """Return and clear queued synthetic inputs.

        Args:
            None.

        Returns:
            Inputs in FIFO order.

        Errors:
            None.

        Side effects:
            Clears the input queue.
        """

        inputs = list(self._inputs)
        self._inputs.clear()
        return inputs
```

- [ ] **Step 4: Run fake hardware tests**

Run:

```bash
uv run pytest tests/test_fake_hardware.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_deck/hardware/fake.py tests/test_fake_hardware.py
git commit -m "feat: 添加 fake 硬件表面"
```

### Task 7: Daemon HTTP API

**Files:**
- Create: `src/agent_deck/server/app.py`
- Create: `tests/test_server.py`

- [ ] **Step 1: Write failing server tests**

Create `tests/test_server.py`:

```python
"""Tests for the local Agent Deck daemon API.

The API tests use FastAPI's in-process test client and fake hardware, avoiding
real network sockets and physical devices.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.server.app import create_app


def test_event_endpoint_updates_status_and_layout() -> None:
    client = TestClient(create_app())
    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="SessionStart",
        normalized_type=EventType.SESSION_STARTED,
        agent_id="codex",
        session_id="session-1",
        occurred_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
    )

    response = client.post("/events", json=event.model_dump(mode="json"))

    assert response.status_code == 200
    assert response.json()["state"]["agent_key"] == "codex:session-1"

    status = client.get("/status")
    assert status.status_code == 200
    assert status.json()["agents"][0]["status"] == "idle"
    assert status.json()["layout"]["touchscreen"]["title"] == "session-1"


def test_decision_request_and_resolution() -> None:
    client = TestClient(create_app())

    created = client.post(
        "/decisions/request",
        json={
            "agent_key": "codex:session-1",
            "session_id": "session-1",
            "tool_name": "shell",
            "reason": "Run shell command",
            "timeout_seconds": 30,
        },
    )
    assert created.status_code == 200
    decision_id = created.json()["decision_id"]

    resolved = client.post(f"/decisions/{decision_id}/resolve", json={"behavior": "deny", "message": "no"})

    assert resolved.status_code == 200
    assert resolved.json()["result"]["behavior"] == "deny"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_server.py -q
```

Expected: FAIL with missing `server.app`.

- [ ] **Step 3: Implement daemon app**

Create `src/agent_deck/server/app.py`:

```python
"""FastAPI application for the local Agent Deck daemon.

The app exposes local-only event, status, and decision endpoints. It owns an
in-memory state store and fake hardware surface for the first vertical slice.
It does not patch user Codex config, open real hardware, or execute shell
actions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent_deck.core.decisions import DecisionBehavior, DecisionBroker, PendingDecision
from agent_deck.core.events import EventType, NormalizedEvent
from agent_deck.core.modes import DeckMode, DeckSelection
from agent_deck.core.state import AgentStateStore
from agent_deck.hardware.fake import FakeHardwareSurface
from agent_deck.rendering.layout import LayoutPlan, build_layout_plan


class DecisionRequest(BaseModel):
    """HTTP body for creating a pending decision.

    Args:
        agent_key: Source-qualified agent key.
        session_id: Source session ID.
        tool_name: Tool requiring approval.
        reason: Sanitized display reason.
        timeout_seconds: Hook wait timeout in seconds.
        turn_id: Optional source turn ID.

    Returns:
        Pydantic request model.

    Errors:
        FastAPI returns 422 for invalid request bodies.

    Side effects:
        None during validation.
    """

    model_config = ConfigDict(frozen=True)

    agent_key: str
    session_id: str
    tool_name: str = ""
    reason: str = ""
    timeout_seconds: float = Field(default=30, gt=0)
    turn_id: str | None = None


class DecisionResolveRequest(BaseModel):
    """HTTP body for resolving a pending decision.

    Args:
        behavior: Allow or deny behavior.
        message: Optional reason returned to hook helper.

    Returns:
        Pydantic request model.

    Errors:
        FastAPI returns 422 for invalid behavior values.

    Side effects:
        None during validation.
    """

    model_config = ConfigDict(frozen=True)

    behavior: DecisionBehavior
    message: str = ""


class AppState:
    """Container for daemon runtime dependencies.

    Args:
        None.

    Returns:
        Runtime state object with reducer, decision broker, fake surface, and
        current selection.

    Errors:
        None.

    Side effects:
        Allocates in-memory dependency instances only.
    """

    def __init__(self) -> None:
        self.store = AgentStateStore()
        self.decisions = DecisionBroker()
        self.surface = FakeHardwareSurface()
        self.selection = DeckSelection(mode=DeckMode.OVERVIEW)

    def render(self) -> LayoutPlan:
        """Render current state to the fake surface and return the plan.

        Args:
            None.

        Returns:
            Current logical layout plan.

        Errors:
            Pydantic validation errors if layout generation fails.

        Side effects:
            Records the layout on the fake hardware surface.
        """

        states = self.store.snapshot()
        if self.selection.selected_agent_key is None and states:
            self.selection = self.selection.model_copy(update={"selected_agent_key": states[0].agent_key})
        plan = build_layout_plan(
            states=states,
            decisions=self.decisions.pending(),
            selection=self.selection,
        )
        self.surface.render(plan)
        return plan


def create_app() -> FastAPI:
    """Create a local Agent Deck daemon app.

    Args:
        None.

    Returns:
        Configured FastAPI app with in-memory runtime dependencies.

    Errors:
        None during construction.

    Side effects:
        Allocates in-memory state only. No sockets are opened until served by
        Uvicorn or a test client.
    """

    runtime = AppState()
    app = FastAPI(title="Agent Deck Daemon")

    @app.post("/events")
    async def ingest_event(event: NormalizedEvent) -> dict[str, object]:
        """Ingest one normalized event and refresh layout.

        Args:
            event: Normalized event JSON body.

        Returns:
            Updated agent state and layout plan.

        Errors:
            FastAPI returns 422 for invalid event bodies.

        Side effects:
            Mutates in-memory reducer state and fake render surface.
        """

        state = runtime.store.apply(event)
        plan = runtime.render()
        return {"state": state.model_dump(mode="json"), "layout": plan.model_dump(mode="json")}

    @app.get("/status")
    async def status() -> dict[str, object]:
        """Return current daemon state and latest layout.

        Args:
            None.

        Returns:
            Agent snapshots, pending decisions, and layout plan.

        Errors:
            None.

        Side effects:
            Re-renders the fake surface so layout reflects stale/offline
            projection.
        """

        plan = runtime.render()
        return {
            "agents": [state.model_dump(mode="json") for state in runtime.store.snapshot()],
            "decisions": [decision.model_dump(mode="json") for decision in runtime.decisions.pending()],
            "layout": plan.model_dump(mode="json"),
            "render_count": runtime.surface.render_count,
        }

    @app.post("/decisions/request")
    async def request_decision(request: DecisionRequest) -> dict[str, object]:
        """Create a pending approval decision.

        Args:
            request: Decision request body.

        Returns:
            Pending decision snapshot.

        Errors:
            FastAPI returns 422 for invalid request bodies.

        Side effects:
            Registers an in-memory pending decision and refreshes layout.
        """

        pending = runtime.decisions.create(
            agent_key=request.agent_key,
            session_id=request.session_id,
            turn_id=request.turn_id,
            tool_name=request.tool_name,
            reason=request.reason,
            created_at=datetime.now(UTC),
            timeout=timedelta(seconds=request.timeout_seconds),
        )
        runtime.render()
        return pending.model_dump(mode="json")

    @app.post("/decisions/{decision_id}/resolve")
    async def resolve_decision(decision_id: str, request: DecisionResolveRequest) -> dict[str, object]:
        """Resolve a pending approval decision.

        Args:
            decision_id: Decision ID path parameter.
            request: Resolve request body.

        Returns:
            Updated decision snapshot.

        Errors:
            HTTP 404 when the decision ID is unknown.

        Side effects:
            Completes the in-memory decision future and refreshes layout.
        """

        try:
            updated = runtime.decisions.resolve(decision_id, request.behavior, message=request.message)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown decision") from exc
        runtime.store.mark_decision_resolved(updated.agent_key)
        runtime.render()
        return updated.model_dump(mode="json")

    @app.get("/decisions/{decision_id}/wait")
    async def wait_decision(decision_id: str, timeout_seconds: float = 25) -> dict[str, object]:
        """Wait for a decision result for hook helpers.

        Args:
            decision_id: Decision ID path parameter.
            timeout_seconds: Maximum wait in seconds for this HTTP request.

        Returns:
            Decision result.

        Errors:
            HTTP 404 when the decision ID is unknown.

        Side effects:
            May mark the decision timed out in the broker.
        """

        try:
            result = await runtime.decisions.wait(decision_id, timeout=timeout_seconds)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown decision") from exc
        return result.model_dump(mode="json")

    return app
```

- [ ] **Step 4: Run server tests**

Run:

```bash
uv run pytest tests/test_server.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_deck/server/app.py tests/test_server.py
git commit -m "feat: 添加本地 daemon API"
```

### Task 8: Real N4 Pro Probe

**Files:**
- Create: `src/agent_deck/hardware/streamdock_probe.py`
- Create: `tests/test_streamdock_probe.py`

- [ ] **Step 1: Write failing probe tests**

Create `tests/test_streamdock_probe.py`:

```python
"""Tests for real StreamDock diagnostic probing with fake SDK objects.

The probe module must be testable without physical hardware. Real N4 Pro access
is covered by manual smoke tests because it depends on the user's connected USB
device and official StreamDock app state.
"""

from agent_deck.hardware.streamdock_probe import ProbeResult, probe_streamdock_devices


class FakeDevice:
    def __init__(self, name: str, path: str, should_open: bool = True) -> None:
        self.name = name
        self.path = path
        self.should_open = should_open
        self.closed = False
        self.firmware_version = "V4.N4 Pro.02.010"
        self.serial_number = "SERIAL"

    def getPath(self) -> str:
        return self.path

    def open(self) -> None:
        if not self.should_open:
            raise RuntimeError("busy")

    def init(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeManager:
    def __init__(self, devices: list[FakeDevice]) -> None:
        self.devices = devices

    def enumerate(self) -> list[FakeDevice]:
        return self.devices


def test_probe_reports_openable_device() -> None:
    device = FakeDevice("N4Pro", "path-1")

    results = probe_streamdock_devices(manager=FakeManager([device]))

    assert results == [
        ProbeResult(
            device_type="FakeDevice",
            path="path-1",
            can_open=True,
            can_init=True,
            firmware_version="V4.N4 Pro.02.010",
            serial_number="SERIAL",
            error=None,
        )
    ]
    assert device.closed is True


def test_probe_reports_busy_device() -> None:
    device = FakeDevice("N4Pro", "path-1", should_open=False)

    results = probe_streamdock_devices(manager=FakeManager([device]))

    assert results[0].can_open is False
    assert results[0].can_init is False
    assert "busy" in str(results[0].error)
    assert device.closed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_streamdock_probe.py -q
```

Expected: FAIL with missing `streamdock_probe`.

- [ ] **Step 3: Implement probe module**

Create `src/agent_deck/hardware/streamdock_probe.py`:

```python
"""Real StreamDock diagnostic probe.

This module performs the smallest possible real-device check: enumerate devices,
open each device, initialize it, record firmware/serial metadata, and close it.
It must not render images, alter keys, change LED state, or modify official
StreamDock scenes. The only external side effects are temporary HID open/init
operations against connected devices.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


class StreamDockDeviceLike(Protocol):
    """Subset of official SDK device methods used by the diagnostic probe."""

    firmware_version: str
    serial_number: str

    def getPath(self) -> str:
        """Return SDK device path.

        Args:
            None.

        Returns:
            SDK-specific device path string.

        Errors:
            Propagates SDK errors.

        Side effects:
            None expected.
        """

    def open(self) -> None:
        """Open the HID transport for this device."""

    def init(self) -> None:
        """Initialize the StreamDock device after opening."""

    def close(self) -> None:
        """Close the HID transport for this device."""


class StreamDockManagerLike(Protocol):
    """Subset of official SDK DeviceManager used by the diagnostic probe."""

    def enumerate(self) -> list[StreamDockDeviceLike]:
        """Return currently connected StreamDock devices."""


class ProbeResult(BaseModel):
    """Diagnostic result for one StreamDock device.

    Args:
        device_type: Python class name reported by the SDK.
        path: SDK device path.
        can_open: Whether `open()` succeeded.
        can_init: Whether `init()` succeeded after open.
        firmware_version: Firmware version when readable.
        serial_number: Serial number when readable.
        error: Sanitized error text when probing failed.

    Returns:
        Immutable Pydantic model.

    Errors:
        Pydantic validation errors for invalid field values.

    Side effects:
        None.
    """

    model_config = ConfigDict(frozen=True)

    device_type: str
    path: str
    can_open: bool
    can_init: bool
    firmware_version: str | None = None
    serial_number: str | None = None
    error: str | None = None


def _load_default_manager() -> StreamDockManagerLike:
    """Load the official StreamDock manager lazily.

    Args:
        None.

    Returns:
        Official SDK `DeviceManager` instance.

    Errors:
        ImportError when `streamdock` SDK is not installed.

    Side effects:
        Imports SDK modules, which may load their transport dynamic library.
    """

    from StreamDock.DeviceManager import DeviceManager

    return DeviceManager()


def probe_streamdock_devices(manager: StreamDockManagerLike | None = None) -> list[ProbeResult]:
    """Probe connected StreamDock devices without changing displays.

    Args:
        manager: Optional SDK-compatible manager for tests. When omitted, the
            official SDK manager is imported and used.

    Returns:
        One `ProbeResult` per enumerated device.

    Errors:
        SDK import/enumeration errors propagate when no manager is injected.
        Per-device open/init errors are captured in result objects.

    Side effects:
        Opens, initializes, and closes each enumerated hardware device. It does
        not write images, LEDs, brightness, or scene configuration.
    """

    active_manager = manager or _load_default_manager()
    results: list[ProbeResult] = []
    for device in active_manager.enumerate():
        path = _safe_path(device)
        try:
            device.open()
            try:
                device.init()
                results.append(
                    ProbeResult(
                        device_type=type(device).__name__,
                        path=path,
                        can_open=True,
                        can_init=True,
                        firmware_version=getattr(device, "firmware_version", None),
                        serial_number=getattr(device, "serial_number", None),
                    )
                )
            except Exception as exc:
                results.append(
                    ProbeResult(
                        device_type=type(device).__name__,
                        path=path,
                        can_open=True,
                        can_init=False,
                        error=str(exc),
                    )
                )
        except Exception as exc:
            results.append(
                ProbeResult(
                    device_type=type(device).__name__,
                    path=path,
                    can_open=False,
                    can_init=False,
                    error=str(exc),
                )
            )
        finally:
            try:
                device.close()
            except Exception:
                pass
    return results


def _safe_path(device: Any) -> str:
    """Return a best-effort path for probe reporting.

    Args:
        device: SDK device-like object.

    Returns:
        Device path string or an empty string when unavailable.

    Errors:
        None; SDK exceptions are swallowed for diagnostic stability.

    Side effects:
        Calls `getPath()` or reads `path` attribute.
    """

    try:
        return str(device.getPath())
    except Exception:
        return str(getattr(device, "path", ""))
```

- [ ] **Step 4: Run probe tests**

Run:

```bash
uv run pytest tests/test_streamdock_probe.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Run real N4 Pro probe**

Run while the official StreamDock app is still open:

```bash
uv run python - <<'PY'
from agent_deck.hardware.streamdock_probe import probe_streamdock_devices
for result in probe_streamdock_devices():
    print(result.model_dump())
PY
```

Expected on the current machine:

```text
{'device_type': 'StreamDockN4Pro', 'path': 'DevSrvsID:4295156862', 'can_open': True, 'can_init': True, 'firmware_version': 'V4.N4 Pro.02.010', 'serial_number': '8370D0780F17', 'error': None}
```

If `can_open` is false or errors mention busy/access conflicts, quit `/Applications/StreamDock.app` and rerun the same command.

- [ ] **Step 6: Commit**

```bash
git add src/agent_deck/hardware/streamdock_probe.py tests/test_streamdock_probe.py pyproject.toml uv.lock
git commit -m "feat: 添加 N4 Pro 诊断探针"
```

### Task 9: CLI and Codex Hook Helper

**Files:**
- Create: `src/agent_deck/cli.py`

- [ ] **Step 1: Create CLI implementation**

Create `src/agent_deck/cli.py`:

```python
"""Command-line entry points for Agent Deck.

This module defines the daemon launcher, control CLI, and Codex hook helper.
CLI commands may perform network I/O against the local daemon and may read
stdin for hook payloads. They do not patch user configuration or open real
hardware in this foundation slice.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
import typer
import uvicorn

from agent_deck.core.events import AgentSource, EventType, NormalizedEvent
from agent_deck.server.app import create_app


daemon_app = typer.Typer(help="Run the Agent Deck local daemon.")
ctl_app = typer.Typer(help="Control and inspect Agent Deck.")
codex_hook_app = typer.Typer(help="Forward Codex hook and notify payloads to Agent Deck.")


DEFAULT_DAEMON_URL = "http://127.0.0.1:8765"


def _read_stdin_json() -> dict[str, Any]:
    """Read a JSON object from stdin.

    Args:
        None.

    Returns:
        Parsed JSON object.

    Errors:
        Raises typer.Exit after writing an error when stdin is empty or not a
        JSON object.

    Side effects:
        Reads stdin and writes stderr on failure.
    """

    raw = sys.stdin.read()
    if not raw.strip():
        typer.echo("Expected JSON payload on stdin.", err=True)
        raise typer.Exit(code=2)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"Invalid JSON payload: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not isinstance(payload, dict):
        typer.echo("Expected a JSON object payload.", err=True)
        raise typer.Exit(code=2)
    return payload


def _post_event(event: NormalizedEvent, *, daemon_url: str) -> None:
    """Post a normalized event to the local daemon.

    Args:
        event: Normalized event to send.
        daemon_url: Base daemon URL.

    Returns:
        None.

    Errors:
        Raises HTTPX exceptions for network failures.

    Side effects:
        Performs local HTTP POST.
    """

    with httpx.Client(timeout=2) as client:
        client.post(f"{daemon_url}/events", json=event.model_dump(mode="json")).raise_for_status()


@daemon_app.callback(invoke_without_command=True)
def run_daemon(
    host: Annotated[str, typer.Option(help="Daemon bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Daemon bind port.")] = 8765,
) -> None:
    """Run the local Agent Deck daemon.

    Args:
        host: Bind host. Defaults to loopback.
        port: Bind port. Defaults to 8765.

    Returns:
        None.

    Errors:
        Uvicorn may raise startup errors for occupied ports or invalid config.

    Side effects:
        Starts a local HTTP server and blocks until stopped.
    """

    uvicorn.run(create_app(), host=host, port=port)


@ctl_app.command()
def status(
    daemon_url: Annotated[str, typer.Option(help="Daemon base URL.")] = DEFAULT_DAEMON_URL,
) -> None:
    """Print daemon status JSON.

    Args:
        daemon_url: Base daemon URL.

    Returns:
        None.

    Errors:
        Exits non-zero when the daemon is unreachable or returns an error.

    Side effects:
        Performs local HTTP GET and writes stdout/stderr.
    """

    try:
        with httpx.Client(timeout=2) as client:
            response = client.get(f"{daemon_url}/status")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        typer.echo(f"agent-deckd unavailable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(response.json(), ensure_ascii=False, indent=2))


@ctl_app.command()
def simulate(
    session_id: Annotated[str, typer.Option(help="Synthetic Codex session ID.")] = "sim-session",
    event_type: Annotated[EventType, typer.Option(help="Normalized event type.")] = EventType.SESSION_STARTED,
    daemon_url: Annotated[str, typer.Option(help="Daemon base URL.")] = DEFAULT_DAEMON_URL,
) -> None:
    """Send a synthetic Codex event to the daemon.

    Args:
        session_id: Synthetic session ID.
        event_type: Normalized event type to send.
        daemon_url: Base daemon URL.

    Returns:
        None.

    Errors:
        Exits non-zero when the daemon is unreachable.

    Side effects:
        Performs local HTTP POST and writes stdout/stderr.
    """

    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type=event_type.value,
        normalized_type=event_type,
        agent_id="codex",
        session_id=session_id,
        occurred_at=datetime.now(UTC),
        summary=f"simulated {event_type.value}",
    )
    try:
        _post_event(event, daemon_url=daemon_url)
    except httpx.HTTPError as exc:
        typer.echo(f"agent-deckd unavailable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"sent {event_type.value} for {session_id}")


@ctl_app.command()
def resolve(
    decision_id: Annotated[str, typer.Argument(help="Decision ID.")],
    behavior: Annotated[str, typer.Argument(help="allow or deny.")],
    daemon_url: Annotated[str, typer.Option(help="Daemon base URL.")] = DEFAULT_DAEMON_URL,
) -> None:
    """Resolve a pending decision from the CLI.

    Args:
        decision_id: Pending decision ID.
        behavior: `allow` or `deny`.
        daemon_url: Base daemon URL.

    Returns:
        None.

    Errors:
        Exits non-zero for invalid behavior, unreachable daemon, or unknown
        decision ID.

    Side effects:
        Performs local HTTP POST and writes stdout/stderr.
    """

    if behavior not in {"allow", "deny"}:
        typer.echo("behavior must be allow or deny", err=True)
        raise typer.Exit(code=2)
    try:
        with httpx.Client(timeout=2) as client:
            response = client.post(
                f"{daemon_url}/decisions/{decision_id}/resolve",
                json={"behavior": behavior, "message": f"Resolved via agent-deckctl: {behavior}"},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        typer.echo(f"failed to resolve decision: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(response.json(), ensure_ascii=False, indent=2))


@codex_hook_app.command("notify")
def codex_notify(
    daemon_url: Annotated[str, typer.Option(help="Daemon base URL.")] = DEFAULT_DAEMON_URL,
) -> None:
    """Forward a Codex notify payload as a turn completed event.

    Args:
        daemon_url: Base daemon URL.

    Returns:
        None.

    Errors:
        Notify is best-effort; daemon failures are written to stderr and exit 0
        so Codex completion notification is not blocked.

    Side effects:
        Reads stdin, performs local HTTP POST when possible, writes stderr on
        failure.
    """

    payload = _read_stdin_json()
    session_id = str(payload.get("session_id") or payload.get("thread_id") or "codex-notify")
    event = NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type="notify",
        normalized_type=EventType.TURN_COMPLETED,
        agent_id="codex",
        session_id=session_id,
        occurred_at=datetime.now(UTC),
        summary=str(payload.get("summary") or "Codex turn completed"),
        payload=payload,
    )
    try:
        _post_event(event, daemon_url=daemon_url)
    except httpx.HTTPError as exc:
        typer.echo(f"agent-deck notify forwarding failed: {exc}", err=True)


@codex_hook_app.command("permission-request")
def codex_permission_request(
    daemon_url: Annotated[str, typer.Option(help="Daemon base URL.")] = DEFAULT_DAEMON_URL,
    timeout_seconds: Annotated[float, typer.Option(help="Hook wait timeout in seconds.")] = 25,
) -> None:
    """Handle a Codex PermissionRequest hook with fail-closed behavior.

    Args:
        daemon_url: Base daemon URL.
        timeout_seconds: Maximum daemon wait. Keep this below Codex hook timeout.

    Returns:
        Codex hook decision JSON on stdout.

    Errors:
        Daemon failures are converted to deny output instead of process errors.

    Side effects:
        Reads stdin, performs local HTTP requests, writes stdout/stderr.
    """

    payload = _read_stdin_json()
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "codex-session")
    agent_key = f"codex:{session_id}"
    deny_output = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "Denied by Agent Deck because the local decision service was unavailable.",
            },
        }
    }
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            created = client.post(
                f"{daemon_url}/decisions/request",
                json={
                    "agent_key": agent_key,
                    "session_id": session_id,
                    "turn_id": payload.get("turn_id") or payload.get("turnId"),
                    "tool_name": str(payload.get("tool_name") or payload.get("toolName") or ""),
                    "reason": str(payload.get("reason") or payload.get("summary") or "Codex permission requested"),
                    "timeout_seconds": timeout_seconds,
                },
            )
            created.raise_for_status()
            decision_id = created.json()["decision_id"]
            waited = client.get(
                f"{daemon_url}/decisions/{decision_id}/wait",
                params={"timeout_seconds": timeout_seconds},
            )
            waited.raise_for_status()
            result = waited.json()
    except httpx.HTTPError as exc:
        typer.echo(f"agent-deck permission request failed closed: {exc}", err=True)
        typer.echo(json.dumps(deny_output, ensure_ascii=False))
        return

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": result["behavior"],
                "message": result.get("message", ""),
            },
        }
    }
    typer.echo(json.dumps(output, ensure_ascii=False))
```

- [ ] **Step 2: Verify CLI imports**

Run:

```bash
uv run python -c "from agent_deck.cli import daemon_app, ctl_app, codex_hook_app; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 3: Run all tests**

Run:

```bash
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/agent_deck/cli.py
git commit -m "feat: 添加 daemon、控制 CLI 与 Codex hook helper"
```

### Task 10: End-to-End Smoke Test

**Files:**
- No new files.

- [ ] **Step 1: Start daemon manually**

Run in one terminal:

```bash
uv run agent-deckd --host 127.0.0.1 --port 8765
```

Expected: Uvicorn starts and listens on `http://127.0.0.1:8765`.

- [ ] **Step 2: Send synthetic session event**

Run in another terminal:

```bash
uv run agent-deckctl simulate --session-id demo --event-type session.started
```

Expected:

```text
sent session.started for demo
```

- [ ] **Step 3: Inspect status**

Run:

```bash
uv run agent-deckctl status
```

Expected JSON includes:

```json
{
  "agents": [
    {
      "agent_key": "codex:demo",
      "status": "idle"
    }
  ],
  "layout": {
    "touchscreen": {
      "title": "demo"
    }
  }
}
```

- [ ] **Step 4: Stop daemon and verify final status**

Stop the daemon with Ctrl-C, then run:

```bash
git status --short --branch
```

Expected: clean branch after all commits.

## Self-Review

- Spec coverage: This plan implements the MVP foundation for event ingestion, state reduction, DeckMode layout, fake hardware, local daemon, decisions, and hook helper. It intentionally leaves real N4 Pro SDK, image generation, installer, and macOS focus actions for later plans because those require hardware and user-environment choices.
- Placeholder scan: No TBD/TODO/fill-in placeholders are used. Every code step contains concrete file content.
- Type consistency: `NormalizedEvent`, `AgentState`, `PendingDecision`, `DeckSelection`, and `LayoutPlan` names are consistent across tests and implementation.
