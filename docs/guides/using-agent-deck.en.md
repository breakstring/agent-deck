# Using Agent Deck

**[简体中文](using-agent-deck.zh-CN.md)**

This guide is for people running Agent Deck on their own Mac. It documents the currently verified path: **macOS + MiraBox N4 Pro + Codex**. It calls `agent-deckd` the “background service” and intentionally avoids internal protocol and status details. Developers should use the [Developer Q&A](../references/developer-q-and-a.md) for those details.

## 1. Prerequisites

### Required Dependencies

| Item | Purpose | Check |
| --- | --- | --- |
| macOS | Currently verified runtime platform | `sw_vers` |
| Python 3.11+ | Agent Deck runtime | `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | Python environment and dependency management | `uv --version` |
| Git | Obtain the source | `git --version` |

### Optional Dependencies

| Item | When it is needed | Check |
| --- | --- | --- |
| `tmux` and `lsof` | Use the recommended persistent launcher | `tmux -V`, `lsof -v` |
| [Bun](https://bun.sh/docs/installation) | Show token/cost history trends; the project reads data through `bunx ccusage` | `bunx --version` |
| MiraBox N4 Pro | Display and input on physical hardware | `uv run agent-deckctl doctor` |
| Official StreamDock Python SDK | Compatibility fallback when the bundled SDK cannot load on macOS | See [Run with Physical Hardware](#run-with-physical-hardware) |
| Codex | Show Codex state and quota, or install optional hook integration | `uv run agent-deckctl codex-detect --enable-integration` |

`ccusage` only affects token/cost statistics. Without Bun, the configuration UI, application/URL keys, quota, and hardware display still work.

## 2. Get the Source and Install Dependencies

```bash
git clone https://github.com/breakstring/agent-deck.git
cd agent-deck
uv sync --all-groups
```

Confirm that the CLI works:

```bash
uv run agent-deckctl version
uv run agent-deckctl doctor
```

`doctor` only reads local environment and hardware ownership hints. It does not initialize, clear, or refresh a physical device, so use it first when diagnosing setup issues.

## 3. First Run: Preview Without Taking Over Hardware

For the first run, avoid taking ownership of physical hardware and verify the local service and configuration UI first:

```bash
scripts/agent-deckd-tmux.sh start --disable-hardware-renderer
scripts/agent-deckd-tmux.sh status
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/) in a browser. You can edit key, knob, and lighting settings there. Changes update the preview while you edit and are sent to hardware only after **Save and Apply**.

View logs and stop the service:

```bash
scripts/agent-deckd-tmux.sh logs
scripts/agent-deckd-tmux.sh stop
```

### Background Launcher

Use `scripts/agent-deckd-tmux.sh` to manage the background service:

```bash
scripts/agent-deckd-tmux.sh start
scripts/agent-deckd-tmux.sh restart
scripts/agent-deckd-tmux.sh status
scripts/agent-deckd-tmux.sh logs
scripts/agent-deckd-tmux.sh attach
scripts/agent-deckd-tmux.sh stop
```

The default service name is `agent-deckd`, and the configuration page uses `127.0.0.1:8765`. Set a different name or port only when running multiple services:

```bash
export AGENT_DECK_TMUX_SESSION=agent-deckd-dev
export AGENT_DECK_HOST=127.0.0.1
export AGENT_DECK_PORT=8765
```

This script keeps the service running after the terminal window closes. If the background service itself exits, use `restart`. A normal N4 Pro disconnect or restart should recover automatically without restarting the service.

Without tmux, use the root `./run.sh start` process manager or run in the foreground:

```bash
uv run agent-deckd --host 127.0.0.1 --port 8765
```

## 4. Run with Physical Hardware

### 4.1 Check Before Taking Ownership

MiraBox N4 Pro is the currently supported physical device. Before starting:

1. Quit the official MiraBox/StreamDock application so it does not own the device.
2. Ensure no other Agent Deck daemon is taking ownership of the same device.
3. Run the read-only diagnostics:

   ```bash
   uv run agent-deckctl doctor --json
   uv run agent-deckctl hardware n4pro status
   ```

These diagnostic commands do not change the N4 Pro display, so use them first.

### 4.2 macOS SDK Compatibility Fallback

On some macOS setups, the StreamDock dynamic library supplied with a Python package may not match the local machine. Download or check out the official MiraBox Device SDK, then point this variable to its `Python-SDK` directory or its `src` directory:

```bash
export AGENT_DECK_STREAMDOCK_SDK_PATH="/absolute/path/to/StreamDock-Device-SDK/Python-SDK"
uv run agent-deckctl doctor
```

The variable only tells Agent Deck where to load the official Python SDK. After diagnostics succeed, start the background service:

```bash
scripts/agent-deckd-tmux.sh start
```

If the tmux session already exists, use `restart` so it inherits the new environment variable:

```bash
scripts/agent-deckd-tmux.sh restart
```

### 4.3 Current N4 Pro Behavior

- The ten main keys can open or switch applications, open URLs, send keyboard shortcuts, show agent status, show quota/usage status, or remain unassigned.
- A shortcut can be one physical key, a chord, or a sequence of up to 16 steps. Each released step may wait 0–2000 ms, and the full sequence is limited to 10 seconds.
- Application, website, and custom shortcut icons are cached locally and shared by the configuration preview and hardware rendering. A shortcut without a custom image gets an auto-generated chord icon, and the Web preview uses the exact PNG emitted by the hardware renderer.
- Each of the four knobs can have its own rotation action. For volume actions, pressing the knob implicitly toggles output mute or microphone mute; pressing is not separately configured.
- Knob lighting is configured independently: off, or a base color with an optional breathing effect. When the device supports it, volume/brightness actions reflect state from the base color; muted states use red or turn off.
- The bottom virtual panel rotates through the brand image, quota, and usage. Usage can switch among Day, Week, Month, and All.
- If the N4 Pro is unplugged, loses power, or restarts, the background service waits for it to return and restores the display, lighting, and input. This normally takes a few seconds and does not require the official StreamDock application.

### 4.4 Keyboard Shortcuts and macOS Permission

After assigning **Keyboard Shortcut** to a main key, select **Start Recording** and enter one or more real
chords continuously; **Continue Recording** resumes an existing sequence. While recording, the control
becomes **Stop and Apply**. It stops capture and invokes the same full-device save action as the header
**Save and Apply** button. The nearby **Apply to Hardware** action is the same operation, not a second save
mechanism. Use the manual picker for modifier-only steps. Saving the binding enables it; there is no
separate global or per-key enable switch.

Before the first execution, select **Request Current Agent Permission** in the configuration UI. The
browser is only the configuration surface and does not need Accessibility access; the Agent background
process that posts key events is the permission requester. The UI keeps this as a compact status row; hover,
keyboard focus, or a **Details** click reveals the current executable plus **Open Accessibility Settings**
and **Recheck** actions. Startup and normal status refreshes only preflight this permission and never request
it automatically.

Starting through `scripts/agent-deckd-tmux.sh` is a development mode whose final executable is a Python
runtime. Depending on the launch chain, macOS may show Codex, Terminal, or Python as the responsible entry,
and changing launch methods may require another authorization. Use the entry introduced by the explicit
request; do not authorize the browser. The distribution roadmap replaces this with a signed
`Agent Deck.app` and user-level Agent service with a stable identity.

The sequence pins the frontmost application PID at execution start. A `succeeded` job means events were
posted, not that the target application handled them. Only one sequence runs at a time; another key press
returns `busy` and is not queued. Version 1 does not support Fn, media keys, Caps Lock, text, mouse, shell,
or mixed actions.

If you intentionally need to replace a residual device image after the daemon has exited, use this write operation to show the branded splash image:

```bash
uv run agent-deckctl hardware n4pro splash
```

This command changes the device display. Quit the official StreamDock application and any other Agent Deck service before running it.

## 5. Configuration and Backups

For everyday use, open [http://127.0.0.1:8765/](http://127.0.0.1:8765/), edit the layout, and choose **Save and Apply**. To back up or move settings to another Mac, the main files saved by the Web configuration page are:

- Key layout: `~/Library/Application Support/AgentDeck/n4pro-key-layout.json`
- Custom shortcut icons: `~/Library/Application Support/AgentDeck/shortcut-icons/`
- Knob and lighting layout: `~/Library/Application Support/AgentDeck/n4pro-rotary-layout.json`
- Codex quota presentation policy: `~/Library/Application Support/AgentDeck/quota-presentation.json`

The repository's [`agent-deck.toml`](../../agent-deck.toml) is the default-settings example. Explicit configuration paths, isolated test directories, and custom quota presentation rules are advanced topics covered by the [Developer Q&A](../references/developer-q-and-a.md) and [Codex quota reference](../references/codex-app-server-quota.md).

## 6. Codex Integration

Start by producing a read-only detection report and manual integration guidance:

```bash
uv run agent-deckctl codex-detect --enable-integration
```

The installer is dry-run by default and does not write user configuration:

```bash
uv run agent-deckctl codex-install
```

Only apply changes after reviewing the output:

```bash
uv run agent-deckctl codex-install --apply
```

The installer creates backups before edits. Keep the default approval setting unless you understand the effect of changing it; by default, approvals stay in Codex's native interface.

## 7. Token, Cost, and Quota

- The background service updates quota and token/cost information automatically.
- Token/cost trends require Bun. Run `bunx --version`; without Bun or enough history, a trend may be empty.
- Network problems, a signed-out Codex session, or a Codex version change can make quota temporarily unavailable. It normally returns on a later update.

## 8. Troubleshooting

### The configuration UI opens, but N4 Pro does not update

Confirm that the background service is running:

```bash
scripts/agent-deckd-tmux.sh status
uv run agent-deckctl status
```

Then quit the official MiraBox/StreamDock application, make sure Agent Deck is not running twice, and run `uv run agent-deckctl doctor` again. If the launch command contains `--disable-hardware-renderer`, remove it and restart.

### `streamdock` fails to load on macOS

Follow the SDK compatibility steps under [Run with Physical Hardware](#run-with-physical-hardware), then run `uv run agent-deckctl doctor` again.

### Insufficient macOS permission reports successful writes but leaves the brand image

When launching or debugging from Codex Desktop, set its execution policy to **Full Access**, then quit and restart Codex completely. If `not permitted` still appears, open System Settings -> Privacy & Security -> Input Monitoring, allow Codex or the terminal that launches Agent Deck, and restart that application.

### A shortcut reports permission required or does not affect the frontmost app

Use **Request Permission** in the configuration page, then allow the application that runs the daemon in
System Settings -> Privacy & Security -> Accessibility. Restart that terminal/application and refresh the
page. Make sure the intended target is frontmost when the hardware key is pressed; Agent Deck pins that app
and does not infer a target from the shortcut label. The `/status` response exposes capability, active, and
recent job diagnostics under `keyboard_shortcuts`.

### N4 Pro does not recover after a restart or reconnect

Inspect the service and its logs:

```bash
uv run agent-deckctl status
scripts/agent-deckd-tmux.sh logs
```

Normally the device recovers within a few seconds after reconnecting. If it remains on the brand image:

1. Check Codex or terminal permissions as described above.
2. Run `scripts/agent-deckd-tmux.sh restart`.
3. If it still does not recover, stop Agent Deck and open the official StreamDock application to confirm that it recognizes the device.
4. Quit the official application completely before starting Agent Deck again, so both applications never control the device at once.

When reporting the problem, include logs, firmware version, and whether the device connects directly to the Mac or through a USB hub. See the [Developer Q&A](../references/developer-q-and-a.md) for protocol details and internal status fields.

### The token/cost panel has no trend line

Run:

```bash
bunx --version
uv run agent-deckctl status
```

Confirm Bun is available and allow enough `ccusage` history to accumulate. With no history, a single latest point is the expected fallback.

### The port is busy or the tmux launcher fails

Inspect the session and port:

```bash
scripts/agent-deckd-tmux.sh status
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

Stop the known Agent Deck session before starting again, or choose another `AGENT_DECK_PORT`.

## 9. Getting Help and Reporting Issues

When reporting an issue, include the macOS version, device model, whether the official software was opened, launch method, `agent-deckctl doctor` output, and relevant logs. Do not paste API keys, tokens, complete conversations, or other private information.

Read the [contributing guide](../../CONTRIBUTING.md) before changing code or adding a hardware adapter.
