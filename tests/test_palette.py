import argparse
import io
import os
import subprocess
import unittest
from unittest import mock

import palette as helper


TARGET = helper.Target("/tmp/tmux-test/default", "%7", "$3", "/dev/ttys010", "xterm", "256,RGB")


def guards(marker, error=False):
    return (b"%begin 100 11 0\n%end 100 11 0\n"
            b"%begin 100 15 0\n" +
            (b"%error 100 15 0\n" if error else b"%end 100 15 0\n") +
            b"%begin 100 19 0\n" + marker.encode() +
            b"\n%end 100 19 0\n%begin 100 20 0\n%end 100 20 0\n")


class FakeProcess:
    """Real anonymous pipes exercise selectors without a tmux server or PTY."""
    def __init__(self, payload):
        self.stdin = io.BytesIO()
        self.stdout_read, self.stdout_write = os.pipe()
        self.stderr_read, self.stderr_write = os.pipe()
        self.stdout = os.fdopen(self.stdout_read, "rb", buffering=0)
        self.stderr = os.fdopen(self.stderr_read, "rb", buffering=0)
        if payload:
            os.write(self.stdout_write, payload)
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.stdin.closed and self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def dispose(self):
        os.close(self.stdout_write)
        os.close(self.stderr_write)
        self.stdout.close()
        self.stderr.close()


class ProtocolTests(unittest.TestCase):
    def test_success_and_wait_end_are_accepted_in_fragments(self):
        protocol = helper.Protocol("nonce")
        payload = guards("nonce")
        for i in range(0, len(payload), 7):
            protocol.feed(payload[i:i + 7])
        self.assertTrue(protocol.ready)
        self.assertEqual(protocol.successes, 4)

    def test_hook_guards_and_noncontiguous_numbers_are_allowed(self):
        protocol = helper.Protocol("nonce")
        hook = b"%begin 100 14 0\nignored hook content\n%end 100 14 0\n"
        protocol.feed(guards("nonce").replace(b"%begin 100 15", hook + b"%begin 100 15"))
        self.assertTrue(protocol.ready)

    def test_error_aborts_before_ready(self):
        with self.assertRaises(helper.CompatError):
            helper.Protocol("nonce").feed(guards("nonce", error=True))

    def test_hook_error_aborts(self):
        with self.assertRaises(helper.CompatError):
            helper.Protocol("nonce").feed(b"%begin 100 1 0\n%error 100 1 0\n")

    def test_marker_and_reports_alone_are_not_ready(self):
        protocol = helper.Protocol("nonce")
        protocol.feed(guards("nonce").split(b"%begin 100 20")[0])
        self.assertFalse(protocol.ready)

    def test_spoofed_notification_marker_is_not_acknowledged(self):
        protocol = helper.Protocol("nonce")
        protocol.feed(b"nonce\n%begin 1 1 0\n%end 1 1 0\n")
        self.assertFalse(protocol.ready)

    def test_mismatched_guard_and_nested_begin_rejected(self):
        for payload in (b"%begin 1 1 0\n%end 1 2 0\n", b"%begin 1 1 0\n%begin 1 2 0\n"):
            with self.subTest(payload=payload), self.assertRaises(helper.CompatError):
                helper.Protocol("nonce").feed(payload)

    def test_exit_and_overlong_line_rejected(self):
        for payload in (b"%exit\n", b"x" * 65537, b"x" * 65537 + b"\n"):
            with self.subTest(size=len(payload)), self.assertRaises(helper.CompatError):
                helper.Protocol("nonce").feed(payload)


BIRTH = "Thu Oct 15 16:00:00 2026"
PARENTS = {111: (112, "/usr/bin/tmux", BIRTH), 112: (1, "/usr/bin/mosh-server", BIRTH)}
ENV = {"TMUX": "/tmp/path,with,commas/default,123,0", "TMUX_PANE": "%7"}
CLIENT = "/dev/ttys010\t0\t$3\txterm\t256,RGB\t111\t@2\t%7"


class DiscoveryTests(unittest.TestCase):
    def discover(self, clients=CLIENT, facts="%7\t$3\t@2", parents=PARENTS):
        return helper.discover(ENV, read=mock.Mock(side_effect=[facts, clients]), parents=parents)

    def test_exact_pane_socket_session_and_process(self):
        read = mock.Mock(side_effect=["%7\t$3\t@2", CLIENT])
        target = helper.discover(ENV, read=read, parents=PARENTS)
        self.assertEqual(target.pane, "%7")
        self.assertEqual(target.session, "$3")
        self.assertEqual(target.client, "/dev/ttys010")
        self.assertEqual(target.socket, "/tmp/path,with,commas/default")
        self.assertEqual(target.window, "@2")
        self.assertEqual(target.pid, 111)
        self.assertEqual(target.birth, BIRTH)
        self.assertNotIn("-t", read.call_args_list[1].args[1])

    def test_missing_tmux_or_bad_pane_does_not_query(self):
        for env in ({}, {"TMUX": "/tmp/s,1,0", "TMUX_PANE": "%7;kill-server"}):
            read = mock.Mock()
            with self.assertRaises(helper.CompatError):
                helper.discover(env, read=read)
            read.assert_not_called()

    def test_rejects_wrong_pane_window_session_selection_and_missing_pid(self):
        cases = [(CLIENT, "%8\t$3\t@2"), (CLIENT.replace("@2", "@3"), "%7\t$3\t@2"),
                 (CLIENT.replace("$3", "$4"), "%7\t$3\t@2"),
                 (CLIENT.replace("\t%7", "\t%8"), "%7\t$3\t@2"),
                 (CLIENT.replace("\t111", "\t"), "%7\t$3\t@2")]
        for client, facts in cases:
            with self.subTest(client=client), self.assertRaises(helper.CompatError):
                self.discover(client, facts)

    def test_mosh_must_be_ancestor_of_exact_visual_client(self):
        for parents in ({111: (1, "tmux"), 999: (1, "mosh-server")},
                        {111: (112, "tmux"), 112: (111, "tmux")},
                        {111: (112, "tmux"), 112: (1, "not-mosh-server")}):
            with self.subTest(parents=parents), self.assertRaises(helper.CompatError):
                self.discover(parents=parents)

    def test_multiple_viewers_fail_closed_including_linked_session_or_non_mosh(self):
        others = [CLIENT.replace("ttys010", "ttys011"),
                  CLIENT.replace("$3", "$4").replace("111", "222"),
                  CLIENT.replace("111", "222").replace("%7", "%8")]
        for other in others:
            with self.subTest(other=other), self.assertRaises(helper.CompatError):
                self.discover(CLIENT + "\n" + other)

    def test_control_clients_and_other_windows_do_not_conflict(self):
        control = "/control\t1\t$3\t\t\t777\t@2\t%7"
        other = CLIENT.replace("@2", "@8").replace("111", "222")
        self.assertEqual(self.discover(CLIENT + "\n" + control + "\n" + other).pid, 111)

    def test_truecolor_uses_capability_not_term_name(self):
        self.assertTrue(helper.truecolor(self.discover()))
        self.assertFalse(helper.truecolor(self.discover(CLIENT.replace("256,RGB", "256"))))
        self.assertFalse(helper.truecolor(self.discover(CLIENT.replace("256,RGB", "256,notRGB"))))

    def test_process_scan_does_not_request_arguments_or_environment(self):
        result = subprocess.CompletedProcess([], 0, b"111 112 Thu Oct 15 16:00:00 2026 /usr/bin/tmux\n112 1 Thu Oct 15 16:00:00 2026 /usr/bin/mosh-server\n", b"")
        runner = mock.Mock(return_value=result)
        self.assertEqual(helper.process_parents(runner), PARENTS)
        self.assertEqual(runner.call_args.args[0], ["ps", "-axo", "pid=,ppid=,lstart=,comm="])
        self.assertEqual(runner.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_owner_can_wait_for_original_client_to_return_from_other_pane_window_or_session(self):
        target = self.discover()
        for other in (CLIENT.replace("%7", "%8"), CLIENT.replace("@2", "@9"),
                      CLIENT.replace("$3", "$4"), CLIENT + "\n" + CLIENT.replace("ttys010", "ttys011")):
            with self.subTest(other=other):
                read = mock.Mock(side_effect=["%7\t$3\t@2", other])
                self.assertIsNone(helper.discover(ENV, read=read, parents=PARENTS, owner=target))
        read = mock.Mock(side_effect=["%7\t$3\t@2", CLIENT])
        self.assertEqual(helper.discover(ENV, read=read, parents=PARENTS, owner=target), target)

    def test_dormant_owner_rejects_pid_reuse_detach_and_pane_replacement(self):
        target = self.discover()
        reused = dict(PARENTS)
        reused[111] = (112, "/usr/bin/tmux", "Thu Oct 15 17:00:00 2026")
        for facts, clients, parents in (("%7\t$3\t@2", CLIENT, reused),
                                        ("%7\t$3\t@2", "", PARENTS),
                                        ("%7\t$3\t@9", CLIENT, PARENTS),
                                        ("%7\t$3\t@2", CLIENT.replace("111", "333"), PARENTS)):
            with self.subTest(facts=facts, clients=clients), self.assertRaises(helper.CompatError):
                helper.discover(ENV, read=mock.Mock(side_effect=[facts, clients]), parents=parents, owner=target)

    def test_tmux_queries_never_start_server(self):
        result = subprocess.CompletedProcess([], 0, b"%7\t$3\n", b"")
        with mock.patch.object(helper.subprocess, "run", return_value=result) as run:
            helper.tmux_read("/tmp/existing", ["display-message", "-p", "#{pane_id}"])
        self.assertEqual(run.call_args.args[0][:4], ["tmux", "-N", "-S", "/tmp/existing"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)


class BridgeTests(unittest.TestCase):
    def test_start_ack_and_cleanup_only_owned_waiter(self):
        bridge = helper.Bridge(TARGET, "#abb2bf", "#282c34")
        process = FakeProcess(guards(bridge.channel))
        try:
            with mock.patch.object(helper.subprocess, "Popen", return_value=process) as popen:
                bridge.start()
            self.assertTrue(bridge.protocol.ready)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            argv = popen.call_args.args[0]
            self.assertEqual(argv[:5], ["tmux", "-C", "-N", "-S", TARGET.socket])
            self.assertEqual(argv.count(";"), 3)
            self.assertIn("%7:\x1b]10;rgb:abab/b2b2/bfbf\x1b\\", argv)
            self.assertNotIn("attach-session", argv)
            with mock.patch.object(helper.subprocess, "run") as run:
                bridge.close()
            self.assertEqual(run.call_args.args[0], ["tmux", "-N", "-S", TARGET.socket,
                                                    "wait-for", "-S", bridge.channel])
            self.assertTrue(process.stdin.closed)
        finally:
            process.dispose()

    def test_timeout_and_protocol_error_cleanup(self):
        for kind in ("timeout", "error"):
            bridge = helper.Bridge(TARGET, "#ffffff", "#000000")
            process = FakeProcess(guards(bridge.channel, True) if kind == "error" else b"")
            try:
                with mock.patch.object(helper.subprocess, "Popen", return_value=process), self.assertRaises(helper.CompatError):
                    bridge.start(timeout=0.01)
                with mock.patch.object(helper.subprocess, "run"):
                    bridge.close()
                self.assertTrue(process.stdin.closed)
            finally:
                process.dispose()

    def test_early_process_exit_prevents_ready(self):
        bridge = helper.Bridge(TARGET, "#ffffff", "#000000")
        process = FakeProcess(guards(bridge.channel))
        process.returncode = 1
        try:
            with mock.patch.object(helper.subprocess, "Popen", return_value=process), self.assertRaises(helper.CompatError):
                bridge.start()
        finally:
            with mock.patch.object(helper.subprocess, "run"):
                bridge.close()
            process.dispose()

    def test_cleanup_escalates_only_owned_client_with_bounded_waits(self):
        bridge = helper.Bridge(TARGET, "#123456", "#abcdef")
        process = mock.Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1),
                                    subprocess.TimeoutExpired("fixture", 1), 0]
        bridge.process = process
        with mock.patch.object(helper.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            bridge.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=1)] * 3)
        self.assertIsNone(bridge.process)


class PanePaletteTests(unittest.TestCase):
    def test_reports_ready_before_start_and_removes_own_array(self):
        values = tuple("#%06x" % i for i in range(16))
        owner = helper.PanePalette(TARGET, values)
        rendered = "\n".join(f"pane-colours[{i}] {v}" for i, v in enumerate(values))
        with mock.patch.object(helper, "tmux_read", side_effect=["", "pane-colours", "", rendered, ""]) as read:
            owner.start()
            self.assertEqual(owner.owned, dict(enumerate(values)))
            owner.close()
        self.assertEqual(read.call_args.args[1], ["set-option", "-pu", "-t", "%7", "pane-colours"])

    def test_restores_preexisting_empty_local_array(self):
        owner = helper.PanePalette(TARGET, ("#ffffff",))
        with mock.patch.object(helper, "tmux_read", side_effect=["pane-colours", "pane-colours", "", "pane-colours[0] #ffffff", ""]) as read:
            owner.start()
            owner.close()
        self.assertEqual(read.call_args.args[1], ["set-option", "-p", "-t", "%7", "pane-colours", ""])

    def test_preserves_inherited_palette_without_shadowing(self):
        owner = helper.PanePalette(TARGET, ("#ffffff",) * 16)
        with mock.patch.object(helper, "tmux_read", return_value="pane-colours[200]* red") as read:
            owner.start()
            owner.close()
        self.assertEqual(read.call_count, 2)

    def test_preserves_later_user_change(self):
        owner = helper.PanePalette(TARGET, ("#000000", "#ffffff"))
        with mock.patch.object(helper, "tmux_read", side_effect=["", "pane-colours", "", "pane-colours[0] #112233\npane-colours[1] #ffffff", ""]) as read:
            owner.start()
            owner.close()
        self.assertEqual(read.call_args.args[1][-1], "pane-colours[1]")

    def test_partial_start_failure_can_be_cleaned(self):
        owner = helper.PanePalette(TARGET, ("#000000", "#ffffff"))
        with mock.patch.object(helper, "tmux_read", side_effect=["", "pane-colours", helper.CompatError("failed"), "pane-colours[0] #000000", ""]) as read:
            with self.assertRaises(helper.CompatError):
                owner.start()
            owner.close()
        self.assertEqual(read.call_args.args[1][-1], "pane-colours[0]")


class ColorTests(unittest.TestCase):
    def test_rgb_validation_and_exact_default_reports(self):
        self.assertEqual(helper.rgb("#AbB2bF"), "#abb2bf")
        self.assertEqual(helper.colour_report("%7", 10, "#123456"), "%7:\x1b]10;rgb:1212/3434/5656\x1b\\")
        self.assertEqual(helper.colour_report("%7", 11, "#abcdef"), "%7:\x1b]11;rgb:abab/cdcd/efef\x1b\\")
        for value in ("red", "#fff", "#123456;foo", "123456", "#gggggg"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                helper.rgb(value)


if __name__ == "__main__":
    unittest.main()
