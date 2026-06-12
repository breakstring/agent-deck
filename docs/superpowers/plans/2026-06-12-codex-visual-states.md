# Codex Visual States Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a renderer-facing visual state abstraction that maps the existing eight Agent statuses into compact Codex button visuals.

**Architecture:** Keep `AgentStatus` as the internal lifecycle contract and add a separate visual resolver in `agent_deck.rendering.visuals`. `LayoutPlan` keys will carry a `VisualIconSpec` so future real N4 Pro renderers can choose GIFs, generated frames, overlays, and animations without changing the state reducer.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, uv.

---

### Task 1: Add Visual State Resolver

**Files:**
- Create: `src/agent_deck/rendering/visuals.py`
- Create: `tests/test_visuals.py`

- [x] **Step 1: Write failing tests**

Add tests that assert these mappings:

```python
AgentStatus.APPROVAL_NEEDED -> needs_user, asset codex-needs-user
AgentStatus.WAITING_USER -> needs_user, asset codex-needs-user
AgentStatus.RUNNING_TOOL -> working, asset codex-working
AgentStatus.THINKING -> working, asset codex-working
AgentStatus.IDLE -> idle, asset assets/codex/codex.gif
AgentStatus.OFFLINE -> offline, asset assets/codex/codex.png
AgentStatus.ERROR -> error, asset codex-error
AgentStatus.COMPLETED_RECENTLY -> idle, asset assets/codex/codex.gif, badge success
```

- [x] **Step 2: Verify tests fail**

Run:

```bash
uv run pytest tests/test_visuals.py -q
```

Expected: import failure because `agent_deck.rendering.visuals` does not exist yet.

- [x] **Step 3: Implement minimal resolver**

Create `VisualAgentState`, `VisualAnimation`, `VisualBadge`, `VisualIconSpec`, and `resolve_visual_icon_spec`.

- [x] **Step 4: Verify tests pass**

Run:

```bash
uv run pytest tests/test_visuals.py -q
```

Expected: all visual resolver tests pass.

### Task 2: Attach Visual Specs To Layout Keys

**Files:**
- Modify: `src/agent_deck/rendering/layout.py`
- Modify: `tests/test_layout.py`

- [x] **Step 1: Write failing layout tests**

Add coverage showing agent slot `KeyPlan.visual` is populated, while action keys without bound agent state keep `visual=None`.

- [x] **Step 2: Verify layout tests fail**

Run:

```bash
uv run pytest tests/test_layout.py -q
```

Expected: assertion failure because `KeyPlan.visual` is missing.

- [x] **Step 3: Update layout model**

Import `VisualIconSpec` and `resolve_visual_icon_spec`, add `visual` to `KeyPlan`, and populate it in `_build_base_keys`.

- [x] **Step 4: Verify targeted tests pass**

Run:

```bash
uv run pytest tests/test_visuals.py tests/test_layout.py -q
```

Expected: visual and layout tests pass.

### Task 3: Full Verification

**Files:**
- No production changes.

- [x] **Step 1: Run full test suite**

Run:

```bash
uv run pytest -q
```

Expected: all tests pass with only the known Starlette/httpx deprecation warning.

- [x] **Step 2: Inspect git diff**

Run:

```bash
git diff --stat
git status --short --branch
```

Expected: only the plan, visual resolver, layout, and tests are changed in the feature worktree.
