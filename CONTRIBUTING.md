# Contributing to Agent Deck

**[简体中文](CONTRIBUTING.zh-CN.md)**

Thank you for helping improve Agent Deck. The project is a local hardware console for AI agents. Its current focus is **macOS + MiraBox N4 Pro + Codex**, while the core layering must leave room for future hardware and agents.

This guide explains how to report issues, run the development environment, validate changes, and respect non-negotiable physical-hardware boundaries.

> **License status:** this checkout does not yet include a root `LICENSE`. This guide does not establish legal terms, grants, or assignments for external contributions. Maintainers should select and add a project license before formally accepting external contributions or publishing releases. Until then, contact a maintainer before contributing.

## Code of Conduct

- Keep discussion focused on reproducible technical facts and respect different devices, systems, and workflows.
- Do not submit API keys, tokens, complete prompts, private application paths, or other personal data in issues, pull requests, logs, or fixtures.
- Do not disclose security issues with exploitable details in a public issue. Contact a maintainer first until a formal security channel is published.

## Reporting an Issue

When possible, include:

- macOS version, Python version, `uv --version`, and Agent Deck commit/version.
- Device model, firmware information, and whether the official MiraBox/StreamDock application is running.
- Launch method: tmux, `run.sh`, or a direct daemon command.
- Redacted output of `uv run agent-deckctl doctor --json` and relevant logs.
- Expected behavior, actual behavior, and reproducible steps.

Do not publish `doctor` output unchanged when it can include serial numbers, paths, or account information. Redact first.

## Development Environment

### Dependencies

- macOS with Python 3.11+ is the currently verified development combination.
- Use [uv](https://docs.astral.sh/uv/) to manage the Python environment and dependencies.
- The recommended daemon launcher needs `tmux` and `lsof`.
- Work on usage trends needs [Bun](https://bun.sh/docs/installation) to run `bunx ccusage`.
- A physical N4 Pro is needed for real-device work; fake hardware is always available for development without one.

### Set Up

```bash
git clone https://github.com/breakstring/agent-deck.git
cd agent-deck
uv sync --all-groups
uv run pytest -q
```

Before changing anything, inspect the worktree so you do not overwrite somebody else's uncommitted changes:

```bash
git status --short --branch
```

Create a focused branch from current `main`, for example:

```bash
git switch -c feat/describe-the-change
```

If you need isolation from existing uncommitted work, use `.worktrees/` in the project root and ensure it remains ignored by Git.

## Architecture Boundaries

New behavior must respect this data and interaction boundary:

```text
Agent ingress -> NormalizedEvent -> AgentStateStore -> DeckMode/LayoutPlan
    -> HardwareSurface -> InteractionIntent/ActionExecutor
```

The current core modules are:

| Module | Responsibility |
| --- | --- |
| `src/agent_deck/core/events.py` | Normalized event model, payload redaction, and time validation. |
| `src/agent_deck/core/state.py` | In-memory reduction from events to agent state. |
| `src/agent_deck/core/decisions.py` | Permission decisions, timeouts, and default deny. |
| `src/agent_deck/core/modes.py` | Logical deck modes and selection state. |
| `src/agent_deck/rendering/layout.py` | Hardware-neutral layouts from state and decisions. |
| `src/agent_deck/hardware/fake.py` | Fake hardware with no real I/O, for tests and local development. |
| `src/agent_deck/hardware/streamdock_probe.py` | Read-only diagnostics for physical StreamDock devices. |
| `src/agent_deck/server/app.py` | Local FastAPI daemon and web configuration endpoints. |

Required rules:

- Hardware drivers must not parse Codex hook payloads, and Codex adapters must not operate a device directly.
- Hardware input must first become an `InteractionIntent` and then be handled by the action layer. It must not directly execute shell commands, AppleScript, or inject text into an agent.
- Use Pydantic models with clear immutable or copy semantics for data crossing modules.
- All timestamps must be timezone-aware `datetime` values.
- Redact payloads, logs, and errors. Never expose values such as `token`, `secret`, `authorization`, `api_key`, or `password`.
- High-risk actions must default to disabled or fail-closed, especially approvals, text input, and injection into an unknown foreground window.

## Physical Hardware Rules

A real N4 Pro is an explicit manual smoke environment and **must never** become a prerequisite for automated tests.

- Keep and maintain the fake adapter. Tests must not access a real HID device.
- The official MiraBox/StreamDock application can own the device. Before taking control, use `agent-deckctl doctor` for diagnostic hints or quit the official application.
- `streamdock_probe.py` may only briefly open/read/close a device and must not call SDK `device.init()`. `init()` can wake displays, change brightness, clear images, or refresh the device.
- Resolve macOS SDK loading issues by pointing `AGENT_DECK_STREAMDOCK_SDK_PATH` to the official `Python-SDK` or `src`; do not change diagnostics to initialize a device.
- When adding hardware, model capabilities first and add device-specific layouts second. Do not hard-code N4 Pro key/knob counts or coordinates into generic logic.

See [the usage guide](docs/guides/using-agent-deck.en.md#run-with-physical-hardware) for user-facing ownership steps. Developers should use the [Developer Q&A](docs/references/developer-q-and-a.md) for protocol, reconnection, false-success permissions, status fields, and real-hardware smoke evidence.

## Code and Documentation Conventions

- Keep changes focused. Do not add unrelated refactors, formatting churn, or dependency upgrades.
- New public models, functions, classes, and protocols should have Chinese docstrings describing semantics, constraints, return values, errors, and side effects.
- Leave concise comments for non-obvious I/O, caching, concurrency, or device state transitions. Explain constraints instead of narrating each line.
- Update the relevant README, usage guide, roadmap, or design document when changing public behavior, hardware capability models, DeckMode, hook installation, or security defaults.
- Use commit messages in the form `<type>(scope): <summary>`, for example `fix(n4pro): 修复旋钮灯光预览`. The summary should be Chinese, start with a verb, and have no final period.

## Validation Expectations

Run the smallest sufficient verification for the change. Normally include at least:

```bash
uv run pytest -q
git diff --check
```

Add verification appropriate to the touched area:

| Change type | Suggested verification |
| --- | --- |
| CLI or configuration | `uv run agent-deckctl version`, relevant subcommand `--help`, and configuration parsing tests. |
| Web UI | Start the daemon and check the key flow at `http://127.0.0.1:8765/`; when changing JavaScript, run `node --check src/agent_deck/web/app.js`. |
| Rendering or caches | Run targeted pytest and inspect generated-frame dimensions, state, and cache invalidation boundaries. |
| Codex integration | Start with read-only `uv run agent-deckctl codex-detect --enable-integration`; dry-run the installer before using `--apply` only when explicitly needed. |
| Physical hardware | Perform a separate explicit manual smoke after automated tests, recording device, firmware, and launcher. Do not attach HID to pytest. |

If a check cannot run, state why, what remains unverified, and how a maintainer can reproduce it in the pull request.

## Opening a Pull Request

Keep pull requests reviewable:

1. Solve one clear problem per PR and state the user-visible behavior and out-of-scope work.
2. Describe the architecture layer, data/cache, or hardware side effect touched by the change.
3. List commands run and their results. Mark physical-device verification separately.
4. Include screenshots or a short recording for UI changes, and a minimal example for protocol/configuration changes.
5. Exclude unrelated files, generated caches, personal configuration, secrets, and device serial numbers.

Maintainers will prioritize security boundaries, hardware side effects, compatibility, test coverage, and synchronized documentation.
