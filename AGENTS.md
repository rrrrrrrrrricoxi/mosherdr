# Project guidance

Read README.md and docs/design.md when present before broad changes. This is the
standalone mosherdr source. Keep host-specific deployment data out of this repo.

- Use Python's standard library. Preserve ordinary Mosh, tmux, and agent commands.
- Moshdrop is optional compatibility, never a dependency.
- Keep terminal colors independent of agent identification and agent launchers.
- Never log terminal content, prompts, credentials, argv, or environment values.
- Never start, stop, or restart a user's Herdr or tmux server as a side effect.
- Preserve proxy launchers and unrelated shell/configuration changes.
- Check ownership receipts and backup changed files during migration/installation.
- Keep generated runtime state, host configurations, and bytecode out of Git.
- Run focused tests first, then `python3 -B -m unittest discover -s tests -v`.
- Delegate only when assigned by the parent. Keep concurrent writers in their
  assigned files. The parent owns interfaces, integration, and publication.
