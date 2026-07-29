# Agent Deck

**[简体中文](README.md)**

> Bring local AI-agent status, subscription quota, task pets, and controlled actions to the physical surface of a MiraBox N4 Pro.

Agent Deck is a local hardware-console bridge for AI agents. It maps agent state, usage, task pets, and explicitly configured actions to MiraBox hardware, with a browser-based local configuration UI. Version **`0.2.0`** supports **macOS + MiraBox N4 Pro + Codex**; other operating systems, hardware models, and agent platforms do not yet carry compatibility guarantees.

## Product Video

https://github.com/user-attachments/assets/f0283ca5-a5c8-419b-9235-635c6c82fd5f

visit the
[intro page](https://breakstring.github.io/agent-deck/) 

## Why Agent Deck

When several AI agents are running at once, their states, pending input, and usage information are scattered across terminals, desktop apps, and windows. Agent Deck reduces those local signals into a unified state, then projects it onto a hardware surface with keys, a touch display, and knobs. You can see state at a glance, switch panels or focus context, and keep high-risk actions disabled by default.

The core boundary remains:

```text
Agent ingress -> NormalizedEvent -> AgentStateStore -> DeckMode/LayoutPlan
             -> HardwareSurface -> InteractionIntent/ActionExecutor
```

Codex and the N4 Pro are the currently verified combination; the core architecture leaves room for other agents and hardware.

## Web Configuration

![Agent Deck local web configuration: N4 Pro preview, key purpose, and save-and-apply control](assets/agent-deck/config.png)

The local configuration page uses the N4 Pro preview as its workspace. Select a key or knob to edit it; changes appear in the GUI preview first and reach a connected device only after you choose **Save and Apply**. You can configure:

- Ten LCD main keys for local apps, URLs, keyboard shortcuts, subscription/quota, token/cost usage,
  agent-status entry points, and a Codex pet. ChatGPT/Codex launcher keys can also show a temporary
  task-state pet overlay.
- A bottom logical panel for the brand card, Codex quota, usage trends, and a multi-task PETS colony.
  Usage trends come from local caches so a panel switch does not block hardware interaction.
- Four knob rotation actions, such as changing a panel or time period, adjusting system input/output volume, display brightness, and console-screen brightness.
- A shared knob-ring color and optional breathing effect, both previewable before saving.

A keyboard shortcut may be one physical key, a chord with Command/Control/Option/Shift, or an ordered
sequence of up to 16 steps. One recording session can capture multiple steps; **Stop and Apply** invokes
the same full-device save action as the header **Save and Apply** button. The UI can also add modifier-only
steps manually, edit inter-step delays, and use either an auto-generated or uploaded default icon. The Web
auto preview is the exact PNG produced by the hardware renderer, so it cannot drift from the N4 Pro image.
Execution pins the
frontmost application at the start of the sequence. Only one sequence runs at a time; another press
returns busy instead of waiting in a queue. The first use requires an explicit macOS Accessibility request
from the configuration UI. The compact permission row reveals the actual requester and system-setting
actions only on hover, focus, or **Details** click; the browser itself does not need Accessibility access.

Without a connected device, the configuration page and core service still run with fake hardware for exploration, development, and troubleshooting.

## Codex Pet System

Agent Deck reuses existing Codex/ChatGPT pet assets at runtime and does not maintain or redistribute a
second asset library. The local pet follows Codex's global selection. Built-in assets needed by PETS actors
are read on demand from an installed ChatGPT/Codex application, while custom pets are loaded safely from
the local Codex directory.

The three presentation surfaces are independent:

- **Codex pet key**: dedicates one main key to global Codex activity. Pressing it performs no action.
- **App-key task overlay**: optionally covers a ChatGPT/Codex launcher icon while a task is running,
  waiting, failing, or showing completion feedback. The original icon returns when idle, and the key still
  only opens or focuses the application.
- **PETS virtual panel**: represents top-level local and enabled SSH Remote tasks that are active or showing
  completion feedback as independent pets sharing the N4 Pro touch bar. Remote actors use stable, muted
  halos to identify their execution host.

Select the PETS touch bar in the Web device preview to choose whether remote pets follow the local pet,
read the remote Codex selection, or receive a stable built-in assignment, and to select slow, medium, or
fast patrol speed. Pets remain presentation-only: they never approve requests, execute tasks, or replace
Agent status keys. Child/subagents, version 2 gaze rows, and mouse tracking are outside the current scope.
See the [Codex Pet section of the usage guide](docs/guides/using-agent-deck.en.md#61-codex-pet) for
configuration, remote-asset safety, and diagnostics.

## Current Support

| Area | Status |
| --- | --- |
| Project version | `0.2.0` |
| Operating system | macOS is the verified target. Windows and Linux are not formally supported yet. |
| Physical hardware | MiraBox N4 Pro. The architecture leaves room for other StreamDock/MiraBox models, but they are not released as supported targets. |
| Agent | Local Codex App/CLI state, ChatGPT SSH remote-task observation, quota, hooks, and pet presentation. |
| Python | Python 3.11 or later. |
| Usage trends | Optional: Bun's `bunx` and `ccusage`. Without them, other features still run, but token/cost trends are unavailable. |

## Quick Start

Read the full [usage guide](docs/guides/using-agent-deck.en.md) for installation, hardware ownership, and troubleshooting. This is the shortest path to start the local UI with fake hardware:

```bash
git clone https://github.com/breakstring/agent-deck.git
cd agent-deck
uv sync --all-groups

# Read local environment and device hints; this does not write to a device.
uv run agent-deckctl doctor

# Start the local service without taking ownership of a physical device.
scripts/agent-deckd-tmux.sh start --disable-hardware-renderer
```

Then open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). To stop the service:

```bash
scripts/agent-deckd-tmux.sh stop
```

To take ownership of an N4 Pro, first quit the official MiraBox/StreamDock application and inspect device hints with `doctor`. If the SDK dynamic library is not compatible on macOS, point `AGENT_DECK_STREAMDOCK_SDK_PATH` to the official Python SDK. See [Run with Physical Hardware](docs/guides/using-agent-deck.en.md#run-with-physical-hardware).

## Codex Integration and Safety Boundaries

Agent Deck can read local Codex state, quota, and `ccusage` data, and can optionally install Codex hook integration. The installer always starts with a dry run; it writes local Codex configuration only when you explicitly pass `--apply`:

```bash
# Inspect the current Codex environment and generate integration guidance.
uv run agent-deckctl codex-detect --enable-integration

# Preview the notify and hook changes.
uv run agent-deckctl codex-install

# Apply only after reviewing the preview.
uv run agent-deckctl codex-install --apply
```

The default approval mode keeps Codex's native approval UI and does not automatically move approval control to hardware. Keyboard shortcuts are limited to physical keys, chords, and timing; they do not expose text, mouse, shell, media-key, or Fn injection. Text input and approval/deny actions remain high-risk capabilities that require explicit configuration. If the daemon is unavailable, a response is invalid, or a wait times out, the approval path follows a fail-closed policy.

## Common Commands and Process Management

| Command | Purpose |
| --- | --- |
| `uv run agent-deckd` | Starts the core daemon that receives agent events and drives hardware rendering. |
| `uv run agent-deckctl` | Runs environment diagnosis, runtime inspection, hardware checks, and event simulation. |
| `uv run agent-deck-codex-hook` | Bridge helper invoked by Codex notify/command hooks. |
| `scripts/agent-deckd-tmux.sh [start\|stop\|status\|logs\|attach\|restart]` | Manages the persistent service through tmux; recommended for regular use. |
| `./run.sh [start\|stop\|status\|logs\|restart]` | Standard background-process helper for machines without tmux. |

For foreground debugging, run:

```bash
uv run agent-deckd --host 127.0.0.1 --port 8765
```

## Documentation

- [Usage guide (English)](docs/guides/using-agent-deck.en.md): installation, startup, configuration, hardware, Codex integration, and troubleshooting.
- [使用指南（中文）](docs/guides/using-agent-deck.zh-CN.md)
- [Contributing guide (English)](CONTRIBUTING.md)
- [贡献指南（中文）](CONTRIBUTING.zh-CN.md)
- [Project roadmap](docs/references/agent-deck-roadmap.md): longer-term direction, phase boundaries, and open validation items.

## Development and Verification

```bash
uv run pytest -q
uv run agent-deckctl version
git diff --check
```

Physical-device verification is an explicit manual smoke test and is not part of the automated suite. When reporting an issue, do not include API keys, tokens, full prompts, or private application paths; see the [contributing guide](CONTRIBUTING.md).

## License

The core codebase is licensed under the **[MIT License](LICENSE)**. `vendor/streamdock-python-sdk` is the third-party Python SDK used to communicate with MiraBox/StreamDock console devices; it comes from [MiraboxSpace/StreamDock-Plugin-SDK](https://github.com/MiraboxSpace/StreamDock-Plugin-SDK) and is also available under the [MIT License](vendor/streamdock-python-sdk/LICENSE).
