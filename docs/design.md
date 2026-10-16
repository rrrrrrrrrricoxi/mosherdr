# Design

mosherdr has two independent functions: agent badges and terminal-color compatibility. They share an installer and shell hooks, but color setup does not identify agents or change their launch commands.

## Agent badges

The local interactive zsh hook watches ordinary foreground `mosh` commands for configured host aliases. It also recognizes the exact `pi-ccc` command name as a connection to configured host `ccc`. It records a unique Herdr report source for that connection. Both entry points use the outer Herdr shell PID and the same command filters and runtime checks. It preserves existing Mosh aliases and functions and does not wrap Mosh or agent commands. Re-sourcing the hook replaces only mosherdr's own hook entries.

The local watcher uses Herdr's local API to confirm the pane, shell, and foreground connection. It reads local process identity and the Mosh client's argument vector to identify and pin the endpoint, then rechecks identity before accepting the result. The remote SSH probe matches the Mosh server's UDP socket to one attached tmux client and inspects foreground process metadata in that client's selected pane. It may inspect process arguments to identify supported agent launchers. These are bounded, read-only checks; mosherdr does not inspect terminal contents or prompts, and it does not log arguments, environment values, or credentials.

The watcher reports a badge only when it can identify one supported foreground agent. It asks Herdr to explain the state, then reports the returned state. When evidence is missing or ambiguous, it hides the report and retries. Retry delay starts at one second and grows to a maximum of ten seconds after failures. A replacement shell, Mosh process, endpoint, or remote connection does not inherit the old report. A guardian process attempts cleanup if the watcher exits unexpectedly. If Herdr's API remains unavailable during cleanup, a stale badge can remain; there is no report TTL in this integration.

The watcher requires local Herdr. The remote probe uses Python 3, tmux, Mosh process/socket metadata, `lsof`, SSH, and standard system tools. Moshdrop is optional compatibility for identifying a wrapped local connection.

## Color bridge

The remote interactive zsh hook runs a color check before each command. The helper confirms that the pane has one visual tmux client, that the client is attached to the exact pane, and that its process has a Mosh server ancestor. It also checks negotiated tmux terminal features. It does not infer truecolor support from the `TERM` name alone.

For a configured explicit foreground/background pair, a small owner process sends OSC 10 and OSC 11 color reports through remote tmux's control mode to that client. The remote tmux must support `refresh-client -r`. A marker and protocol acknowledgements ensure the reports are processed before a newly launched command starts. The bridge tracks the original pane and client; it does not read pane contents. If that client stops selecting the pane or multiple visual clients create a conflict, the daemon closes its bridge and remains dormant. It resumes only when the same client selects the same pane and the view is unambiguous again. The daemon ends when the original client detaches, the pane closes, the client identity changes, or the configured profile changes.

The profile is opt-in and per host. The bridge preserves terminal themes by reporting the explicit configured colors, honors `NO_COLOR`, and never sets `FORCE_COLOR`. The shell hook sets `COLORTERM=truecolor` only when the verified client negotiated RGB and no `NO_COLOR` or existing `COLORTERM` value blocks it. It removes only the value it added, at the next prompt, and preserves later user changes. A palette-only terminal receives the color reports without an RGB claim.

Multiple visual clients in a window make one shared default color ambiguous. The helper declines that case. Other unsupported or uncertain states stop color reports. Closing mosherdr's control client does not guarantee that tmux clears a cached palette while other control clients remain attached. A client that attaches while a TUI is already running cannot trigger the shell's pre-command check, so that TUI receives no new color report. After returning to the shell, a new command triggers the check for programs launched afterward. Generic color behavior has not yet been verified in a live session.

## Installer boundary

The installer expects a clean, committed Git checkout. It runs the unit suite, uses the source commit as the installation version, and transfers a bounded archive over strict batch SSH. The remote side verifies the archive and tests it before applying it. Installation adds an owned interactive-shell block, a private configuration, runtime files, and a receipt. It creates backups before writes and checks for concurrent changes and prior file drift. Re-running an unchanged install is safe. The installer does not start, stop, or restart Herdr or tmux.

Legacy migration is opt-in. It validates the prior installer receipt and installed file fingerprints, removes the recorded legacy shell block, and restores the protected downstream launcher. It validates and preserves the existing RGB configuration block. A mismatch, ambiguous block, or interrupted transaction causes migration to stop. Unrelated shell and proxy changes are preserved.

This package was authored as a local compatibility implementation. It does not include vendored Herdr, Mosh, or Moshdrop code.
