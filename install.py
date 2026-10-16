#!/usr/bin/env python3
"""Install a reviewed local checkout and its remote Mosh integration."""

import argparse
import base64
import fcntl
import io
import json
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import uuid

sys.dont_write_bytecode = True

import config
from transactions import InstallError, atomic, digest, fingerprint, run_bounded, safe_path, snapshot, open_lock, private_directory, safe_unlink

RUNTIME = ("agent.py", "colors.py", "config.py", "palette.py", "transactions.py",
           "install.py", "mosherdr.py", "hooks/local.zsh", "hooks/remote.zsh")
BEGIN = b"# BEGIN mosherdr\n"
END = b"# END mosherdr\n"
LEGACY_BEGIN = b"# BEGIN herdr-mosh-compat\n"
LEGACY_END = b"# END herdr-mosh-compat\n"


def state_path(home):
    return Path(home) / ".local/state/mosherdr/install"


def runtime_path(home):
    return Path(home) / ".local/share/mosherdr/runtime"


def block(data, begin=BEGIN, end=END):
    if data.count(begin) != 1 or data.count(end) != 1:
        raise InstallError("owned shell block is missing or ambiguous")
    start, stop = data.find(begin), data.find(end)
    if stop < start or (start and data[start - 1:start] != b"\n"):
        raise InstallError("owned shell block boundaries changed")
    return data[start:stop + len(end)]


def remove_block(data, begin=BEGIN, end=END):
    old = block(data, begin, end)
    return data.replace(old, b"", 1)


def observed_snapshot(path, observed=None):
    path = safe_path(path)
    data, current = snapshot(path)
    if observed is not None:
        if path in observed and observed[path] != current:
            raise InstallError("concurrent file change while planning: " + str(path))
        observed[path] = current
    return data, current


def read_configuration(path, observed=None):
    data, current = observed_snapshot(path, observed)
    if current is None:
        return {"version": 1, "hosts": {}}
    if len(data) > 65536:
        raise InstallError("configuration exceeded size limit")
    return config.validate(json.loads(data))


def load_receipt(home, observed=None):
    state = state_path(home)
    _, pending = observed_snapshot(state / "pending.json", observed)
    if pending is not None:
        raise InstallError("interrupted installation: inspect the pending journal and backups")
    data, current = observed_snapshot(state / "receipt.json", observed)
    if current is None:
        return None
    receipt = json.loads(data)
    if (not isinstance(receipt, dict) or receipt.get("schema") != 1
            or receipt.get("role") not in ("local", "remote")
            or not isinstance(receipt.get("files"), dict)):
        raise InstallError("invalid installation receipt")
    return receipt


def check_receipt(receipt, shell, configuration, observed=None):
    if receipt is None:
        return
    shell, configuration = safe_path(shell), safe_path(configuration)
    home = shell.parent
    expected_paths = {str(runtime_path(home) / name) for name in RUNTIME}
    expected_paths.update((str(shell), str(configuration), str(home / ".local/bin/mosherdr")))
    restored = receipt.get("legacy_restored_files", {})
    allowed_restored = {str(home / ".local/bin/codex")} if receipt.get("role") == "remote" else set()
    if (not isinstance(restored, dict) or set(restored) - allowed_restored
            or receipt.get("configuration") != str(configuration)
            or set(receipt["files"]) != expected_paths):
        raise InstallError("installation receipt paths or configuration identity changed")
    runtime = runtime_path(home)
    if (not runtime.is_dir() or runtime.is_symlink()
            or {p.relative_to(runtime).as_posix() for p in runtime.rglob("*")
                if p.is_symlink() or not p.is_dir()} != set(RUNTIME)):
        raise InstallError("runtime contains unowned or missing files")
    for name, expected in receipt["files"].items():
        path = safe_path(name)
        data, actual = observed_snapshot(path, observed)
        if not isinstance(expected, dict) or set(expected) != {"sha256", "mode"}:
            raise InstallError("invalid installation fingerprint")
        if path == shell:
            valid = (actual is not None and actual["mode"] == expected["mode"]
                     and digest(block(data)) == receipt.get("shell_block_sha256"))
        elif path == configuration:
            config.validate(json.loads(data))
            valid = actual is not None and actual["mode"] == expected["mode"]
        else:
            valid = actual == expected
        if not valid:
            raise InstallError("installed file drift: " + str(path))


def legacy_plan(home, role, shell_data, observed=None):
    """Validate the old receipt; never erase it to force a migration."""
    home = Path(home)
    state = home / ".local/state/herdr-mosh-plain-install"
    _, pending = observed_snapshot(state / "pending.json", observed)
    if pending is not None:
        raise InstallError("legacy installation has an interrupted journal")
    raw, receipt_before = observed_snapshot(state / "receipt.json", observed)
    if receipt_before is None:
        raise InstallError("legacy migration requested but no legacy receipt exists")
    receipt = json.loads(raw)
    if not isinstance(receipt, dict) or not isinstance(receipt.get("files"), dict):
        raise InstallError("invalid legacy receipt")
    expected_role = "macbook" if role == "local" else "ccc"
    if receipt.get("role") != expected_role:
        raise InstallError("legacy host role does not match")
    shell = home / ".zshrc"
    if role == "local" and str(shell) not in receipt["files"]:
        raise InstallError("legacy receipt does not own the shell block")
    for name, expected in receipt["files"].items():
        path = safe_path(name)
        data, actual = observed_snapshot(path, observed)
        if path == shell and role == "local":
            valid = (actual is not None and actual["mode"] == expected["mode"]
                     and digest(block(data, LEGACY_BEGIN, LEGACY_END))
                     == receipt.get("zshrc_block_sha256"))
        elif name == receipt.get("tmux_config"):
            valid = (actual is not None and actual["mode"] == expected["mode"]
                     and digest(block(data, b"# BEGIN herdr-mosh-compat RGB\n",
                                      b"# END herdr-mosh-compat RGB\n"))
                     == receipt.get("tmux_block_sha256"))
        else:
            valid = actual == expected
        if not valid:
            raise InstallError("legacy installed file drift: " + str(path))
    changes = {}
    if role == "local":
        shell_data = remove_block(shell_data, LEGACY_BEGIN, LEGACY_END)
    else:
        tmux_config = receipt.get("tmux_config")
        if not isinstance(tmux_config, str) or tmux_config not in receipt["files"]:
            raise InstallError("legacy receipt has no protected tmux RGB configuration")
        launcher = home / ".local/bin/codex"
        downstream = state / "downstream-codex"
        if str(launcher) not in receipt["files"] or str(downstream) not in receipt["files"]:
            raise InstallError("legacy receipt has no protected Codex launcher")
        downstream_data, _ = observed_snapshot(downstream, observed)
        changes[launcher] = (downstream_data, receipt["files"][str(launcher)]["mode"])
    return shell_data, changes, digest(raw)


def plan(home, role, payload, commit, host=None, colors=None, migrate_legacy=False,
         configuration=None):
    home = Path(home).absolute()
    if role not in ("local", "remote"):
        raise InstallError("invalid host role")
    if set(payload) != set(RUNTIME):
        raise InstallError("runtime inventory does not match this release")
    runtime, shell = runtime_path(home), home / ".zshrc"
    configuration = safe_path(configuration if configuration else home / ".config/mosherdr/config.json")
    observed = {}
    receipt = load_receipt(home, observed)
    if receipt and receipt.get("role") != role:
        raise InstallError("host role differs from its receipt")
    check_receipt(receipt, shell, configuration, observed)
    original, shell_before = observed_snapshot(shell, observed)
    settings = read_configuration(configuration, observed)
    changes = {}
    legacy_hash = receipt.get("legacy_receipt_sha256") if receipt else None
    if migrate_legacy and not legacy_hash:
        original, legacy_changes, legacy_hash = legacy_plan(home, role, original, observed)
        changes.update(legacy_changes)
    elif LEGACY_BEGIN in original or LEGACY_END in original:
        raise InstallError("legacy hook found; use --migrate-legacy to validate and replace it")
    if receipt:
        original = remove_block(original)
    elif BEGIN in original or END in original:
        raise InstallError("unowned mosherdr shell block collision")
    if colors is not None:
        config.validate_colors(colors)
    if role == "local":
        config.validate_host(host)
        options = settings.setdefault("hosts", {}).setdefault(host, {})
        if colors is not None:
            options["colors"] = colors
    elif colors is not None:
        settings["colors"] = colors
    settings.setdefault("version", 1)
    config.validate(settings)
    legacy_restored = {str(path): {"sha256": digest(data), "mode": mode}
                       for path, (data, mode) in changes.items()}
    if receipt:
        legacy_restored = receipt.get("legacy_restored_files", legacy_restored)
    if runtime.exists():
        if runtime.is_symlink() or not runtime.is_dir():
            raise InstallError("runtime is not a regular directory")
        owned = {Path(name).relative_to(runtime).as_posix() for name in (receipt or {}).get("files", {})
                 if Path(name).is_relative_to(runtime)}
        actual = {p.relative_to(runtime).as_posix() for p in runtime.rglob("*") if p.is_symlink() or not p.is_dir()}
        if actual != owned:
            raise InstallError("runtime contains unowned or missing files")
    for name, data in payload.items():
        target = runtime / name
        _, target_before = observed_snapshot(target, observed)
        if target_before is not None and not receipt:
            raise InstallError("unowned runtime file collision")
        changes[target] = (data, 0o644)
    executable = home / ".local/bin/mosherdr"
    _, executable_before = observed_snapshot(executable, observed)
    if executable_before is not None and (not receipt or str(executable) not in receipt["files"]):
        raise InstallError("unowned mosherdr executable collision")
    wrapper = ("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -B " +
               shlex.quote(str(runtime / "mosherdr.py")) + ' "$@"\n').encode()
    changes[executable] = (wrapper, 0o755)
    hook = runtime / "hooks" / ("local.zsh" if role == "local" else "remote.zsh")
    shell_block = BEGIN + ("if [[ -o interactive ]]; then\n  source " +
                           shlex.quote(str(hook)) + "\nfi\n").encode() + END
    changes[shell] = (original + (b"\n" if original and not original.endswith(b"\n") else b"")
                      + shell_block, shell_before["mode"] if shell_before else 0o600)
    changes[configuration] = ((json.dumps(settings, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    for path in changes:
        if path not in observed:
            observed_snapshot(path, observed)
    # Every earlier read remains the baseline; never silently adopt a later edit.
    for path, expected in observed.items():
        if fingerprint(path) != expected:
            raise InstallError("concurrent file change while planning: " + str(path))
    before = observed
    metadata = {"schema": 1, "role": role, "source_commit": commit,
                "shell_block_sha256": digest(shell_block), "configuration": str(configuration),
                "legacy_receipt_sha256": legacy_hash, "legacy_restored_files": legacy_restored}
    return changes, before, metadata


def apply_plan(home, changes, before, metadata, dry_run=False):
    actual = {path: pair for path, pair in changes.items()
              if before[path] != {"sha256": digest(pair[0]), "mode": pair[1]}}
    print(metadata["role"] + ": " + str(len(actual)) + " file(s) " +
          ("planned" if dry_run else "to update"), flush=True)
    if dry_run:
        for path in actual:
            print(str(path))
        return
    state = state_path(home)
    private_directory(state)
    with open_lock(state / "lock") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("another installer is active") from exc
        _, pending = snapshot(state / "pending.json")
        if pending is not None:
            raise InstallError("interrupted installation: inspect journal and backups")
        for path in before:
            if fingerprint(path) != before[path]:
                raise InstallError("concurrent file change before installation")
        backup = state / "backups" / uuid.uuid4().hex
        journal = []
        if actual:
            private_directory(state / "backups")
            private_directory(backup, exclusive=True)
            for index, path in enumerate(actual):
                previous = before[path]
                saved = backup / str(index)
                if previous is not None:
                    data, backed_up = snapshot(path)
                    if backed_up != previous:
                        raise InstallError("concurrent file change during backup")
                    atomic(saved, data, 0o600)
                    if fingerprint(saved)["sha256"] != previous["sha256"]:
                        raise InstallError("concurrent file change during backup")
                journal.append({"path": str(path), "before": previous,
                                "backup": str(saved) if previous else None})
            journal_data = json.dumps(journal, indent=2).encode()
            atomic(state / "pending.json", journal_data, 0o600)
            for path, (data, mode) in actual.items():
                if fingerprint(path) != before[path]:
                    raise InstallError("concurrent file change during installation")
                atomic(path, data, mode)
        for path, expected in before.items():
            if path not in changes and path != state / "pending.json" and fingerprint(path) != expected:
                raise InstallError("concurrent installation input change before receipt")
        desired = {str(path): {"sha256": digest(data), "mode": mode}
                   for path, (data, mode) in changes.items()}
        for path in changes:
            if fingerprint(path) != desired[str(path)]:
                raise InstallError("concurrent installed file change before receipt")
        receipt = dict(metadata, files={name: expected for name, expected in desired.items()
                                       if name not in metadata.get("legacy_restored_files", {})})
        if actual:
            receipt["backup"] = str(backup)
        else:
            previous_data, previous_fp = snapshot(state / "receipt.json")
            previous = json.loads(previous_data) if previous_fp is not None else None
            if previous and previous.get("backup"):
                receipt["backup"] = previous["backup"]
        atomic(state / "receipt.json", (json.dumps(receipt, indent=2) + "\n").encode(), 0o600)
        if actual:
            if fingerprint(state / "pending.json") != {"sha256": digest(journal_data), "mode": 0o600}:
                raise InstallError("installation journal changed before cleanup")
            safe_unlink(state / "pending.json")
            print(metadata["role"] + ": installed; protected backup " + str(backup), flush=True)
        else:
            print(metadata["role"] + ": unchanged", flush=True)


def verify(source):
    print("Verifying mosherdr (120-second limit)...", flush=True)
    try:
        result = run_bounded([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-q"],
                             cwd=source, stdin=subprocess.DEVNULL, timeout=120, cleanup_grace=4)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("verification timed out; no files installed on this host") from exc
    if result.returncode:
        raise InstallError("tests failed; no files installed on this host")


def clean_commit(source):
    result = subprocess.run(["git", "-C", str(source), "status", "--porcelain=v1"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if result.returncode or result.stdout:
        raise InstallError("install from a clean, committed checkout")
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if result.returncode:
        raise InstallError("source checkout has no commit")
    return result.stdout.decode("ascii").strip()


def git_bytes(source, commit, name=None):
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise InstallError("invalid source commit")
    arguments = (["show", commit + ":" + name] if name is not None
                 else ["ls-tree", "-r", "--name-only", commit])
    try:
        result = subprocess.run(["git", "-C", str(source), *arguments],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("source object read timed out") from exc
    if result.returncode:
        raise InstallError("required file is missing from the source commit")
    return result.stdout


def runtime_payload(source, commit=None):
    if commit is not None:
        return {name: git_bytes(source, commit, name) for name in RUNTIME}
    return {name: snapshot(Path(source) / name)[0] for name in RUNTIME}


def distribution(source, commit, colors, migrate):
    source = Path(source)
    tracked = git_bytes(source, commit).decode("utf-8", "strict").splitlines()
    files = list(RUNTIME) + [name for name in tracked
                             if re.fullmatch(r"tests/test_[a-z_]+\.py", name)]
    payload = {name: git_bytes(source, commit, name) for name in files}

    manifest = {"files": {name: digest(data) for name, data in payload.items()},
                "source_commit": commit, "colors": colors, "migrate_legacy": migrate}
    payload["MANIFEST.json"] = json.dumps(manifest, sort_keys=True).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in sorted(payload.items()):
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(data), 0o644
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


# The receiver uses only the standard library until every archive member has
# been validated and materialized in its fresh, private temporary directory.
BOOTSTRAP = r'''import hashlib,io,json,re,sys,tarfile,tempfile
from pathlib import Path
sys.dont_write_bytecode=True
data=sys.stdin.buffer.read(4*1024*1024+1)
if len(data)>4*1024*1024: raise SystemExit('archive too large')
files={}
with tarfile.open(fileobj=io.BytesIO(data),mode='r:') as archive:
 for item in archive:
  if (not item.isfile() or item.name in files or item.size>512*1024 or
      not re.fullmatch(r'(?:[a-z_]+\.py|hooks/(?:local|remote)\.zsh|tests/test_[a-z_]+\.py|MANIFEST\.json)',item.name)):
   raise SystemExit('unexpected archive member')
  files[item.name]=archive.extractfile(item).read()
if 'MANIFEST.json' not in files: raise SystemExit('missing archive manifest')
manifest=json.loads(files.pop('MANIFEST.json'))
if (not isinstance(manifest,dict) or set(manifest)!={'files','source_commit','colors','migrate_legacy'} or
    not isinstance(manifest['source_commit'],str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',manifest['source_commit']) or
    type(manifest['migrate_legacy']) is not bool):
 raise SystemExit('invalid archive manifest')
required={'agent.py','colors.py','config.py','palette.py','transactions.py','install.py','mosherdr.py','hooks/local.zsh','hooks/remote.zsh'}
if not required.issubset(files) or any(name not in required and not name.startswith('tests/test_') for name in files):
 raise SystemExit('incomplete or unexpected runtime')
if manifest['files']!={name:hashlib.sha256(value).hexdigest() for name,value in files.items()}:
 raise SystemExit('archive fingerprint mismatch')
with tempfile.TemporaryDirectory(prefix='mosherdr-') as temporary:
 stage=Path(temporary).resolve()
 for name,value in files.items():
  path=stage/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(value)
 sys.path.insert(0,str(stage))
 import install
 if not set(install.RUNTIME).issubset(files): raise SystemExit('incomplete runtime')
 install.receive(stage,manifest)
'''


def receive(source, manifest):
    verify(source)
    home = Path.home()
    changes, before, metadata = plan(home, "remote", runtime_payload(source), manifest["source_commit"],
                                    colors=manifest.get("colors"),
                                    migrate_legacy=manifest.get("migrate_legacy", False),
                                    configuration=config.config_path())
    apply_plan(home, changes, before, metadata)
    if not installation_status(home, configuration=config.config_path())["verified"]:
        raise InstallError("post-install verification failed")


def transfer(host, source, commit, colors, migrate):
    config.validate_host(host)
    encoded = base64.b64encode(BOOTSTRAP.encode()).decode("ascii")
    code = "import base64;exec(base64.b64decode(" + repr(encoded) + "))"
    remote_command = "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin python3 -B -c " + shlex.quote(code)
    if clean_commit(source) != commit:
        raise InstallError("source commit changed before transfer")
    archive = distribution(source, commit, colors, migrate)
    if clean_commit(source) != commit:
        raise InstallError("source changed while preparing transfer")
    result = run_bounded(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                          "-o", "ConnectTimeout=5", host, remote_command],
                         input=archive, timeout=180, cleanup_grace=4)
    if result.returncode:
        raise InstallError("remote installation failed; inspect both hosts' receipts before retrying")


def installation_status(home=None, configuration=None):
    try:
        default_configuration = config.config_path() if home is None else Path(home) / ".config/mosherdr/config.json"
        home = Path(home).absolute() if home is not None else Path.home()
        configuration = safe_path(configuration if configuration is not None else default_configuration)
        receipt = load_receipt(home)
        if not receipt:
            return {"installed": False, "verified": False}
        check_receipt(receipt, home / ".zshrc", configuration)
        return {"installed": True, "verified": True, "role": receipt["role"],
                "source_commit": receipt["source_commit"],
                "runtime_sha256": {name: digest((runtime_path(home)/name).read_bytes()) for name in RUNTIME}}
    except (InstallError, OSError, ValueError, KeyError, TypeError):
        return {"installed": True, "verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("local", "status"))
    parser.add_argument("--host")
    parser.add_argument("--foreground")
    parser.add_argument("--background")
    parser.add_argument("--palette-file", help="JSON file containing an array of 16 #RRGGBB colors")
    parser.add_argument("--migrate-legacy", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "status":
        result = installation_status()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["verified"] else 1
    config.validate_host(args.host)
    colors = None
    if args.palette_file is not None and (args.foreground is None or args.background is None):
        raise InstallError("--palette-file requires both --foreground and --background")
    if args.foreground is not None or args.background is not None:
        colors = {"enabled": True, "foreground": args.foreground, "background": args.background}
        if args.palette_file is not None:
            try:
                colors["palette"] = json.loads(Path(args.palette_file).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise InstallError("palette file must contain a JSON array of 16 #RRGGBB colors") from exc
        colors = config.validate_colors(colors)
    source = Path(__file__).resolve().parent
    commit = clean_commit(source)
    verify(source)
    if clean_commit(source) != commit:
        raise InstallError("source commit changed during verification")
    payload = runtime_payload(source, commit=commit)
    if clean_commit(source) != commit:
        raise InstallError("source changed while preparing runtime")
    home = Path.home()
    changes, before, metadata = plan(home, "local", payload, commit, host=args.host,
                                    colors=colors, migrate_legacy=args.migrate_legacy,
                                    configuration=config.config_path())
    dry_run = not args.apply
    apply_plan(home, changes, before, metadata, dry_run=dry_run)
    if dry_run:
        print("Remote transfer and verification will run with --apply.")
        return 0
    profile = config.load_config()["hosts"][args.host].get("colors", {"enabled": False})
    transfer(args.host, source, commit, profile, args.migrate_legacy)
    if not installation_status(home, configuration=config.config_path())["verified"]:
        raise InstallError("local post-install verification failed")
    print("Update complete on local and remote hosts.")
    print("Open a fresh local Herdr shell and an ordinary Mosh connection. Existing sessions were not restarted.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("mosherdr: installation cancelled", file=sys.stderr)
        sys.exit(130)
    except (InstallError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        print("mosherdr: " + str(exc), file=sys.stderr)
        sys.exit(1)
