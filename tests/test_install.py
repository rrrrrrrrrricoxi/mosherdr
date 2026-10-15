"""Installer fixtures use temporary homes; no SSH or live installation runs."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install
import transactions

COMMIT = "a" * 40


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve() / "home"
        self.home.mkdir()
        self.payload = {name: ("fixture " + name + "\n").encode() for name in install.RUNTIME}

    def write(self, path, data, mode=0o600):
        transactions.atomic(path, data, mode)
        return path

    def plan(self, role="local", **options):
        if role == "local":
            options.setdefault("host", "remote-box")
        return install.plan(self.home, role, self.payload, COMMIT, **options)

    def apply(self, planned=None):
        with contextlib.redirect_stdout(io.StringIO()):
            install.apply_plan(self.home, *(planned or self.plan()))

    def receipt(self):
        return json.loads((install.state_path(self.home) / "receipt.json").read_bytes())

    def test_fresh_and_idempotent_install_preserve_shell_and_private_backup(self):
        shell = self.home / ".zshrc"
        original = b"# unrelated proxy setup\nexport PROXY_PRESENT=1\n"
        self.write(shell, original, 0o640)
        self.apply()
        first = self.receipt()
        self.assertTrue(install.installation_status(self.home)["verified"])
        self.assertTrue(shell.read_bytes().startswith(original))
        self.assertEqual(stat.S_IMODE(shell.stat().st_mode), 0o640)
        self.assertEqual(shell.read_bytes().count(install.BEGIN), 1)
        backup = Path(first["backup"])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertIn(original, [p.read_bytes() for p in backup.iterdir()])
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in backup.iterdir()))
        self.apply()
        self.assertEqual(first, self.receipt())
        self.assertEqual(len(list(backup.parent.iterdir())), 1)
        self.assertEqual(set(first["files"]), {str(path) for path in self.plan()[0]})

    def test_dry_run_does_not_create_state_or_runtime(self):
        with contextlib.redirect_stdout(io.StringIO()):
            install.apply_plan(self.home, *self.plan(), dry_run=True)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_unrelated_shell_and_configuration_edits_are_preserved(self):
        self.apply()
        shell = self.home / ".zshrc"
        shell.write_bytes(b"# user edit\n" + shell.read_bytes())
        configuration = self.home / ".config/mosherdr/config.json"
        configuration.write_text(json.dumps({"version": 1, "hosts": {"other-host": {}, "remote-box": {}}}))
        self.apply()
        self.assertTrue(shell.read_bytes().startswith(b"# user edit\n"))
        self.assertIn("other-host", json.loads(configuration.read_bytes())["hosts"])

    def test_owned_runtime_shell_block_and_mode_drift_are_refused(self):
        self.apply()
        path = install.runtime_path(self.home) / "agent.py"
        original = path.read_bytes()
        path.write_bytes(b"user runtime edit")
        with self.assertRaises(install.InstallError):
            self.plan()
        self.assertFalse(install.installation_status(self.home)["verified"])
        path.write_bytes(original)
        shell = self.home / ".zshrc"
        shell.write_bytes(shell.read_bytes().replace(b"if [[ -o interactive ]]", b"if true"))
        with self.assertRaises(install.InstallError):
            self.plan()

    def test_pending_journal_blocks_fresh_and_existing_installations(self):
        pending = install.state_path(self.home) / "pending.json"
        self.write(pending, b"[]")
        with self.assertRaisesRegex(install.InstallError, "interrupted"):
            self.plan()
        pending.unlink()
        planned = self.plan()
        self.write(pending, b"[]")
        with self.assertRaisesRegex(install.InstallError, "interrupted"):
            self.apply(planned)

    def test_failure_leaves_journal_and_original_backup_for_manual_recovery(self):
        shell = self.home / ".zshrc"
        original = b"# keep user shell\n"
        self.write(shell, original)
        planned = self.plan()
        real_atomic = install.atomic
        def fail_runtime(path, *args):
            if path == install.runtime_path(self.home) / "agent.py":
                raise OSError("fixture write failure")
            return real_atomic(path, *args)
        with mock.patch.object(install, "atomic", side_effect=fail_runtime):
            with self.assertRaises(OSError):
                self.apply(planned)
        state = install.state_path(self.home)
        self.assertTrue((state / "pending.json").exists())
        self.assertFalse((state / "receipt.json").exists())
        self.assertEqual(shell.read_bytes(), original)
        self.assertIn(original, [p.read_bytes() for p in (state / "backups").glob("*/*")])
        with self.assertRaises(install.InstallError):
            self.plan()

    def test_collisions_do_not_adopt_executable_or_runtime(self):
        executable = self.write(self.home / ".local/bin/mosherdr", b"user launcher", 0o755)
        with self.assertRaisesRegex(install.InstallError, "collision"):
            self.plan()
        executable.unlink()
        self.write(install.runtime_path(self.home) / "agent.py", b"user runtime")
        with self.assertRaisesRegex(install.InstallError, "unowned"):
            self.plan()

    def test_unowned_shell_block_collision_is_refused(self):
        self.write(self.home / ".zshrc", install.BEGIN + b"user-owned\n" + install.END)
        with self.assertRaisesRegex(install.InstallError, "collision"):
            self.plan()

    def test_runtime_extra_file_and_symlink_are_not_reported_verified(self):
        self.apply()
        runtime = install.runtime_path(self.home)
        extra = runtime / "unowned.py"
        extra.write_bytes(b"preserve this user file")
        self.assertFalse(install.installation_status(self.home)["verified"])
        with self.assertRaisesRegex(install.InstallError, "unowned"):
            self.plan()
        extra.unlink()
        target = self.home / "unrelated-directory"
        target.mkdir()
        extra.symlink_to(target, target_is_directory=True)
        self.assertFalse(install.installation_status(self.home)["verified"])
        self.assertEqual(list(target.iterdir()), [])

    def test_configuration_identity_and_receipt_inventory_cannot_change(self):
        self.apply()
        alternate = self.home / "other-config.json"
        with self.assertRaisesRegex(install.InstallError, "identity"):
            self.plan(configuration=alternate)
        receipt = self.receipt()
        receipt["files"].pop(str(install.runtime_path(self.home) / "agent.py"))
        (install.state_path(self.home) / "receipt.json").write_text(json.dumps(receipt))
        with self.assertRaisesRegex(install.InstallError, "receipt paths"):
            self.plan()

    def test_edit_between_first_shell_read_and_final_baseline_is_refused(self):
        shell = self.write(self.home / ".zshrc", b"original\n")
        real_read = install.read_configuration
        def edit_then_read(*args):
            shell.write_bytes(b"concurrent shell edit\n")
            return real_read(*args)
        with mock.patch.object(install, "read_configuration", side_effect=edit_then_read):
            with self.assertRaisesRegex(install.InstallError, "concurrent"):
                self.plan()
        self.assertEqual(shell.read_bytes(), b"concurrent shell edit\n")

    def test_edit_after_configuration_read_is_refused(self):
        path = self.write(self.home / ".config/mosherdr/config.json", b'{"hosts":{}}')
        real_validate = install.config.validate
        count = 0
        def validate(data):
            nonlocal count
            count += 1
            if count == 2:
                path.write_bytes(b'{"hosts":{"concurrent-host":{}}}')
            return real_validate(data)
        with mock.patch.object(install.config, "validate", side_effect=validate):
            with self.assertRaisesRegex(install.InstallError, "concurrent"):
                self.plan()
        self.assertIn(b"concurrent-host", path.read_bytes())

    def test_edit_after_plan_and_receipt_change_before_apply_are_refused(self):
        shell = self.write(self.home / ".zshrc", b"original\n")
        planned = self.plan()
        shell.write_bytes(b"concurrent\n")
        with self.assertRaisesRegex(install.InstallError, "concurrent"):
            self.apply(planned)
        self.assertEqual(shell.read_bytes(), b"concurrent\n")
        self.apply()
        planned = self.plan()
        receipt_path = install.state_path(self.home) / "receipt.json"
        receipt_path.write_bytes(receipt_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(install.InstallError, "concurrent"):
            self.apply(planned)

    def test_symlink_lock_and_backups_cannot_modify_target(self):
        state = install.state_path(self.home)
        transactions.private_directory(state)
        target = self.write(self.home / "unrelated", b"untouched", 0o644)
        (state / "lock").symlink_to(target)
        with self.assertRaises((install.InstallError, OSError)):
            self.apply()
        self.assertEqual(target.read_bytes(), b"untouched")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        (state / "lock").unlink()
        outside = self.home / "outside-backups"
        outside.mkdir()
        (state / "backups").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((install.InstallError, OSError)):
            self.apply()
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_runtime_ancestor_and_hardlinked_lock_are_refused(self):
        outside = self.home / "outside"
        outside.mkdir()
        (self.home / ".local").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(install.InstallError):
            self.plan()
        (self.home / ".local").unlink()
        state = install.state_path(self.home)
        transactions.private_directory(state)
        target = self.write(self.home / "shared-file", b"preserve", 0o600)
        os.link(target, state / "lock")
        with self.assertRaisesRegex(install.InstallError, "unsafe installation lock"):
            self.apply()
        self.assertEqual(target.read_bytes(), b"preserve")

    def legacy(self, role):
        state = self.home / ".local/state/herdr-mosh-plain-install"
        shell = self.home / ".zshrc"
        files = {}
        receipt = {"role": "macbook" if role == "local" else "ccc", "files": files}
        proxy = self.write(self.home / ".local/bin/proxy-fixture", b"#!/bin/sh\n# unrelated proxy\n", 0o755)
        if role == "local":
            block = install.LEGACY_BEGIN + b"source owned-legacy-hook\n" + install.LEGACY_END
            self.write(shell, b"# preserve local shell\n" + block)
            receipt["zshrc_block_sha256"] = install.digest(block)
            paths = [shell, self.write(self.home / "Code/codex-setup/tools/herdr-mosh-compat/mosh.zsh", b"legacy source")]
        else:
            self.write(shell, b"# preserve remote shell\n")
            rgb = b"# BEGIN herdr-mosh-compat RGB\nset-option -as terminal-features ',xterm-256color:RGB'\n# END herdr-mosh-compat RGB\n"
            tmux = self.write(self.home / ".tmux.conf", b"# preserve tmux\n" + rgb)
            receipt.update(tmux_config=str(tmux), tmux_block_sha256=install.digest(rgb))
            launcher = self.write(self.home / ".local/bin/codex", b"#!/bin/sh\n# legacy dispatcher\n", 0o755)
            downstream = self.write(state / "downstream-codex", b"#!/bin/sh\n# original proxy launcher\n", 0o700)
            paths = [tmux, launcher, downstream, self.write(state / "runtime/dispatch.py", b"legacy dispatcher")]
        for path in paths:
            files[str(path)] = install.fingerprint(path)
        receipt_path = self.write(state / "receipt.json", json.dumps(receipt).encode())
        return receipt_path, proxy

    def test_legacy_local_migration_preserves_receipt_source_and_proxy(self):
        receipt_path, proxy = self.legacy("local")
        original_receipt, proxy_bytes = receipt_path.read_bytes(), proxy.read_bytes()
        with self.assertRaisesRegex(install.InstallError, "legacy hook"):
            self.plan()
        self.apply(self.plan(migrate_legacy=True))
        self.assertNotIn(install.LEGACY_BEGIN, (self.home / ".zshrc").read_bytes())
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        self.assertEqual(proxy.read_bytes(), proxy_bytes)
        self.assertEqual(self.receipt()["legacy_receipt_sha256"], install.digest(original_receipt))
        self.apply(self.plan(migrate_legacy=True))

    def test_legacy_remote_restores_launcher_once_preserves_rgb_and_old_receipt(self):
        receipt_path, proxy = self.legacy("remote")
        original_receipt = receipt_path.read_bytes()
        tmux = self.home / ".tmux.conf"
        original_tmux, proxy_bytes = tmux.read_bytes(), proxy.read_bytes()
        downstream = receipt_path.parent / "downstream-codex"
        original_launcher = downstream.read_bytes()
        self.apply(self.plan("remote", migrate_legacy=True))
        launcher = self.home / ".local/bin/codex"
        self.assertEqual(launcher.read_bytes(), original_launcher)
        receipt = self.receipt()
        self.assertNotIn(str(launcher), receipt["files"])
        self.assertIn(str(launcher), receipt["legacy_restored_files"])
        launcher.write_bytes(b"# legitimate later Codex launcher edit\n")
        self.apply(self.plan("remote", migrate_legacy=True))
        self.assertEqual(launcher.read_bytes(), b"# legitimate later Codex launcher edit\n")
        self.assertEqual(tmux.read_bytes(), original_tmux)
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        self.assertEqual(downstream.read_bytes(), original_launcher)
        self.assertEqual(proxy.read_bytes(), proxy_bytes)
        self.assertTrue(install.installation_status(self.home)["verified"])

    def test_legacy_drift_and_concurrent_downstream_edits_refuse_restore(self):
        receipt_path, _ = self.legacy("remote")
        downstream = receipt_path.parent / "downstream-codex"
        original = downstream.read_bytes()
        downstream.write_bytes(b"drift")
        with self.assertRaisesRegex(install.InstallError, "legacy installed file drift"):
            self.plan("remote", migrate_legacy=True)
        downstream.write_bytes(original)
        real_snapshot = install.observed_snapshot
        changed = False
        def edit_after_validation(path, observed=None):
            nonlocal changed
            result = real_snapshot(path, observed)
            if Path(path) == downstream and not changed:
                changed = True
                downstream.write_bytes(b"concurrent preserved launcher edit")
            return result
        with mock.patch.object(install, "observed_snapshot", side_effect=edit_after_validation):
            with self.assertRaisesRegex(install.InstallError, "concurrent"):
                self.plan("remote", migrate_legacy=True)
        self.assertEqual(downstream.read_bytes(), b"concurrent preserved launcher edit")

    def test_config_path_relocation_is_not_reported_as_verified(self):
        self.apply()
        path = install.state_path(self.home) / "receipt.json"
        receipt = self.receipt()
        old = receipt["configuration"]
        relocated = self.home / "relocated.json"
        relocated.write_bytes(Path(old).read_bytes())
        receipt["files"][str(relocated)] = receipt["files"].pop(old)
        receipt["configuration"] = str(relocated)
        path.write_text(json.dumps(receipt))
        self.assertFalse(install.installation_status(self.home)["verified"])

    def test_concurrent_edit_after_runtime_write_is_not_adopted_by_receipt(self):
        planned = self.plan()
        target = install.runtime_path(self.home) / "agent.py"
        real_atomic = install.atomic
        def edit_after_write(path, *args):
            result = real_atomic(path, *args)
            if Path(path) == target:
                target.write_bytes(b"concurrent runtime edit")
            return result
        with mock.patch.object(install, "atomic", side_effect=edit_after_write):
            with self.assertRaisesRegex(install.InstallError, "concurrent installed"):
                self.apply(planned)
        self.assertFalse((install.state_path(self.home) / "receipt.json").exists())
        self.assertTrue((install.state_path(self.home) / "pending.json").exists())
        self.assertEqual(target.read_bytes(), b"concurrent runtime edit")


class TransactionTests(unittest.TestCase):
    def test_atomic_parent_swap_cannot_redirect_writes_through_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            owned, outside = root / "owned", root / "outside"
            owned.mkdir()
            outside.mkdir()
            (outside / "file").write_bytes(b"unrelated")
            real_replace = transactions.os.replace
            def swap_then_replace(*args, **kwargs):
                owned.rename(root / "old-owned")
                owned.symlink_to(outside, target_is_directory=True)
                return real_replace(*args, **kwargs)
            with mock.patch.object(transactions.os, "replace", side_effect=swap_then_replace):
                transactions.atomic(owned / "file", b"installation", 0o600)
            self.assertEqual((outside / "file").read_bytes(), b"unrelated")
            self.assertEqual((root / "old-owned/file").read_bytes(), b"installation")

    def test_snapshot_rejects_file_replacement_during_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "file"
            path.write_bytes(b"original")
            real_fstat = transactions.os.fstat
            count = 0
            def replace_before_second_stat(descriptor):
                nonlocal count
                count += 1
                if count == 2:
                    replacement = path.with_name("replacement")
                    replacement.write_bytes(b"other content")
                    replacement.replace(path)
                return real_fstat(descriptor)
            with mock.patch.object(transactions.os, "fstat", side_effect=replace_before_second_stat):
                with self.assertRaisesRegex(transactions.InstallError, "concurrent"):
                    transactions.snapshot(path)
            self.assertEqual(path.read_bytes(), b"other content")


class ArchiveTests(unittest.TestCase):
    def archive(self, changes=None, extra=None):
        payload = {name: b"# fixture\n" for name in install.RUNTIME}
        # The receiver stub reports acceptance without touching a home or system.
        payload["install.py"] = ("RUNTIME=" + repr(install.RUNTIME) + "\ndef receive(source, manifest):\n print('fixture accepted '+manifest['source_commit'])\n").encode()
        payload["tests/test_fixture.py"] = b"# fixture test\n"
        payload.update(changes or {})
        manifest = {"files": {name: install.digest(value) for name, value in payload.items()},
                    "source_commit": COMMIT, "colors": None, "migrate_legacy": False}
        payload["MANIFEST.json"] = json.dumps(manifest).encode()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, value in payload.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b"x" * extra.size) if extra.isfile() else None)
        return output.getvalue()

    def receive(self, archive):
        return subprocess.run([sys.executable, "-B", "-c", install.BOOTSTRAP], input=archive,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                              cwd="/")

    def test_accepts_complete_matching_archive_with_mock_receiver(self):
        result = self.receive(self.archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ("fixture accepted " + COMMIT + "\n").encode())

    def test_rejects_traversal_duplicate_symlink_and_unexpected_module(self):
        for name, kind in (("../escape.py", tarfile.REGTYPE), ("agent.py", tarfile.REGTYPE),
                           ("hooks/other.zsh", tarfile.REGTYPE), ("extra.py", tarfile.REGTYPE),
                           ("tests/test_link.py", tarfile.SYMTYPE)):
            info = tarfile.TarInfo(name)
            info.type, info.size, info.linkname = kind, 0, "/tmp/never-follow"
            with self.subTest(name=name):
                result = self.receive(self.archive(extra=info))
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"fixture accepted", result.stdout)

    def test_rejects_checksum_mismatch_missing_runtime_and_oversize_input(self):
        archive = self.archive()
        self.assertIn(b"# fixture\n", archive)
        result = self.receive(archive.replace(b"# fixture\n", b"# changed\n", 1))
        self.assertNotEqual(result.returncode, 0)
        result = self.receive(b"x" * (4 * 1024 * 1024 + 1))
        self.assertNotEqual(result.returncode, 0)
        output = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source, tarfile.open(fileobj=output, mode="w") as target:
            for info in source:
                if info.name != "agent.py":
                    target.addfile(info, source.extractfile(info))
        result = self.receive(output.getvalue())
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"fixture accepted", result.stdout)

    def test_local_payload_and_archive_use_commit_objects(self):
        def objects(source, commit, name=None):
            self.assertEqual(commit, COMMIT)
            if name is None:
                return b"tests/test_committed.py\ntests/not_a_test.py\nignored.py\n"
            return ("committed " + name).encode()
        with mock.patch.object(install, "git_bytes", side_effect=objects):
            payload = install.runtime_payload(Path("/not-a-working-tree"), commit=COMMIT)
            archive = install.distribution(Path("/not-a-working-tree"), COMMIT, None, False)
        self.assertEqual(payload["agent.py"], b"committed agent.py")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as packed:
            self.assertEqual(packed.extractfile("agent.py").read(), b"committed agent.py")
            self.assertIn("tests/test_committed.py", packed.getnames())
            self.assertNotIn("ignored.py", packed.getnames())
            self.assertNotIn("tests/not_a_test.py", packed.getnames())
            manifest = json.load(packed.extractfile("MANIFEST.json"))
            self.assertEqual(manifest["source_commit"], COMMIT)
            self.assertEqual(manifest["files"]["agent.py"], install.digest(b"committed agent.py"))

    def test_transfer_uses_strict_batch_ssh_and_checks_source_before_and_after_packaging(self):
        with mock.patch.object(install, "clean_commit", side_effect=[COMMIT, COMMIT]) as clean, \
                mock.patch.object(install, "distribution", return_value=b"archive"), \
                mock.patch.object(install, "run_bounded", return_value=subprocess.CompletedProcess([], 0)) as run:
            install.transfer("remote-box", Path("/fixture"), COMMIT, None, False)
        self.assertEqual(clean.call_count, 2)
        argv = run.call_args.args[0]
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[-2], "remote-box")
        self.assertEqual(run.call_args.kwargs["input"], b"archive")
        with mock.patch.object(install, "clean_commit", side_effect=[COMMIT, "b" * 40]), \
                mock.patch.object(install, "distribution", return_value=b"archive"), \
                mock.patch.object(install, "run_bounded") as run:
            with self.assertRaises(install.InstallError):
                install.transfer("remote-box", Path("/fixture"), COMMIT, None, False)
        run.assert_not_called()

    def test_dirty_checkout_and_test_failure_refuse_installation(self):
        with mock.patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b" M file\n")):
            with self.assertRaisesRegex(install.InstallError, "clean, committed"):
                install.clean_commit(Path("/fixture"))
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(install, "run_bounded", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(install.InstallError, "tests failed"):
                install.verify(Path("/fixture"))


if __name__ == "__main__":
    unittest.main()
