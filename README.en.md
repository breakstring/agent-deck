# Agent Deck

**[简体中文](README.md)**

Agent Deck is a local hardware-console bridge for AI agents. It maps agent state, usage data, and configurable actions to MiraBox hardware while providing a local browser-based configuration UI.

The current public release focuses on **macOS + MiraBox N4 Pro + Codex**. The primary path is usable today, but the project is still evolving in the `0.x` stage. Other operating systems, hardware models, and agent platforms do not have compatibility guarantees yet.

## What It Does

- Configure the ten N4 Pro LCD keys for local applications, URLs, agent status, and subscription or usage views.
- Rotate a bottom virtual panel through the brand image, subscription quota, and token/cost statistics. Usage views are pre-rendered from local caches to keep switching responsive.
- Configure the rotation action of four knobs, including panel or period switching, system input/output volume, display brightness, and console screen brightness.
- Configure knob LED colors and optional breathing effects, with live preview in the web configuration page.
- Read local Codex state, quota, and `ccusage` data, with optional Codex hook integration. The default approval mode preserves Codex's native approval UI instead of moving approval control to hardware.
- Run the configuration UI and core service with fake hardware when no physical device is attached.

## Current Support

| Area | Status |
| --- | --- |
| Operating system | macOS is the verified target. Windows and Linux are not formally supported yet. |
| Physical hardware | MiraBox N4 Pro. The architecture leaves room for other StreamDock/MiraBox models, but they are not released as supported targets. |
| Agent | Codex local App/CLI state and hook integration. |
| Python | Python 3.11 or later. |
| Usage trends | Optional: Bun's `bunx` and `ccusage`. Without them, other features still run, but token/cost trends are unavailable. |

## Quick Start

Read the full [usage guide](docs/guides/using-agent-deck.en.md) for setup, hardware ownership, and troubleshooting. This is the shortest route to run the local UI with fake hardware:

```bash
git clone https://github.com/breakstring/agent-deck.git
cd agent-deck
uv sync --all-groups

# Inspect the local environment and hardware hints.
uv run agent-deckctl doctor

# Start without taking ownership of a physical device.
scripts/agent-deckd-tmux.sh start --disable-hardware-renderer
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/), then stop the service with:

```bash
scripts/agent-deckd-tmux.sh stop
```

To take ownership of an N4 Pro, first quit the official MiraBox/StreamDock application and use `doctor` to inspect device hints. If the SDK dynamic library is not compatible on macOS, point `AGENT_DECK_STREAMDOCK_SDK_PATH` to the official Python SDK. See [Run with Physical Hardware](docs/guides/using-agent-deck.en.md#run-with-physical-hardware).

## Documentation

- [Usage guide (English)](docs/guides/using-agent-deck.en.md): installation, startup, configuration, hardware, Codex integration, and troubleshooting.
- [使用指南（中文）](docs/guides/using-agent-deck.zh-CN.md)
- [Contributing guide (English)](CONTRIBUTING.md)
- [贡献指南（中文）](CONTRIBUTING.zh-CN.md)
- [Project roadmap](docs/references/agent-deck-roadmap.md): upcoming direction and scope.
- [Overall design](docs/superpowers/specs/2026-06-12-agent-deck-analysis.md): event, state, layout, and hardware boundaries.

## Running the Daemon

The tmux launcher is the recommended way to manage a persistent daemon:

```bash
scripts/agent-deckd-tmux.sh start
scripts/agent-deckd-tmux.sh status
scripts/agent-deckd-tmux.sh logs
scripts/agent-deckd-tmux.sh restart
scripts/agent-deckd-tmux.sh attach
scripts/agent-deckd-tmux.sh stop
```

You can also use the root `run.sh` process manager or run the daemon directly:

```bash
uv run agent-deckd --host 127.0.0.1 --port 8765
```

## Security and Privacy Boundaries

- Hardware input is first reduced to a business intent and is then executed by the action layer. Hardware drivers must not directly execute shell commands or inject text into an agent.
- The default Codex approval mode in `agent-deck.toml` is `passthrough`, so Codex keeps its native approval UI.
- Full user prompts are not collected by default. Configured application paths, URLs, and cached icons remain local.
- `agent-deckctl doctor` is read-only. Do not call `device.init()` from diagnostic code because it can wake, clear, or refresh a physical device.

## License

The core codebase of this project is licensed under the **[MIT License](LICENSE)**. You are free to use, modify, and distribute this software.

### Third-Party Components & Licenses

This project includes the following third-party components inside the `vendor/` directory:
- **streamdock-python-sdk**: A Python SDK for interacting with MiraBox/StreamDock console devices.
  - **Original Repository**: [MiraboxSpace/StreamDock-Plugin-SDK](https://github.com/MiraboxSpace/StreamDock-Plugin-SDK)
  - **License**: [MIT License](vendor/streamdock-python-sdk/LICENSE)


## Development and Verification

```bash
uv run pytest -q
uv run agent-deckctl version
git diff --check
```

Physical-device verification is an explicit manual smoke test and is not part of automated tests. See the [contributing guide](CONTRIBUTING.md).
