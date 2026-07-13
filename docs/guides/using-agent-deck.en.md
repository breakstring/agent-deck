# Using Agent Deck

**[简体中文](using-agent-deck.zh-CN.md)**

This guide is for people running Agent Deck on their own Mac. It documents the currently verified path: **macOS + MiraBox N4 Pro + Codex**. You can still use the local configuration UI and daemon with fake hardware when no N4 Pro is connected.

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

`ccusage` only affects token/cost statistics. Without Bun, the configuration UI, application/URL keys, quota, and the main hardware path still work; the daemon status will record the token-polling failure reason.

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

## 3. First Run: Fake Hardware Mode

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

### tmux Launcher

Use `scripts/agent-deckd-tmux.sh` to manage a persistent daemon:

```bash
scripts/agent-deckd-tmux.sh start
scripts/agent-deckd-tmux.sh restart
scripts/agent-deckd-tmux.sh status
scripts/agent-deckd-tmux.sh logs
scripts/agent-deckd-tmux.sh attach
scripts/agent-deckd-tmux.sh stop
```

The default tmux session is `agent-deckd` and the default listener is `127.0.0.1:8765`. Set these before launching when needed:

```bash
export AGENT_DECK_TMUX_SESSION=agent-deckd-dev
export AGENT_DECK_HOST=127.0.0.1
export AGENT_DECK_PORT=8765
```

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

The `doctor` probe briefly opens, reads, and closes the device, but intentionally does not call SDK `init()`. Do not add `init()` to diagnostic scripts: a real SDK initialization can wake displays, change brightness, clear key images, or refresh the device.

### 4.2 macOS SDK Compatibility Fallback

On some macOS setups, the StreamDock dynamic library supplied with a Python package may not match the local machine. Download or check out the official MiraBox Device SDK, then point this variable to its `Python-SDK` directory or its `src` directory:

```bash
export AGENT_DECK_STREAMDOCK_SDK_PATH="/absolute/path/to/StreamDock-Device-SDK/Python-SDK"
uv run agent-deckctl doctor
```

The variable only tells Agent Deck where to import the official Python SDK. It does not install or initialize the device. After diagnostics succeed, start the real renderer:

```bash
scripts/agent-deckd-tmux.sh start
```

If the tmux session already exists, use `restart` so it inherits the new environment variable:

```bash
scripts/agent-deckd-tmux.sh restart
```

### 4.3 Current N4 Pro Behavior

- The ten main keys can open or switch applications, open URLs, show agent status, show quota/usage status, or remain unassigned.
- Application and website icons are cached locally and shared by the configuration preview and hardware rendering.
- Each of the four knobs can have its own rotation action. For volume actions, pressing the knob implicitly toggles output mute or microphone mute; pressing is not separately configured.
- Knob lighting is configured independently: off, or a base color with an optional breathing effect. When the device supports it, volume/brightness actions reflect state from the base color; muted states use red or turn off.
- The bottom virtual panel rotates through the brand image, quota, and usage. Usage can switch among Day, Week, Month, and All.

If you intentionally need to replace a residual device image after the daemon has exited, use this write operation to show the branded splash image:

```bash
uv run agent-deckctl hardware n4pro splash
```

This is not a read-only diagnostic. Make sure it will not overwrite another daemon that is currently controlling the device.

## 5. Configuration Locations and Persistence

The default configuration lookup order is:

1. An explicit `agent-deckd --config /path/to/config.toml` path.
2. The `AGENT_DECK_CONFIG` environment variable.
3. `agent-deck.toml` in the current working directory.
4. `~/Library/Application Support/AgentDeck/config.toml`.

The repository's [`agent-deck.toml`](../../agent-deck.toml) is a readable default example. Its current default is `codex.permission_request.mode = "passthrough"`, which keeps approval handling in Codex's native UI.

Hardware layouts saved from the web UI live at:

- Key layout: `~/Library/Application Support/AgentDeck/n4pro-key-layout.json`
- Knob and lighting layout: `~/Library/Application Support/AgentDeck/n4pro-rotary-layout.json`
- Codex quota presentation policy: `~/Library/Application Support/AgentDeck/quota-presentation.json`

Use these variables to isolate paths for tests or multiple configurations:

```bash
export AGENT_DECK_N4PRO_KEY_LAYOUT="/path/to/key-layout.json"
export AGENT_DECK_N4PRO_ROTARY_LAYOUT="/path/to/rotary-layout.json"
export AGENT_DECK_QUOTA_PRESENTATION="/path/to/quota-presentation.json"
```

`quota-presentation.json` is not edited by the current Web configuration page. It sets a short label,
display order, and visibility for each Codex limit. Rules match the app-server `limit_id`, never a
`primary` / `secondary` slot. An unknown future limit remains visible by default, and no file means the
same default behavior.

```json
{
  "version": 1,
  "presentation": {
    "unmatched_visible": true,
    "rules": [
      { "limit_id": "codex", "label": "Codex", "visible": true, "order": 0 },
      { "limit_id": "codex_bengalfox", "label": "Spark", "visible": true, "order": 10 }
    ]
  }
}
```

Read `codex_quota.snapshot.windows` from `GET /status` to find the current `limit_id` values.
`display_snapshot` is the filtered collection used by the hardware. Restart the daemon after changing
the file.

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

The installer creates backups before edits. Read the target files and hook content it prints. Agent Deck's `notify` hook is best-effort and must not interfere with normal Codex work; `permission-request` fails closed when the daemon is unavailable, times out, or responds incorrectly. Keep the `passthrough` default unless you understand the security impact of changing the approval flow.

## 7. Token, Cost, and Quota Data

- The daemon proactively refreshes quota and token/cost caches at startup, then follows its local refresh policy.
- Token/cost trends use `bunx ccusage`. Check `bunx --version`; without Bun or enough `ccusage` history, a trend can be empty or show only the newest point.
- Quota comes from Codex app-server-related paths. Network state, sign-in state, or Codex changes can make it temporarily unavailable. Inspect daemon data through `uv run agent-deckctl status`.
- Panel and key images use the same cache. Knob/key interaction prefers already rendered frames while stale snapshots refresh in the background, so user input is not blocked by network or usage calculations.

## 8. Troubleshooting

### The configuration UI opens, but N4 Pro does not update

Confirm that the daemon was started with the real renderer:

```bash
scripts/agent-deckd-tmux.sh status
uv run agent-deckctl status
```

Then quit the official MiraBox/StreamDock application, confirm no second daemon exists, and run `uv run agent-deckctl doctor` again. `--disable-hardware-renderer` deliberately does not own the device.

### `streamdock` fails to load on macOS

Set `AGENT_DECK_STREAMDOCK_SDK_PATH` to the official SDK's `Python-SDK` or `src`, then verify with `doctor`. Do not add `device.init()` to a probe to work around library loading issues.

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

When reporting an issue, include the macOS version, Python/uv version, device model, whether the official software is running, launch method, redacted `agent-deckctl doctor` output, and relevant daemon logs. Do not paste API keys, tokens, complete prompts, or private application paths into issues or logs.

Read the [contributing guide](../../CONTRIBUTING.md) before changing code or adding a hardware adapter.
