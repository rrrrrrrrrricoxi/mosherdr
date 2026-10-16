import contextlib
import fcntl
import io
import itertools
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

import colors
from palette import CompatError, Target


TARGET = Target("/tmp/tmux-test/default", "%7", "$3", "/dev/pts/4",
                "xterm-256color", "256,RGB", "@2", 111, "Thu Oct 15 16:00:00 2026")
PROFILE = {"colors": {"enabled": True, "foreground": "#123456", "background": "#abcdef"}}


class ProfileTests(unittest.TestCase):
    def test_full_palette_reaches_bridge_arguments(self):
        values = ["#123456"] * 16
        self.assertEqual(colors.profile({"colors": {**PROFILE["colors"], "palette": values}}),
                         ("#123456", "#abcdef", tuple(values)))

    def test_disabled_or_absent_profile_never_discovers_or_starts(self):
        for data in ({}, {"colors": {"enabled": False}}):
            with mock.patch.object(colors, "discover") as discover:
                self.assertIsNone(colors.ensure({}, config=data))
            discover.assert_not_called()

    def test_explicit_complete_palette_only(self):
        self.assertEqual(colors.profile(PROFILE), ("#123456", "#abcdef"))
        for data in ({"colors": {"enabled": True}},
                     {"colors": {"enabled": True, "foreground": "red", "background": "#ffffff"}}):
            with self.assertRaises(CompatError):
                colors.profile(data)

    def test_unsupported_main_is_silent_and_successful(self):
        with mock.patch.object(colors, "ensure", side_effect=CompatError("unsupported")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(colors.main(["ensure"]), 0)
        self.assertEqual(out.getvalue(), "")

    def test_main_prints_only_the_acknowledged_capability_token(self):
        for result in ("palette", "truecolor"):
            with mock.patch.object(colors, "ensure", return_value=result), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(colors.main(["ensure"]), 0)
            self.assertEqual(out.getvalue(), result + "\n")


class StateTests(unittest.TestCase):
    def test_private_paths_and_lock_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = colors.private_directory(Path(temp) / "colors")
            root.chmod(0o755)
            colors.private_directory(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            lock, endpoint = colors.state_paths(root, TARGET)
            self.assertNotIn("%7", lock.name)
            fd = colors.open_lock(lock)
            try:
                self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
            finally:
                os.close(fd)
            self.assertEqual(endpoint.suffix, ".sock")

    def test_symlink_state_and_lock_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "link").symlink_to(root, target_is_directory=True)
            with self.assertRaises(CompatError):
                colors.private_directory(root / "link")
            (root / "lock").symlink_to(root / "unrelated")
            with self.assertRaises(OSError):
                colors.open_lock(root / "lock")
            with self.assertRaises(CompatError):
                colors.remove_socket(root / "lock")

    def test_state_home_must_be_absolute(self):
        with self.assertRaises(CompatError):
            colors.state_directory({"XDG_STATE_HOME": "relative"})


class EnsureTests(unittest.TestCase):
    def test_handshake_before_success_preserves_environment(self):
        environment = {"TMUX_PANE": "%7", "FORCE_COLOR": "1", "HTTPS_PROXY": "unchanged"}
        original = environment.copy()
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(colors, "discover", return_value=TARGET), mock.patch.object(colors, "start_owner", return_value=b"R\n") as start:
            self.assertEqual(colors.ensure(environment, PROFILE, temp), "truecolor")
        self.assertIs(start.call_args.args[1], environment)
        self.assertEqual(environment, original)

    def test_no_color_is_preserved_and_never_advertises_truecolor(self):
        for value in ("", "1"):
            environment = {"NO_COLOR": value}
            with tempfile.TemporaryDirectory() as temp, mock.patch.object(colors, "discover", return_value=TARGET), mock.patch.object(colors, "start_owner", return_value=b"R\n"):
                self.assertEqual(colors.ensure(environment, PROFILE, temp), "palette")
            self.assertEqual(environment, {"NO_COLOR": value})

    def test_existing_owner_is_revalidated_without_starting_another(self):
        with tempfile.TemporaryDirectory() as temp:
            lock, _ = colors.state_paths(Path(temp), TARGET)
            fd = colors.open_lock(lock)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with mock.patch.object(colors, "discover", return_value=TARGET), mock.patch.object(colors, "request", return_value=b"P\n") as request, mock.patch.object(colors, "start_owner") as start:
                    self.assertEqual(colors.ensure({}, PROFILE, temp), "palette")
                request.assert_called_once()
                start.assert_not_called()
            finally:
                os.close(fd)

    def test_stale_owner_fails_closed_and_does_not_signal_unknown_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            lock, _ = colors.state_paths(Path(temp), TARGET)
            fd = colors.open_lock(lock)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with mock.patch.object(colors, "discover", return_value=TARGET), mock.patch.object(colors, "request", return_value=b"") as request, mock.patch.object(colors, "start_owner") as start, mock.patch.object(colors.time, "sleep"):
                    with self.assertRaises(CompatError):
                        colors.ensure({}, PROFILE, temp)
                self.assertEqual(request.call_count, 2)
                start.assert_not_called()
            finally:
                os.close(fd)


class HandshakeTests(unittest.TestCase):
    def test_start_waits_for_ready_and_does_not_inherit_stdin(self):
        process = mock.Mock()

        def launch(argv, **kwargs):
            self.assertEqual(kwargs["stdin"], colors.subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"], colors.subprocess.DEVNULL)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(argv[-3], "_serve")
            os.write(int(argv[-1]), b"R\n")
            return process

        with tempfile.TemporaryFile() as lock:
            self.assertEqual(colors.start_owner(lock.fileno(), {}, launch), b"R\n")
        process.terminate.assert_not_called()

    def test_failed_handshake_terminates_only_just_created_helper(self):
        process = mock.Mock()
        with tempfile.TemporaryFile() as lock, mock.patch.object(colors, "read_ready", side_effect=CompatError("timeout")):
            with self.assertRaises(CompatError):
                colors.start_owner(lock.fileno(), {}, mock.Mock(return_value=process))
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    def test_closed_or_timed_out_handshake_is_not_success(self):
        read, write = os.pipe()
        try:
            with self.assertRaises(CompatError):
                colors.read_ready(read, timeout=0.001)
            os.close(write)
            write = None
            with self.assertRaises(CompatError):
                colors.read_ready(read)
        finally:
            os.close(read)
            if write is not None:
                os.close(write)


class LifetimeTests(unittest.TestCase):
    def serve_fixture(self, bridge, discovery, factory=None):
        with tempfile.TemporaryDirectory(prefix="mh-", dir="/tmp") as temp:
            environment = {"XDG_STATE_HOME": temp}
            directory = colors.private_directory(colors.state_directory(environment))
            lock_path, socket_path = colors.state_paths(directory, TARGET)
            lock = colors.open_lock(lock_path)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            read, write = os.pipe()
            # Managed sandbox prohibits AF_UNIX bind. The endpoint fixture keeps
            # real state files/locks/pipes but never opens a listening socket.
            endpoint = mock.Mock()
            endpoint.bind.side_effect = lambda path: Path(path).touch(mode=0o600)
            endpoint.accept.side_effect = socket.timeout()
            try:
                with mock.patch.object(colors, "profile", return_value=("#123456", "#abcdef")), mock.patch.object(colors, "discover", side_effect=discovery) as discover, mock.patch.object(colors.time, "monotonic", side_effect=itertools.count(0, 2)), mock.patch.object(colors.socket, "socket", return_value=endpoint), mock.patch.object(colors, "remove_socket", side_effect=lambda path: path.unlink(missing_ok=True)):
                    colors.serve(lock, write, environment, factory or mock.Mock(return_value=bridge))
                self.assertTrue(all(call.kwargs.get("owner") == TARGET for call in discover.call_args_list[1:]))
                payload = os.read(read, 3)
                self.assertFalse(socket_path.exists())
                endpoint.close.assert_called_once()
                # Reconnect can obtain the same lock after the old owner exits.
                new_lock = colors.open_lock(lock_path)
                try:
                    fcntl.flock(new_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(new_lock)
                return payload
            finally:
                os.close(read)

    def test_detach_or_pane_closure_closes_only_own_bridge(self):
        bridge = mock.Mock()
        payload = self.serve_fixture(bridge, [TARGET, TARGET, CompatError("detached")])
        self.assertEqual(payload, b"R\n")
        bridge.start.assert_called_once()
        bridge.close.assert_called_once()

    def test_client_reconnect_or_conflicting_viewer_invalidates_owner(self):
        bridge = mock.Mock()
        payload = self.serve_fixture(bridge, [TARGET, TARGET, CompatError("client replaced")])
        self.assertEqual(payload, b"R\n")
        bridge.close.assert_called_once()

    def test_return_to_running_tui_restarts_bridge_without_preexec(self):
        first, resumed = mock.Mock(), mock.Mock()
        factory = mock.Mock(side_effect=[first, resumed])
        payload = self.serve_fixture(first, [TARGET, TARGET, None, TARGET, TARGET,
                                             CompatError("detached")], factory)
        self.assertEqual(payload, b"R\n")
        self.assertEqual(factory.call_args_list, [mock.call(TARGET, "#123456", "#abcdef")] * 2)
        first.start.assert_called_once()
        first.close.assert_called_once()
        resumed.start.assert_called_once()
        resumed.close.assert_called_once()

    def test_start_failure_never_acknowledges_and_releases_state(self):
        bridge = mock.Mock()
        bridge.start.side_effect = CompatError("report rejected")
        self.assertEqual(self.serve_fixture(bridge, [TARGET]), b"")
        bridge.close.assert_called_once()

    def test_changed_target_before_ack_fails_closed(self):
        bridge = mock.Mock()
        self.assertEqual(self.serve_fixture(bridge, [TARGET, CompatError("changed")]), b"")
        bridge.close.assert_called_once()

    def test_bridge_exit_cleans_endpoint(self):
        bridge = mock.Mock()
        bridge.drain.side_effect = [None, CompatError("control exited")]
        self.assertEqual(self.serve_fixture(bridge, [TARGET, TARGET]), b"R\n")
        bridge.close.assert_called_once()

    def test_bridge_exit_during_startup_never_acknowledges(self):
        bridge = mock.Mock()
        bridge.drain.side_effect = CompatError("control exited")
        self.assertEqual(self.serve_fixture(bridge, [TARGET, TARGET]), b"")
        bridge.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
