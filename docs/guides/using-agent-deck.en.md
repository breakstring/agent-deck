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

- The ten main keys can open or switch applications, open URLs, send keyboard shortcuts, show agent status,
  show quota/usage status, show the Codex pet, or remain unassigned. A Codex pet key is display-only and
  pressing it performs no action.
- A ChatGPT/Codex launcher key can optionally show a pet while a matching task is active. The pet temporarily
  covers the original icon; pressing the key still only opens or focuses the application.
- A shortcut can be one physical key, a chord, or a sequence of up to 16 steps. Each released step may wait 0–2000 ms, and the full sequence is limited to 10 seconds.
- Application, website, and custom shortcut icons are cached locally and shared by the configuration preview and hardware rendering. A shortcut without a custom image gets an auto-generated chord icon, and the Web preview uses the exact PNG emitted by the hardware renderer.
- Each of the four knobs can have its own rotation action. For volume actions, pressing the knob implicitly toggles output mute or microphone mute; pressing is not separately configured.
- Knob lighting is configured independently: off, or a base color with an optional breathing effect. When the device supports it, volume/brightness actions reflect state from the base color; muted states use red or turn off.
- With the Codex pet enabled, the bottom virtual panel manually rotates through Brand, Quota, Tokens, and
  Pets; disabling the pet removes Pets from that rotation. A pending approval MESSAGE temporarily overrides
  the display without changing the selected panel, which returns naturally when the approval ends.
- PETS renders active top-level local tasks and tasks from enabled SSH Remote Connections as independent pets.
  Select the PETS touch bar in the Web device preview to configure the remote-pet source and slow, medium,
  or fast patrol speed.
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
- PETS panel preferences: `~/Library/Application Support/AgentDeck/n4pro-pets-panel.json`

The repository's [`agent-deck.toml`](../../agent-deck.toml) is the default-settings example. Explicit configuration paths, isolated test directories, and custom quota presentation rules are advanced topics covered by the [Developer Q&A](../references/developer-q-and-a.md) and [Codex quota reference](../references/codex-app-server-quota.md).

### 5.1 Log Levels and File Limits

For a personal always-on daemon, Agent Deck records only `warning`, `error`, and `critical` messages by
default and disables the per-request HTTP access log. The active file rotates at 5 MiB and keeps two
backups, so total usage normally stays below roughly 15 MiB. Configure this in `agent-deck.toml`:

```toml
[logging]
level = "warning" # critical | error | warning | info | debug | trace
access_log = false
file_enabled = true
file_path = "~/Library/Logs/AgentDeck/agent-deckd.log"
max_bytes = 5242880
backup_count = 2
```

- Keep `warning` and `access_log = false` for everyday use; routine polling and successful requests are
  not logged.
- For temporary diagnostics, run `uv run agent-deckd --log-level info` or enable the access log, then
  restore the quiet defaults afterward.
- Set `file_enabled = false` to disable the file while retaining errors in the foreground or tmux console.
- `./run.sh logs` follows the rotating file. `AGENT_DECK_LOG_FILE` can still override the background
  launcher's path temporarily.
- Restart the daemon after changing the configuration.

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

### 6.1 Codex Pet

The pet system has three independent presentation surfaces:

1. Assign **Codex Pet** to a main key for persistent global Codex activity. This key is display-only.
2. Assign a ChatGPT/Codex application to a main key and enable **Show pet while a task is active**. The pet
   temporarily covers the app icon, while the key continues to open or focus the application.
3. Manually switch the bottom logical panel to PETS. Every top-level local or remote ChatGPT task that is
   active or showing completion feedback becomes an independent roaming actor.

The shortest setup is to configure either pet-capable key in the Web UI, select the touch bar in the N4 Pro
preview to open PETS settings, and choose **Save and Apply**. Inspect the resolved assets, actors, and panel
policy with:

```bash
uv run agent-deckctl status
```

Agent Deck does not maintain a second local pet selection. It reads Codex's global `selected-avatar-id` from
the legacy top level or current `[desktop]` table, using `CODEX_HOME` and falling back to `~/.codex`. For
`custom:rick`, it prefers `pets/rick/pet.json` and supports the legacy
`avatars/rick/avatar.json` location.

Configure the initial pet behavior in [`agent-deck.toml`](../../agent-deck.toml):

```toml
[codex.pet]
enabled = true
refresh_interval_seconds = 5.0
panel_fps = 8
motion = "auto" # auto | full | reduced
remote_pet_source = "builtin_random"
patrol_speed = "medium"
```

- `enabled = false` removes PETS from manual panel rotation without changing Codex's selection.
- `refresh_interval_seconds` controls read-only checks of Codex configuration and custom packages.
- `panel_fps` is the maximum PETS target frame rate; device transport can lower the effective rate.
- `motion = "auto"` follows macOS Reduce Motion when available. `reduced` pins representative frames and
  disables travel; `full` always enables animation.
- `remote_pet_source` and `patrol_speed` are startup defaults. PETS preferences saved from the Web device
  preview are stored in `n4pro-pets-panel.json` and take precedence on later runs.

Version 1 custom sheets must be 8×9 at `1536×1872`, and version 2 sheets 8×11 at `1536×2288`, with fixed
`192×208` cells. Version 2 gaze rows are not used. Absolute paths, `..`, and symlink escapes in
`spritesheetPath` are rejected. Rendering preserves complete cells without trimming or per-frame
recentering. RGB residue in fully transparent pixels is normalized and reported as a warning, but does not
reject an asset that Codex itself accepts.

Built-in pets are discovered read-only from the known installed ChatGPT/Codex application `app.asar`.
Agent Deck reads only the required resource at its exact offset and decodes active assets in memory. It
does not scan unrelated applications, unpack built-in assets to disk or the repository, or redistribute
built-in or custom pet material.

Pets are presentation-only and never change approvals, task execution, or Agent slots. Global pet activity
aggregates top-level Codex tasks with `Needs input > Blocked > Ready > Running > Idle` priority and ignores
child agents. Waiting, failed, and review reactions play three cycles before returning to slow idle.
`Ready` currently approximates `COMPLETED_RECENTLY`, because no reliable unread signal is available.

#### Task-state overlay on ChatGPT/Codex launcher keys

The launcher overlay is independent from the dedicated pet key and PETS panel. The UI recognizes current
OpenAI bundle identifiers or explicit `ChatGPT.app`/`Codex.app` paths; it does not trust a matching display
name alone. Enabling the overlay preserves both the open-or-focus action and the user's original icon.

Only top-level `codex-app:*` Desktop tasks participate. Codex CLI, child agents, and unrelated apps are
excluded. Matching tasks aggregate with `Needs input > Error > Review > Running > Completed` priority.
Running, waiting, and error states remain visible until cleared. `COMPLETED_RECENTLY` plays three waving
cycles, holds the last frame for five seconds, and restores the original app icon.

Multiple launcher keys may enable the overlay. They share asset and frame caches. The default write budget
is 10 key updates per second with at least 5 FPS per animated key, so up to two keys animate and additional
active keys use representative static frames. Pressing a launcher only changes dynamic-slot priority; it
does not acknowledge a task notification. `/status.codex_pet.app_overlay` reports linked, visible, animated,
and static-fallback key counts plus the effective FPS and write budget.

#### Multi-task PETS colony

PETS uses the existing `800×136` virtual panel but does not collapse every task into one global state. Each
top-level local or remote `codex-app:*` task becomes an actor only while active or showing completion
feedback. Its position, direction, animation phase, and base speed are derived from the Agent identity. All
actors share the full width. Their speed varies slightly with independent low-frequency envelopes;
collisions bounce only in short windows so actors do not form permanent territories. Local actors have no
host marker. Remote actors use a stable, muted halo derived from the observer host; the halo identifies
execution location, not success or failure.

Select the PETS touch bar in the Web device preview to save:

- **Remote pet source**: `follow_local` uses the local selection; `remote_config` reads the remote Codex pet
  ID; `builtin_random` gives each task a stable assignment from the locally installed built-in catalog
  without reading remote pet configuration.
- **Patrol speed**: `slow`, `medium`, or `fast`. Changing speed preserves current actor positions.

`remote_config` adds the minimum read-only `config/read` call only for SSH connections managed by ChatGPT
Settings with auto-connect strictly enabled, and retains only `selected-avatar-id`. A recognized remote
built-in ID reuses the corresponding local App asset. For remote `custom:<name>`, a short-lived system SFTP
process mirrors only the manifest and its single declared sprite sheet into Agent Deck's content-addressed
cache. It never executes pet code, copies the whole directory, writes to the remote host, or modifies the
local Codex directory. Unknown, oversized, symlinked, path-escaping, or invalid packages attempt to fall
back to a stable built-in pet instead of impersonating a stale selection.

A pending approval MESSAGE remains the highest-priority transient override and never rewrites the selected
PETS, Quota, or other panel. The dedicated N4 Pro pet key still renders one complete cell on a `112×112`
surface without horizontal travel.

`uv run agent-deckctl status` and `/status` expose the selected ID, resolution state, sprite version, global
activity, motion mode, and short asset diagnostics under `codex_pet`. `app_overlay` reports launcher-key
scheduling. `panel_colony` reports actor and remote-actor counts, assignments, source policy, speed,
collisions, built-in catalog state, and remote custom-cache state. Diagnostics never expose image bytes, a
full sprite sheet, complete remote configuration, or prompts.

The current release does not support uploading a separate Agent Deck pet package, choosing a pet per task
manually, pet-key interactions, hover/jump, version 2 gaze behavior, mouse tracking, or replacing Agent
status keys. Codex CLI tasks do not become ChatGPT App PETS actors.

### 6.2 Remote ChatGPT App Task State over SSH

ChatGPT App [Remote Connections](https://learn.chatgpt.com/docs/remote-connections.md) can run Codex on
another computer over SSH. Agent Deck enables read-only observation of these connections by default. Each
host gets an independent SSH subprocess running `codex app-server proxy`. Normal operation calls only
`initialize`, `initialized`, and `thread/list(useStateDbOnly=true)`; PETS `remote_config` additionally calls
read-only `config/read` and immediately projects only the pet selection ID.

Verify an SSH alias, remote Codex executable, and shared app-server before relying on it:

```bash
uv run agent-deckctl codex-remote-state \
  --host minibox.example \
  --timeout-seconds 10 \
  --limit 80
```

The result keeps only host summary, cwd, optional thread name, timestamps, and coarse status counts.
Previews, turns, items, rollout paths, and raw responses are discarded before they can reach daemon state,
logs, or `/status`.

After the diagnostic succeeds, add and enable the SSH connection in ChatGPT **Settings → Connections**.
The observer is enabled by default; advanced polling settings are:

```toml
[codex.remote_ssh]
enabled = true
poll_interval_seconds = 5.0
timeout_seconds = 10.0
thread_limit = 80
stale_after_seconds = 20.0
completed_feedback_seconds = 10.0
```

Agent Deck reads only ChatGPT-managed connections whose auto-connect value is strictly `true`. It does not
discover hosts from `~/.ssh/config`, historical projects, or a selected-host field. Disabling a connection
in ChatGPT closes the corresponding observer and clears its old state; ambiguous or missing Settings data
fails closed. Local and remote threads use host-aware identities, and only top-level `sourceKinds=["vscode"]`
threads participate, excluding CLI, exec, and child/subagent work.

| Remote `ThreadStatus` | Agent Deck |
| --- | --- |
| `active + waitingOnApproval` | `APPROVAL_NEEDED` |
| `active + waitingOnUserInput` | `WAITING_USER` |
| `active` without a waiting flag | `THINKING` |
| `systemError` | `ERROR` |
| `active -> idle` | Brief local `COMPLETED_RECENTLY`, then restore the original icon |
| Cold `idle` or `notLoaded` | Do not cover the original icon |

These same top-level remote tasks become independent PETS actors. Changing their pet-source policy changes
only asset assignment; it does not change host authorization, task polling, launcher overlays, or the
remote tasks themselves. A persistent connection failure clears stale state after `stale_after_seconds`.
`/status.pollers.codex_remote_ssh` reports Settings discovery counts, per-host success times, short error
types, status counts, and associated Agent counts.

OpenAI-relayed Remote Connections do not currently expose a public third-party thread-status interface.
Agent Deck does not reverse-engineer that private transport; this feature supports SSH Remotes that are
already reachable as `ssh <host>` from the local Mac.

#### Physical-device acceptance checklist

The following steps must be rerun before a release; they are not a claim that the current multi-actor
implementation has passed physical-hardware validation:

1. Configure the current local custom pet, one dedicated pet key, and PETS.
2. Run multiple local tasks and at least one authorized SSH Remote task. Verify independent actors, stable
   remote halos, all three source policies, and all three patrol speeds.
3. Run a 60-second state smoke and a 15-minute soak. Check for clipping, ghosting, or fixed-territory
   crowding.
4. Measure an effective PETS background rate of roughly 7 FPS or better. The first single-pet
   implementation's 901-second result of about 7.88 FPS is only an older baseline.
5. Confirm `open/init=1`, no unexpected reconnects or HID errors, and no sustained CPU/thread growth.
6. Explicitly close the device session and background service, leaving no `agent-deckd` process.
7. With one, two, and three pet-enabled launcher keys, measure animation rate, static fallback, response,
   original-icon restoration, and HID errors.

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
