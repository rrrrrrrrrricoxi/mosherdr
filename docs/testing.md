# Testing

The unit suite exercises configuration validation, agent identification, connection identity checks, retry behavior, shell hooks, color protocol handling, and installer planning with synthetic data and mocks. It does not connect to Herdr, open a real SSH session, create a live tmux server, or verify a terminal emulator's OSC behavior.

Run the checks from the repository root:

```sh
python3 -B -m unittest discover -s tests -v
for hook in hooks/*.zsh; do zsh -n "$hook"; done
```

The CI workflow runs those checks on Ubuntu and macOS. CI does not install or run Herdr, Mosh, tmux, or an agent application.

## Live check still needed

Before describing v0.1 as live-verified, use a disposable Herdr setup and an SSH alias such as `remote-box`. Check a fresh local Herdr shell and a standard Mosh connection into an existing tmux session. Confirm that supported agents produce the expected badge and state, that shell return and connection exit clear the badge, and that unsupported or ambiguous process evidence hides it. Confirm that the optional color profile reaches the intended client before a program starts, respects `NO_COLOR`, and makes no RGB claim when tmux does not negotiate RGB. Confirm that deselection or multiple-viewer conflict makes the daemon dormant, and that the original client and pane resume color reports when they become eligible again. Check detach, pane closure, client replacement, and profile change termination. Also check a TUI that was already running when a client attaches: the attachment does not run the shell's pre-command hook, so the TUI receives no new report; returning to the shell and launching a new command triggers the next check. Observe whether tmux retains a cached palette while other control clients remain attached; mosherdr does not guarantee that tmux clears it. Repeat with two visual clients attached to the same window and confirm that the bridge declines the ambiguous target. No live generic color results are available yet.

Do not treat results from synthetic tests as proof of remote or terminal compatibility. Platform support remains limited to macOS local zsh and macOS/Linux remote zsh until live checks establish more.
