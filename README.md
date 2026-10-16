# mosherdr

mosherdr is a small compatibility package for agent badges in Herdr and terminal colors across Mosh and tmux. It keeps the normal `mosh`, `tmux`, and agent commands in use. It does not require Moshdrop; it can recognize a Moshdrop-wrapped connection when Moshdrop is installed.

The agent watcher uses Herdr's local API and read-only SSH probes to identify the visible agent in the foreground tmux pane. It reads process, socket, and tmux metadata. On the local machine, it reads the Mosh client's argument vector to identify and pin the connection endpoint. It does not inspect terminal contents or prompts, and it does not log arguments, environment values, or credentials. If the watcher cannot verify one agent, it hides its badge. A Herdr API failure during cleanup can leave a stale badge until Herdr clears it.

The optional color bridge sends an explicit foreground, background, and optional 16-color palette profile through remote tmux's terminal-color protocol before remote programs launch. It respects terminal themes and `NO_COLOR`. It does not set `FORCE_COLOR`, replace agent launchers, or claim RGB support for every terminal. Color reporting is disabled until both colors are configured. The bridge becomes dormant when its original client stops selecting the pane or its view becomes ambiguous; it resumes for that same client and pane when they become eligible again. It does not promise that tmux clears a previously cached palette when the bridge closes.

This project is experimental (v0.1). Automated tests use synthetic process, tmux, and Herdr responses; they do not prove compatibility with a live Herdr server, Mosh client, or terminal. See docs/testing.md for the scope of live verification.

## Requirements

- Local: macOS, zsh, Python 3.9 or later, Git, Mosh, SSH, and Herdr with its local CLI/API available.
- Remote: macOS or Linux, zsh, Python 3.9 or later, tmux with `refresh-client -r` support, Mosh, and `lsof` for process/socket metadata.
- Both shells must use zsh. Other operating systems and shells are not verified.
- The SSH host must be a configured host alias. The installer uses batch SSH and strict host-key checking.

Agent badges require Herdr only on the local machine. The remote probe does not install or call Herdr. Color bridging requires an existing remote tmux pane and a compatible local Mosh client and terminal path; local tmux is not required.

## Install

Clone this repository and use a clean, committed checkout. The installer runs the unit tests before it changes files. It stages the local runtime, transfers the pinned checkout to the remote host over SSH, and verifies the remote files. A dry run is the default:

```sh
python3 -B install.py local --host remote-box --dry-run
```

To also enable the optional color profile, provide both colors. This sample palette is illustrative; choose values for your terminal theme:

```sh
python3 -B install.py local --host remote-box \
  --foreground '#ffffff' --background '#282c34' --dry-run
```

Applications such as Pi derive their automatic theme from the first 16 ANSI
colors as well as foreground and background. To preserve that appearance through
Mosh, also pass `--palette-file /path/to/palette.json`. The file must contain a
JSON array of exactly 16 `#RRGGBB` strings, in ANSI index order. Use colors captured
from your actual local terminal. This option requires both foreground and
background options. Omitting all color options preserves the installed profile.
A later change to the local terminal theme requires updating this explicit profile.

Review the proposed local paths. Then apply the same command with `--apply`:

```sh
python3 -B install.py local --host remote-box \
  --foreground '#ffffff' --background '#282c34' --apply
```

On a first installation, omitting both color options leaves the color bridge disabled. On an existing installation, omitting them preserves its configured color profile. The installer creates protected backups and ownership receipts, checks for file drift, and refuses ambiguous or interrupted changes. It does not restart Herdr or tmux. Open a fresh local Herdr shell and start an ordinary connection, for example:

```sh
mosh remote-box -- tmux new -A -s work
```

Start agents with their ordinary commands inside the remote tmux pane.

To check the local installation receipt:

```sh
python3 -B install.py status
```

### Legacy migration

Use `--migrate-legacy` only when the previous `herdr-mosh-plain-install` installer left its receipt and its installed files still match that receipt. The migration validates those records before changing anything. It removes the owned legacy local shell block and restores the original downstream Codex launcher from the protected receipt. It preserves the existing tmux RGB color block and unrelated proxy wrappers. If a receipt or installed file has drifted, migration stops for manual review.

## Configuration and limits

Configuration is stored at `~/.config/mosherdr/config.json`; an absolute `XDG_CONFIG_HOME` changes that base directory. Runtime files go under `~/.local/share/mosherdr/runtime`. Installer receipts and backups go under `~/.local/state/mosherdr/install`. Private color state goes under `~/.local/state/mosherdr/colors`, or under an absolute `XDG_STATE_HOME` when set.

The agent watcher checks one ordinary foreground Mosh connection at a time. It retries missing evidence at one-second intervals, increasing the delay to at most ten seconds after repeated failures. It resumes only for the same observed connection; it does not transfer a badge to a replacement connection. Agent detection covers Codex, Claude Code, and Pi. It hides ambiguous results. Herdr's existing agent explanation supplies the badge state.

The watcher uses the default tmux socket. Panes in copy mode are not listed. Popups and lock screens are not classified separately. Address translation or unavailable socket metadata can prevent the remote probe from matching the connection. A cleanup API failure can leave a badge behind.

The color daemon ends when the original client detaches, the pane closes, the client is replaced, or the configured profile changes. A newly attached client does not take over the existing daemon. A fresh attachment while a TUI is already running cannot trigger the remote shell's pre-command hook, so that TUI does not receive a new color report. Return to the shell and run another command to trigger the hook for programs launched afterward. Closing mosherdr's tmux control client does not guarantee that tmux clears a cached palette while other control clients remain attached.

For design boundaries and verification scope, see [docs/design.md](docs/design.md) and [docs/testing.md](docs/testing.md).
