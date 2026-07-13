# Agent Deck

**[简体中文](README.md)**

> Bring local AI-agent status, subscription quota, and controlled actions to the physical surface of a MiraBox N4 Pro.

Agent Deck is a local hardware-console bridge for AI agents. It maps agent state, usage, and explicitly configured actions to MiraBox hardware while providing a browser-based local configuration UI. The current version is **`0.1.0`** and completes the first usable path for **macOS + MiraBox N4 Pro + Codex**. The project remains in the `0.x` stage; other operating systems, hardware models, and agent platforms do not yet carry compatibility guarantees.

## Product Video

[![Play the Agent Deck product introduction](assets/agent-deck/brand-intro-v02.png)](https://breakstring.github.io/agent-deck/)

Select the image to open the browser-based product-video page. The page is deployed separately through GitHub Pages; the final 1080p MP4 will be attached to the first [`v0.1.0` Release](https://github.com/breakstring/agent-deck/releases/tag/v0.1.0).

## Why Agent Deck

When several AI agents are running at once, their states, pending input, and usage information are scattered across terminals, desktop apps, and windows. Agent Deck reduces those local signals into a unified state, then projects it onto a hardware surface with keys, a touch display, and knobs. You can see state at a glance, switch panels or focus context when needed, and keep high-risk actions disabled by default.

The core boundary remains:

```text
Agent ingress -> NormalizedEvent -> AgentStateStore -> DeckMode/LayoutPlan
             -> HardwareSurface -> InteractionIntent/ActionExecutor
```

Codex and the N4 Pro are the currently verified combination, not irreplaceable implementation assumptions.

## Web Configuration

![Agent Deck local web configuration: N4 Pro preview, key purpose, and save-and-apply control](assets/agent-deck/config.png)

The local configuration page uses the N4 Pro preview as its workspace. Select a key or knob to edit it; changes appear in the GUI preview first and reach a connected physical device only after you choose **Save and Apply**. The current configuration surface includes:

- Ten LCD main keys for local apps, URLs, subscription/quota, token/cost usage, and agent-status entry points.
- A bottom logical panel for the brand card, Codex quota, and usage trends. Usage trends come from local caches so a panel switch does not block hardware interaction.
- Four knob rotation actions, such as changing a panel or time period, adjusting system input/output volume, display brightness, and console-screen brightness.
- A shared knob-ring color and optional breathing effect, both previewable before saving.

Without a connected device, the configuration page and core service still run with fake hardware for exploration, development, and troubleshooting.

## Current Support

| Area | Status |
| --- | --- |
| Project version | `0.1.0`; the first formal GitHub Release will use the `v0.1.0` tag. |
| Operating system | macOS is the verified target. Windows and Linux are not formally supported yet. |
| Physical hardware | MiraBox N4 Pro. The architecture leaves room for other StreamDock/MiraBox models, but they are not released as supported targets. |
| Agent | Local Codex App/CLI state, quota, and hook integration. |
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

The default approval mode keeps Codex's native approval UI and does not automatically move approval control to hardware. Text input and approval/deny actions are high-risk capabilities that require explicit configuration. If the daemon is unavailable, a response is invalid, or a wait times out, the approval path follows a fail-closed policy.

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
