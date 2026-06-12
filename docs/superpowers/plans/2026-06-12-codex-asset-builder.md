# Codex Asset Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Codex visual asset pre-renderer that turns `codex.gif` plus state overlays into cached button frames and a preview image.

**Architecture:** Keep runtime state mapping separate from pixel rendering. `VisualIconSpec` will describe both the source asset and generated variant id, while `agent_deck.rendering.asset_builder` will use Pillow to create frame sequences under a caller-provided output directory.

**Tech Stack:** Python 3.12, Pillow, Pydantic v2, Typer, pytest, uv.

---

### Task 1: Clarify VisualIconSpec Asset Semantics

**Files:**
- Modify: `src/agent_deck/rendering/visuals.py`
- Modify: `tests/test_visuals.py`

- [x] **Step 1: Write failing tests**

Assert that non-offline statuses use `base_asset_id="assets/codex/codex.gif"` and generated `asset_id` values such as `generated/codex/working`.

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_visuals.py -q
```

Expected: failure because `base_asset_id` does not exist yet.

- [x] **Step 3: Implement minimal model update**

Add `base_asset_id` and `variant_id` to `VisualIconSpec`, then update resolver mappings.

- [x] **Step 4: Run tests to verify pass**

Run:

```bash
uv run pytest tests/test_visuals.py -q
```

Expected: visual tests pass.

### Task 2: Add Codex Asset Builder

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/agent_deck/rendering/asset_builder.py`
- Create: `tests/test_asset_builder.py`

- [x] **Step 1: Add Pillow dependency**

Run `uv add "Pillow>=10.0.0"` so GIF/PNG processing is managed by uv.

- [x] **Step 2: Write failing asset builder tests**

Create temporary source GIF/PNG files and assert that generated variants include `idle`, `working`, `needs_user`, `error`, `completed`, and `offline`, with correct frame size and preview output.

- [x] **Step 3: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_asset_builder.py -q
```

Expected: import failure because `asset_builder.py` does not exist yet.

- [x] **Step 4: Implement minimal asset builder**

Use Pillow to load GIF frames, apply deterministic overlays, write PNG frame sequences, write `offline.png`, and create a preview sheet.

- [x] **Step 5: Run tests to verify pass**

Run:

```bash
uv run pytest tests/test_asset_builder.py -q
```

Expected: asset builder tests pass.

### Task 3: Add CLI Command

**Files:**
- Modify: `src/agent_deck/cli.py`
- Modify: `tests/test_cli.py`

- [x] **Step 1: Write failing CLI test**

Patch the builder and assert `agent-deckctl generate-codex-assets --output-dir <tmp>` calls it with default source assets.

- [x] **Step 2: Run test to verify failure**

Run:

```bash
uv run pytest tests/test_cli.py::test_generate_codex_assets_uses_default_sources -q
```

Expected: command not found.

- [x] **Step 3: Implement CLI command**

Add a `generate-codex-assets` command that accepts output dir, key width/height, and max frames.

- [x] **Step 4: Run targeted tests**

Run:

```bash
uv run pytest tests/test_cli.py::test_generate_codex_assets_uses_default_sources tests/test_asset_builder.py tests/test_visuals.py -q
```

Expected: all targeted tests pass.

### Task 4: Generate Local Preview And Verify

**Files:**
- No committed generated assets.

- [x] **Step 1: Run full test suite**

Run:

```bash
uv run pytest -q
```

Expected: all tests pass with only the known Starlette/httpx deprecation warning.

- [x] **Step 2: Generate preview assets**

Run:

```bash
uv run agent-deckctl generate-codex-assets --output-dir tmp/codex-visual-preview
```

Expected: preview and frame files are written under ignored `tmp/`.

- [x] **Step 3: Inspect status**

Run:

```bash
git status --short --branch
```

Expected: only source, test, lockfile, and plan changes are tracked; `tmp/` output remains ignored.
