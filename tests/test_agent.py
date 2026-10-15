"""Boundary fixtures: no live Herdr IPC, ssh, tmux server, or terminal reads."""
import json
import ctypes
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent as dynamic


IDENTITY = ("terminal-1", 200, 200, "192.0.2.2", 60001)
CONNECTION = [301, "Thu Oct 15 10:00:00 2026", 300, "Thu Oct 15 09:59:59 2026"]
# macOS ps -axww -o pid=,ppid=,pgid=,tpgid=,stat=,comm=
PROCESSES = """1 0 1 -1 Ss /sbin/launchd
300 1 300 -1 Ss /opt/homebrew/bin/mosh-server
301 300 301 301 S+ /opt/homebrew/bin/tmux
400 1 400 500 Ss /bin/zsh
500 400 500 500 S+ /opt/homebrew/bin/codex
"""
CLIENT = "301\t0\t$1\t@2\t%3"
PANE = "%3\t@2\t0\t400\t0"


class ProbeTests(unittest.TestCase):
    def probe(self, *, rows=CLIENT, processes=PROCESSES, pane=PANE,
              sockets="p300\nn*:60001", last=None, argv=None):
        listed = 0
        def command(args, timeout=3):
            nonlocal listed
            if args[0] == "tmux":
                if "list-clients" in args:
                    listed += 1
                    return last if listed > 1 and last is not None else rows
                self.assertIn("%3", args)
                return pane
            if args[0] == "lsof":
                self.assertEqual(args[1:], ["-nP", "-a", "-p", "300", "-iUDP", "-Fn"])
                return sockets
            if "-p" in args:
                return "301 Thu Oct 15 10:00:00 2026\n300 Thu Oct 15 09:59:59 2026"
            return processes
        with mock.patch.object(dynamic, "command", side_effect=command), \
                mock.patch.object(dynamic, "tool", side_effect=lambda name: name), \
                mock.patch.object(dynamic, "argv_for", return_value=argv or ["/bin/zsh"]), \
                mock.patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.1 55000 192.0.2.2 22"}):
            return dynamic.probe("192.0.2.2", 60001)

    def test_exact_visible_foreground_agent(self):
        self.assertEqual(self.probe(), {"agent": "codex", "connection": CONNECTION})

    def test_bare_shell_background_suspended_and_hidden_agents_are_unlisted(self):
        cases = [PROCESSES.replace("400 1 400 500", "400 1 400 400"),
                 PROCESSES.replace("500 400 500 500 S+", "500 400 501 500 S"),
                 PROCESSES.replace("500 400 500 500 S+", "500 400 500 500 T"),
                 PROCESSES.replace("500 400 500 500 S+", "500 999 500 500 S+")]
        for processes in cases:
            with self.subTest(processes=processes):
                self.assertIsNone(self.probe(processes=processes)["agent"])

    def test_claude_pi_and_node_pi(self):
        for executable, expected in (("claude", "claude"), ("claude-code", "claude"), ("pi", "pi")):
            self.assertEqual(self.probe(processes=PROCESSES.replace("/codex", "/" + executable))["agent"], expected)
        self.assertEqual(self.probe(processes=PROCESSES.replace("/codex", "/node"),
            argv=["node", "/x/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"])["agent"], "pi")

    def test_control_clients_and_unrelated_clients_do_not_hide_matching_connection(self):
        rows = CLIENT + "\n999\t1\t$1\t@2\t%3\n999\t0\t$8\t@8\t%8"
        self.assertEqual(self.probe(rows=rows)["agent"], "codex")

    def test_two_matching_visual_clients_are_ambiguous(self):
        processes = PROCESSES + "302 300 302 302 S+ /opt/homebrew/bin/tmux\n"
        with self.assertRaises(dynamic.Unavailable):
            self.probe(rows=CLIENT + "\n302\t0\t$1\t@2\t%3", processes=processes)

    def test_tmux_session_switch_keeps_same_connection(self):
        self.assertEqual(self.probe(rows=CLIENT.replace("$1", "$9"), pane=PANE.replace("$1", "$9"))["connection"], CONNECTION)

    def test_wrong_port_interface_ancestry_pane_or_changed_visibility_fail_closed(self):
        cases = [dict(sockets="p300\nn*:60002"), dict(sockets="p300\nn192.0.2.3:60001"),
                 dict(processes=PROCESSES.replace("301 300", "301 1")),
                 dict(pane=PANE[:-1] + "1"),
                 dict(last=CLIENT.replace("%3", "%4"))]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(dynamic.Unavailable):
                self.probe(**options)

    def test_wrong_ssh_server_address_fails_before_process_queries(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "198.51.100.4 500 10.0.0.1 22"}), \
                mock.patch.object(dynamic, "command") as command:
            with self.assertRaises(dynamic.Unavailable):
                dynamic.probe("10.0.0.2", 60001)
            command.assert_not_called()

    def test_two_foreground_agent_kinds_are_ambiguous(self):
        with self.assertRaises(dynamic.Unavailable):
            self.probe(processes=PROCESSES + "501 400 500 500 S+ /bin/claude\n")


class ClassifierTests(unittest.TestCase):
    def test_darwin_kernel_argv_layout_stops_at_argc(self):
        raw = ((2).to_bytes(4, sys.byteorder, signed=True) + b"/bin/node\0\0\0" +
               b"node\0/path with spaces/pi.js\0PRIVATE_KEY=not-an-argument\0")
        def sysctl(mib, count, buffer, size, new, new_size):
            ctypes.memmove(buffer, raw, len(raw))
            ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = len(raw)
            return 0
        libc = mock.Mock()
        libc.sysctl.side_effect = sysctl
        with mock.patch.object(dynamic.sys, "platform", "darwin"), \
                mock.patch.object(dynamic.ctypes, "CDLL", return_value=libc):
            self.assertEqual(dynamic.argv_for(500), ["node", "/path with spaces/pi.js"])

    def test_exact_labels_only(self):
        for value, expected in (("Claude-Code.exe", "claude"), ("pi.js", "pi"),
                                ("codex", "codex"), ("pipeline", None), ("my-codex", None),
                                ("pi.js.exe", None)):
            self.assertEqual(dynamic.label(value), expected)

    def test_runtime_options_eval_and_script_boundaries(self):
        cases = [(["node", "--require", "something", "/x/pi.js"], "pi"),
                 (["bun", "--", "/x/claude.js"], "claude"),
                 (["node", "--eval", "pi.js"], None),
                 (["node", "-econsole.log(1)", "pi.js"], None),
                 (["node", "--print=pi.js"], None),
                 (["node", "/x/other.js", "pi.js"], None),
                 (["node", "/x/node_modules/@oh-my-pi/pi-coding-agent/dist/cli.js"], None)]
        for argv, expected in cases:
            with self.subTest(argv=argv), mock.patch.object(dynamic, "argv_for", return_value=argv):
                self.assertEqual(dynamic.classify(500, argv[0]), expected)

    def test_versioned_native_binary_uses_exact_argv0(self):
        with mock.patch.object(dynamic, "argv_for", return_value=["/home/user/.local/bin/claude"]):
            self.assertEqual(dynamic.classify(500, "/versions/2.1.0"), "claude")

    def test_legacy_pi_package_has_exact_entrypoint(self):
        cases = [("/x/node_modules/@mariozechner/pi-coding-agent/dist/cli.js", "pi"),
                 ("/x/node_modules/@mariozechner/pi-coding-agent/dist/bundle/cli.js", None),
                 ("/x/node_modules/@mariozechner/pi-coding-agent-extra/dist/cli.js", None)]
        for script, expected in cases:
            with self.subTest(script=script), mock.patch.object(dynamic, "argv_for", return_value=["node", script]):
                self.assertEqual(dynamic.classify(500, "node"), expected)

    def test_npm_claude_package_has_exact_entrypoint(self):
        cases = [("/x/node_modules/@anthropic-ai/claude-code/cli.js", "claude"),
                 ("/x/node_modules/@anthropic-ai/claude-code-extra/cli.js", None),
                 ("/x/node_modules/@anthropic-ai/claude-code/tools/cli.js", None),
                 ("/x/node_modules/other/cli.js", None)]
        for script, expected in cases:
            with self.subTest(script=script), mock.patch.object(dynamic, "argv_for", return_value=["node", script]):
                self.assertEqual(dynamic.classify(500, "node"), expected)


class HerdrTests(unittest.TestCase):
    def api(self):
        api = dynamic.Herdr("/tmp/not-a-real-herdr-socket", "w1:p1", "custom:mosherdr.test")
        api.terminal_id = "terminal-1"
        return api

    def test_cli_envelopes_and_explain_unwrapped(self):
        api = self.api()
        with mock.patch.object(dynamic, "tool", return_value="herdr"), \
                mock.patch.object(dynamic, "command", side_effect=[
                    '{"id":"cli:pane:get","result":{"pane":{"terminal_id":"terminal-1"}}}',
                    '{"agent":"codex","state":"working","skip_state_update":false}']) as command:
            self.assertEqual(api.rpc("pane.get", {}), {"pane": {"terminal_id": "terminal-1"}})
            self.assertEqual(api.explain()["state"], "working")
            self.assertEqual(command.call_args_list[1].args[0], ["herdr", "agent", "explain", "w1:p1", "--json"])

    def test_empty_mutation_output_is_success_and_cli_failure_stays_unavailable(self):
        api = self.api()
        with mock.patch.object(dynamic, "tool", return_value="herdr"), \
                mock.patch.object(dynamic, "command", return_value="") as command:
            self.assertEqual(api.rpc("pane.report_agent", {
                "agent": "codex", "state": "working", "seq": 1}), {})
            self.assertEqual(api.rpc("pane.release_agent", {
                "agent": "codex", "seq": 2}), {})
        self.assertEqual(command.call_count, 2)
        with mock.patch.object(dynamic, "tool", return_value="herdr"), \
                mock.patch.object(dynamic, "command", side_effect=dynamic.Unavailable):
            with self.assertRaises(dynamic.Unavailable):
                api.rpc("pane.report_agent", {"agent": "codex", "state": "working", "seq": 3})

    def test_watch_uses_real_herdr_rpc_for_empty_mutation_output(self):
        api = self.api()
        api.agent = "codex"
        finished = False

        def cli(argv, timeout=3):
            if argv[1:3] == ["agent", "explain"]:
                return '{"agent":"codex","state":"working","skip_state_update":false}'
            if argv[1:3] in (["pane", "report-agent"], ["pane", "release-agent"]):
                return ""
            if argv[1:3] == ["pane", "list"]:
                return '{"result":{"panes":[{"pane_id":"w1:p1","terminal_id":"terminal-1"}]}}'
            self.fail(f"unexpected Herdr command: {argv}")

        def sleep(_interval):
            nonlocal finished
            finished = True

        with mock.patch.object(api, "local", return_value=IDENTITY), \
                mock.patch.object(dynamic, "remote", return_value={
                    "agent": "codex", "connection": CONNECTION}), \
                mock.patch.object(dynamic, "tool", return_value="herdr"), \
                mock.patch.object(dynamic, "command", side_effect=cli):
            dynamic.watch(api, 100, host="remote-box", stop=lambda: finished, sleep=sleep)

    def test_actual_mosh_14_metadata_is_one_opaque_argv_element(self):
        api = self.api()
        info = {"pane_id": "w1:p1", "shell_pid": 100, "foreground_process_group_id": 200,
                "foreground_processes": [{"pid": 200, "name": "mosh-client", "argv":
                    ["/opt/homebrew/bin/mosh-client", "-# mosh remote-box -- tmux new -A -s NAME |", "192.0.2.2", "60001"]}]}
        def rpc(method, params):
            return ({"pane": {"pane_id": "w1:p1", "terminal_id": "terminal-1"}}
                    if method == "pane.get" else {"process_info": info})
        processes = dynamic.process_table("100 1 100 200 Ss /bin/zsh\n200 100 200 200 S+ /bin/mosh-client")
        with mock.patch.object(api, "rpc", side_effect=rpc), \
                mock.patch.object(dynamic, "command", return_value="unused"), \
                mock.patch.object(dynamic, "process_table", return_value=processes), \
                mock.patch.object(dynamic, "process_starts", return_value={200: "birth"}), \
                mock.patch.object(dynamic, "argv_for", side_effect=lambda _: info["foreground_processes"][0]["argv"]):
            self.assertEqual(api.local(100), (*IDENTITY, "birth"))
            for change in ({"shell_pid": 101}, {"foreground_processes": []},
                           {"foreground_processes": info["foreground_processes"] * 2}):
                original = info.copy()
                info.update(change)
                with self.assertRaises(dynamic.Unavailable):
                    api.local(100)
                info.clear()
                info.update(original)
            info["foreground_processes"].append({"pid": 199, "name": "zsh", "argv": ["zsh"]})
            self.assertEqual(api.local(100), (*IDENTITY, "birth"))

    def test_release_follows_moved_terminal_and_guards_source_and_kind(self):
        api, calls = self.api(), []
        def rpc(method, params):
            calls.append((method, params))
            return {"panes": [{"pane_id": "w9:p8", "terminal_id": "terminal-1"}]}
        with mock.patch.object(api, "rpc", side_effect=rpc):
            api.release()
        self.assertEqual([entry[1]["agent"] for entry in calls[1:]], list(dynamic.AGENTS))
        self.assertTrue(all(entry[1]["pane_id"] == "w9:p8" and entry[1]["source"] == api.source for entry in calls[1:]))

    def test_failed_release_kind_does_not_skip_other_owned_kinds(self):
        api = self.api()
        api.terminal_id = None
        with mock.patch.object(api, "rpc", side_effect=[dynamic.Unavailable(), {}, {}]) as rpc:
            self.assertFalse(dynamic.release(api))
        self.assertEqual([call.args[1]["agent"] for call in rpc.call_args_list], list(dynamic.AGENTS))

    def test_remote_ssh_has_bounded_noninteractive_arguments_and_no_session_eval(self):
        with mock.patch.object(dynamic, "command", return_value=json.dumps({"agent": None, "connection": CONNECTION})) as command:
            dynamic.remote(IDENTITY, host="remote-box")
        argv = command.call_args.args[0]
        self.assertEqual(argv[:2], ["ssh", "-n"])
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(command.call_args.kwargs["timeout"], 8)
        self.assertIn("--address 192.0.2.2 --port 60001", argv[-1])
        self.assertEqual(argv[-2], "remote-box")
        self.assertIn("~/.local/share/mosherdr/runtime/agent.py", argv[-1])
        self.assertNotIn("herdr-mosh-plain-install", argv[-1])

    def test_remote_rejects_unsafe_host_aliases_before_ssh(self):
        for host in ("-oProxyCommand=x", "host name", "user@host", "h;touch", "非ASCII", "a" * 65):
            with self.subTest(host=host), mock.patch.object(dynamic, "command") as command:
                with self.assertRaises(dynamic.Unavailable):
                    dynamic.remote(IDENTITY, host=host)
                command.assert_not_called()


class DirectClientTests(unittest.TestCase):
    def local(self, *, processes=None, starts=None, argv=None, info_change=None):
        api = dynamic.Herdr("/tmp/fake-only", "w1:p1", "custom:mosherdr.test")
        api.terminal_id = "terminal-1"
        pane = {"pane_id": "w1:p1", "terminal_id": "terminal-1"}
        info = {"pane_id": "w1:p1", "shell_pid": 100, "foreground_process_group_id": 200,
                "foreground_processes": [{"pid": 200, "name": "mosh-client"}]}
        info.update(info_change or {})
        rows = dynamic.process_table(processes or "100 1 100 200 Ss /bin/zsh\n200 100 200 200 S+ /bin/mosh-client")
        with mock.patch.object(api, "rpc", side_effect=lambda method, _: {"pane": pane} if method == "pane.get" else {"process_info": info}), \
                mock.patch.object(dynamic, "command", return_value="unused"), \
                mock.patch.object(dynamic, "process_table", return_value=rows), \
                mock.patch.object(dynamic, "process_starts", side_effect=starts or [{200: "birth"}, {200: "birth"}]), \
                mock.patch.object(dynamic, "argv_for", return_value=argv or ["mosh-client", "192.0.2.2", "60001"]):
            return api.local(100)

    def test_direct_connection_pins_birth_and_strict_endpoint(self):
        self.assertEqual(self.local(), (*IDENTITY, "birth"))
        for argv in (["mosh-client", "192.0.2.2", "６０００１"],
                     ["mosh-client", "192.0.2.2", "060001"],
                     ["mosh-client", "remote-box", "60001"]):
            with self.subTest(argv=argv), self.assertRaises(dynamic.Unavailable):
                self.local(argv=argv)

    def test_direct_pid_reuse_stopped_and_background_are_confirmed_gone(self):
        with self.assertRaises(dynamic.ConnectionGone):
            self.local(starts=[{200: "birth"}, {200: "replacement birth"}])
        for stat in ("T", "Z"):
            with self.subTest(stat=stat), self.assertRaises(dynamic.ConnectionGone):
                self.local(processes=f"100 1 100 200 Ss /bin/zsh\n200 100 200 200 {stat} /bin/mosh-client")
        with self.assertRaises(dynamic.ConnectionGone):
            self.local(processes="100 1 100 999 Ss /bin/zsh\n200 100 200 999 S /bin/mosh-client")

    def test_incomplete_local_metadata_is_temporary_evidence_loss(self):
        for change in ({"foreground_processes": None}, {"shell_pid": None}):
            with self.subTest(change=change):
                try:
                    self.local(info_change=change)
                except dynamic.ConnectionGone:
                    self.fail("incomplete metadata cannot confirm command exit")
                except dynamic.Unavailable:
                    pass
                else:
                    self.fail("incomplete metadata must hide the badge")


class FakeAPI:
    def __init__(self):
        self.agent = None
        self.events = []
        self.skip = False
    def local(self, pid):
        return IDENTITY
    def report(self, state):
        self.events.append(("report", self.agent, state))
    def release(self):
        self.events.append(("release", self.agent))
    def explain(self):
        return {"agent": self.agent, "state": "working", "skip_state_update": self.skip}


class MoshdropTests(unittest.TestCase):
    """Observed outer foreground wrapper and inner-PTY foreground client."""
    processes = """1 0 1 -1 Ss /sbin/launchd
100 1 100 200 Ss /bin/zsh
200 100 200 200 S+ /opt/homebrew/bin/moshdrop
201 200 201 201 S+ /opt/homebrew/bin/mosh-client
"""
    births = "200 Thu Oct 15 10:00:00 2026\n201 Thu Oct 15 10:00:01 2026"
    identity = ("terminal-1", 201, 200, "192.0.2.2", 60001,
                200, "Thu Oct 15 10:00:00 2026", "Thu Oct 15 10:00:01 2026")

    def local(self, *, processes=None, fresh_processes=None, fresh_births=None,
              client_argv=None, fresh_client_argv=None, change_info=None,
              change_pane=None, wrapper_argv=None):
        api = dynamic.Herdr("/tmp/not-a-real-socket", "w1:p1", "custom:mosherdr.test")
        api.terminal_id = "terminal-1"
        wrapper = {"pid": 200, "name": "moshdrop", "argv":
                   ["moshdrop", "remote-box", "--", "tmux", "new", "-A", "-s", "NAME"]}
        pane = {"pane_id": "w1:p1", "terminal_id": "terminal-1"}
        info = {"pane_id": "w1:p1", "shell_pid": 100,
                "foreground_process_group_id": 200, "foreground_processes": [wrapper]}
        reads = {"pane.get": 0, "pane.process_info": 0}
        tables = iter([self.processes if processes is None else processes,
                       (self.processes if processes is None else processes)
                       if fresh_processes is None else fresh_processes])
        births = iter([self.births, self.births if fresh_births is None else fresh_births])
        argv = client_argv or ["mosh-client", "-# original cmd |", "192.0.2.2", "60001"]
        client_args = iter([argv, argv if fresh_client_argv is None else fresh_client_argv])

        def rpc(method, params):
            reads[method] += 1
            if method == "pane.get":
                return {"pane": dict(pane, **(change_pane or {}))
                        if reads[method] > 1 else pane}
            return {"process_info": dict(info, **(change_info or {}))
                    if reads[method] > 1 else info}

        def command(args, timeout=3):
            if args == ["ps", "-axww", "-o", "pid=,ppid=,pgid=,tpgid=,stat=,comm="]:
                return next(tables)
            self.assertEqual(args, ["ps", "-p", "200,201", "-o", "pid=,lstart="])
            return next(births)

        def argv_for(pid):
            if pid == 200:
                return wrapper_argv if wrapper_argv is not None else wrapper["argv"]
            self.assertEqual(pid, 201)
            return next(client_args)

        with mock.patch.object(api, "rpc", side_effect=rpc), \
                mock.patch.object(dynamic, "command", side_effect=command), \
                mock.patch.object(dynamic, "argv_for", side_effect=argv_for):
            return api.local(100)

    def test_observed_wrapper_inner_pty_shape_and_remote_endpoint(self):
        identity = self.local()
        self.assertEqual(identity, self.identity)
        with mock.patch.object(dynamic, "command", return_value=json.dumps({
                "agent": "codex", "connection": CONNECTION})) as command:
            self.assertEqual(dynamic.remote(identity, host="remote-box")["agent"], "codex")
        self.assertIn("--address 192.0.2.2 --port 60001", command.call_args.args[0][-1])

    def test_unrelated_client_does_not_match_wrapper(self):
        with self.assertRaises(dynamic.Unavailable):
            self.local(processes=self.processes.replace("201 200", "201 1"))

    def test_normal_scheduler_state_changes_preserve_connection(self):
        self.assertEqual(self.local(fresh_processes=self.processes.replace("S+", "R+")),
                         self.identity)

    def test_ambiguous_stopped_background_or_dead_clients_fail_closed(self):
        cases = [self.processes + "202 200 202 202 S+ /bin/mosh-client\n",
                 self.processes.replace("201 201 S+", "201 201 T"),
                 self.processes.replace("201 201 S+", "201 201 Z"),
                 self.processes.replace("201 201 S+", "201 202 S")]
        for processes in cases:
            with self.subTest(processes=processes), self.assertRaises(dynamic.Unavailable):
                self.local(processes=processes)

    def test_wrapper_must_be_live_foreground_owned_by_outer_shell(self):
        cases = [self.processes.replace("200 200 S+", "200 200 T"),
                 self.processes.replace("200 200 S+", "200 999 S"),
                 self.processes.replace("200 100 200", "200 1 200"),
                 self.processes.replace("100 1 100 200", "100 1 100 999")]
        for processes in cases:
            with self.subTest(processes=processes), self.assertRaises(dynamic.Unavailable):
                self.local(processes=processes)

    def test_reparenting_or_foreground_changes_during_read_fail_closed(self):
        cases = [self.processes.replace("201 200", "201 1"),
                 self.processes.replace("200 100 200", "200 1 200"),
                 self.processes.replace("201 201 S+", "201 999 S")]
        for processes in cases:
            with self.subTest(processes=processes), self.assertRaises(dynamic.Unavailable):
                self.local(fresh_processes=processes)

    def test_wrapper_and_client_pid_reuse_fail_closed(self):
        for old in ("10:00:00", "10:00:01"):
            with self.subTest(old=old), self.assertRaises(dynamic.Unavailable):
                self.local(fresh_births=self.births.replace(old, "11:00:00"))

    def test_visible_wrapper_exit_pane_move_or_shell_change_fail_closed(self):
        for change in ({"foreground_processes": []}, {"shell_pid": 101},
                       {"foreground_process_group_id": 201}, {"pane_id": "w1:p2"},
                       {"foreground_processes": [{"pid": 202, "name": "moshdrop"}]}):
            with self.subTest(change=change), self.assertRaises(dynamic.Unavailable):
                self.local(change_info=change)
        with self.assertRaises(dynamic.Unavailable):
            self.local(change_pane={"terminal_id": "replacement-terminal"})

    def test_actual_client_argv_and_strict_ip_port_are_required(self):
        cases = [["moshdrop", "192.0.2.2", "60001"],
                 ["mosh-client", "remote-box", "60001"],
                 ["mosh-client", "192.0.2.2", "0"],
                 ["mosh-client", "192.0.2.2", "65536"],
                 ["mosh-client", "192.0.2.2", "６０００１"],
                 ["mosh-client", "192.0.2.2", "060001"],
                 ["mosh-client", "fe80::1%en0", "60001"]]
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(dynamic.Unavailable):
                self.local(client_argv=argv)
        with self.assertRaises(dynamic.Unavailable):
            self.local(wrapper_argv=["zsh", "moshdrop"])

    def test_endpoint_change_during_read_fails_closed(self):
        with self.assertRaises(dynamic.Unavailable):
            self.local(fresh_client_argv=["mosh-client", "192.0.2.2", "60002"])

    def test_scan_bounds_reject_overflow_even_with_matching_client(self):
        broad = self.processes + "".join(
            f"{pid} 200 {pid} {pid} S+ /bin/helper\n" for pid in range(300, 363))
        deep = self.processes + "".join(
            f"{pid} {200 if pid == 300 else pid - 1} {pid} {pid} S+ /bin/helper\n"
            for pid in range(300, 317))
        for processes in (broad, deep):
            with self.subTest(processes=processes), self.assertRaises(dynamic.Unavailable):
                self.local(processes=processes)

    def test_nested_shell_is_outside_confirmed_wrapper_path(self):
        processes = (self.processes.replace("201 200", "201 202")
                     + "202 200 202 202 S+ /bin/zsh\n")
        with self.assertRaises(dynamic.Unavailable):
            self.local(processes=processes)

    def test_visible_shell_does_not_trigger_any_descendant_scan(self):
        api = dynamic.Herdr("/tmp/not-a-real-socket", "w1:p1", "custom:mosherdr.test")
        with mock.patch.object(api, "rpc", side_effect=[
                {"pane": {"pane_id": "w1:p1", "terminal_id": "terminal-1"}},
                {"process_info": {"pane_id": "w1:p1", "shell_pid": 100,
                    "foreground_processes": [{"pid": 200, "name": "zsh"}]}}]), \
                mock.patch.object(dynamic, "command") as command:
            with self.assertRaises(dynamic.Unavailable):
                api.local(100)
        command.assert_not_called()


class WatcherTests(unittest.TestCase):
    def run_polls(self, results, api=None):
        api = api or FakeAPI()
        finished = False
        iterator = iter(results)
        def remote(identity, *, host):
            item = next(iterator)
            if isinstance(item, Exception):
                raise item
            return {"agent": item, "connection": CONNECTION}
        count = 0
        def sleep(_interval):
            nonlocal count, finished
            count += 1
            finished = count >= len(results)
        with mock.patch.object(dynamic, "remote", side_effect=remote):
            dynamic.watch(api, 100, host="remote-box", stop=lambda: finished, sleep=sleep)
        return api.events

    def test_shell_start_agent_start_exit_and_agent_transitions(self):
        events = self.run_polls([None, "codex", None, "claude", "pi", None])
        self.assertEqual(events, [
            ("report", "codex", "unknown"), ("report", "codex", "working"), ("release", "codex"),
            ("report", "claude", "unknown"), ("report", "claude", "working"), ("release", "claude"),
            ("report", "pi", "unknown"), ("report", "pi", "working"), ("release", "pi")])

    def test_ssh_failure_releases_then_same_connection_recovers(self):
        events = self.run_polls(["codex", dynamic.Unavailable(), "codex"])
        self.assertEqual(sum(event[0] == "release" for event in events), 2)
        self.assertEqual(sum(event[:2] == ("report", "codex") for event in events), 4)

    def test_explain_skip_does_not_publish_state(self):
        api = FakeAPI()
        api.skip = True
        self.assertEqual(self.run_polls(["pi"], api), [("report", "pi", "unknown"), ("release", "pi")])

    def test_local_mosh_exit_ends_watcher_even_without_precmd(self):
        api = FakeAPI()
        with mock.patch.object(api, "local", side_effect=[IDENTITY, IDENTITY, dynamic.ConnectionGone()]), \
                mock.patch.object(dynamic, "remote", return_value={"agent": "codex", "connection": CONNECTION}):
            dynamic.watch(api, 100, host="remote-box", sleep=lambda _: None)
        self.assertEqual(api.events[-1], ("release", "codex"))

    def test_reused_remote_pid_or_new_local_connection_never_migrates(self):
        api = FakeAPI()
        with mock.patch.object(dynamic, "remote", side_effect=[
                {"agent": "codex", "connection": CONNECTION},
                {"agent": "claude", "connection": [301, "new birth", 300, CONNECTION[-1]]}]), \
                mock.patch.object(api, "local", return_value=IDENTITY):
            dynamic.watch(api, 100, host="remote-box", sleep=lambda _: None)
        self.assertEqual([event[1] for event in api.events if event[0] == "report"], ["codex", "codex"])

    def test_wrapper_or_client_replacement_during_remote_probe_cannot_report(self):
        identity = MoshdropTests.identity
        for index in (5, 6, 7):
            changed = list(identity)
            changed[index] = "replacement"
            api = FakeAPI()
            with self.subTest(index=index), \
                    mock.patch.object(api, "local", side_effect=[identity, tuple(changed)]), \
                    mock.patch.object(dynamic, "remote", return_value={
                        "agent": "codex", "connection": CONNECTION}):
                dynamic.watch(api, 100, host="remote-box", sleep=lambda _: None)
            self.assertEqual(api.events, [])

    def test_signal_during_probe_cannot_publish_after_cleanup(self):
        api, stopped = FakeAPI(), False
        def remote(identity, *, host):
            nonlocal stopped
            stopped = True
            return {"agent": "codex", "connection": CONNECTION}
        with mock.patch.object(dynamic, "remote", side_effect=remote):
            dynamic.watch(api, 100, host="remote-box", stop=lambda: stopped)
        self.assertEqual(api.events, [])

    def test_failed_cleanup_is_retried_on_next_empty_poll(self):
        api = FakeAPI()
        with mock.patch.object(api, "release", side_effect=[dynamic.Unavailable(), None, None]) as release:
            self.run_polls(["codex", None, None], api)
        self.assertEqual(release.call_count, 2)

    def test_local_lookup_failure_hides_badge_then_same_connection_recovers(self):
        api, waits = FakeAPI(), []
        local = iter([IDENTITY, IDENTITY, dynamic.Unavailable(), IDENTITY, IDENTITY])
        def read(_):
            item = next(local)
            if isinstance(item, Exception):
                raise item
            return item
        with mock.patch.object(api, "local", side_effect=read), \
                mock.patch.object(dynamic, "remote", return_value={"agent": "codex", "connection": CONNECTION}):
            dynamic.watch(api, 100, host="remote-box", stop=lambda: len(waits) == 3, sleep=waits.append)
        self.assertEqual(waits, [1, 2, 1])
        self.assertEqual(api.events, [
            ("report", "codex", "unknown"), ("report", "codex", "working"),
            ("release", "codex"), ("report", "codex", "unknown"),
            ("report", "codex", "working"), ("release", "codex")])

    def test_ssh_failure_backoff_caps_and_resets_after_recovery(self):
        api, waits = FakeAPI(), []
        values = [dynamic.Unavailable()] * 6 + [{"agent": "codex", "connection": CONNECTION}, dynamic.Unavailable()]
        with mock.patch.object(dynamic, "remote", side_effect=values) as remote:
            dynamic.watch(api, 100, host="second-host", stop=lambda: len(waits) == 8, sleep=waits.append)
        self.assertEqual(waits, [2, 4, 8, 10, 10, 10, 1, 2])
        self.assertTrue(all(call.kwargs == {"host": "second-host"} for call in remote.call_args_list))
        self.assertEqual(api.events[-1], ("release", "codex"))

    def test_slow_initial_handshake_survives_more_than_thirty_seconds(self):
        api, waits = FakeAPI(), []
        def local(_):
            if sum(waits) < 60:
                raise dynamic.ConnectionGone()
            return IDENTITY
        with mock.patch.object(api, "local", side_effect=local), \
                mock.patch.object(dynamic, "remote", return_value={"agent": "codex", "connection": CONNECTION}) as remote:
            dynamic.watch(api, 100, host="remote-box", stop=lambda: bool(api.events), sleep=waits.append,
                          clock=lambda: sum(waits))
        self.assertGreater(sum(waits), 30)
        remote.assert_called_once()
        self.assertIn(("report", "codex", "unknown"), api.events)
        self.assertEqual(api.events[-1], ("release", "codex"))

    def test_local_replacement_after_evidence_loss_is_never_adopted(self):
        for index, value in ((1, 201), (3, "192.0.2.3"), (4, 60002), (5, "new birth")):
            original = (*IDENTITY, "birth")
            replacement = list(original)
            replacement[index] = value
            api, waits = FakeAPI(), []
            values = iter([original, original, dynamic.Unavailable(), tuple(replacement)])
            def local(_):
                item = next(values)
                if isinstance(item, Exception):
                    raise item
                return item
            with self.subTest(index=index), mock.patch.object(api, "local", side_effect=local), \
                    mock.patch.object(dynamic, "remote", return_value={"agent": "codex", "connection": CONNECTION}) as remote:
                dynamic.watch(api, 100, host="remote-box", sleep=waits.append)
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(waits, [1, 2])
            self.assertEqual(api.events[-1], ("release", "codex"))

    def test_stop_after_failed_lookup_retries_cleanup_without_new_report(self):
        api, waits = FakeAPI(), []
        with mock.patch.object(api, "local", side_effect=[IDENTITY, IDENTITY, dynamic.Unavailable()]), \
                mock.patch.object(api, "release", side_effect=[dynamic.Unavailable(), None]) as cleanup, \
                mock.patch.object(dynamic, "remote", return_value={"agent": "codex", "connection": CONNECTION}) as remote:
            dynamic.watch(api, 100, host="remote-box", stop=lambda: len(waits) == 2, sleep=waits.append)
        self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(len(api.events), 2)


class ShellHookTests(unittest.TestCase):
    def hook(self, line, environment=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / "python3"
            code = (
                "import json,os,signal,sys,time\n"
                "mode=sys.argv[3]\n"
                "if mode=='configured': sys.exit(0 if sys.argv[-1]=='remote-box' else 1)\n"
                "open(os.path.join(os.environ['HOOK_LOG'],mode),'w').write(json.dumps(sys.argv))\n"
                "if mode=='watch':\n"
                " def stop(*args):\n"
                "  open(os.path.join(os.environ['HOOK_LOG'],'stopped'),'w').write('yes')\n"
                "  sys.exit(0)\n"
                " signal.signal(signal.SIGTERM,stop)\n"
                " time.sleep(5)\n")
            interpreter.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) +
                                   " -c " + shlex.quote(code) + ' "$@"\n')
            interpreter.chmod(0o755)
            # These are deliberately synthetic. The fake interpreter never
            # imports agent and never contacts a Herdr socket.
            env = {"PATH": str(root) + ":/usr/bin:/bin", "HOME": directory,
                   "HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1",
                   "HERDR_SOCKET_PATH": "/tmp/fake-only", "HOOK_LOG": directory}
            env.update(environment or {})
            source = shlex.quote(str(Path(__file__).resolve().parents[1] / "hooks" / "local.zsh"))
            script = ("alias mosh='command mosh'\nfunction moshdrop { :; }\ntrap ':' INT\n"
                      "before=$(alias mosh; functions moshdrop; trap)\nsource " + source + "\nsource " + source +
                      "\n_mosherdr_preexec " + shlex.quote(line) +
                      "\nif [[ -n ${_mosherdr_monitor_pid-} ]]; then\n"
                      "for attempt in {1..40}; do [[ -f $HOOK_LOG/watch ]] && break; /bin/sleep .05; done\nfi\n"
                      "_mosherdr_restore\n/bin/sleep .1\n"
                      "[[ $before == $(alias mosh; functions moshdrop; trap) ]] || exit 19\n"
                      "print -r -- ${HERDR_AGENT-unset}\n")
            result = subprocess.run(["zsh", "-f", "-c", script], env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, timeout=5)
            result.check_returncode()
            self.assertEqual(result.stderr, b"")
            return {path.name: path.read_text() for path in root.iterdir() if path.name != "python3" and path.is_file()}, result.stdout

    def test_exact_ordinary_command_starts_silent_monitor_then_precmd_stops_it(self):
        files, output = self.hook("mosh remote-box -- tmux new -A -s NAME")
        self.assertEqual(output, b"unset\n")
        self.assertIn("stopped", files)
        watch, cleanup = json.loads(files["watch"]), json.loads(files["cleanup"])
        self.assertIn("--shell-pid", watch)
        self.assertEqual(watch[watch.index("--host") + 1], "remote-box")
        self.assertEqual(Path(watch[2]), Path(__file__).resolve().parents[1] / "agent.py")
        self.assertTrue(watch[watch.index("--source") + 1].startswith("custom:mosherdr."))
        self.assertEqual(watch[watch.index("--source") + 1], cleanup[cleanup.index("--source") + 1])

    def test_herdr_public_opaque_pane_id_starts_monitor(self):
        files, _ = self.hook("mosh remote-box", {"HERDR_PANE_ID": "wA0:pZ2"})
        self.assertIn("watch", files)

    def test_herdr_public_pane_id_alphabet_is_exact(self):
        for pane, accepted in (("wA0:pZ2", True), ("wI:p1", False), ("w1:p_", False),
                               ("w1:p1:extra", False)):
            with self.subTest(pane=pane):
                self.assertEqual(dynamic.PANE.fullmatch(pane) is not None, accepted)

    def test_bare_mosh_is_observed_without_exporting_agent(self):
        files, output = self.hook("mosh remote-box")
        self.assertIn("watch", files)
        self.assertEqual(output, b"unset\n")

    def test_explicit_agent_other_host_and_compound_commands_do_not_spawn(self):
        cases = [("mosh remote-box", {"HERDR_AGENT": ""}), ("mosh remote-box", {"HERDR_ENV": "0"}),
                 ("mosh other", {}), ("mosh remote-box; echo later", {}),
                 ("mosh remote-box $(echo injected)", {}), ("mosh remote-box\necho later", {})]
        for line, env in cases:
            with self.subTest(line=line, env=env):
                files, _ = self.hook(line, env)
                self.assertEqual(files, {})


class ProcessBoundaryTests(unittest.TestCase):
    def test_command_deadline_and_no_terminal_input(self):
        self.assertEqual(dynamic.command([sys.executable, "-c", "import sys;print(len(sys.stdin.read()))"]), "0")
        started = time.monotonic()
        with self.assertRaises(dynamic.Unavailable):
            dynamic.command([sys.executable, "-c", "import time;time.sleep(60)"], timeout=0.1)
        self.assertLess(time.monotonic() - started, 2)

    def test_guardian_releases_when_watcher_is_sigkilled(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "released"
            ready = Path(directory) / "ready"
            # Force the create-before-write window that failed on the MacBook.
            # The guardian exits with os._exit, so close the stream explicitly.
            script = (
                "import agent as d,os,time,sys\n"
                "class API:\n"
                " def release(self):\n"
                "  with open(sys.argv[1],'w') as stream:\n"
                "   time.sleep(0.05)\n"
                "   stream.write('released')\n"
                "writer,child=d.guardian(API())\n"
                "open(sys.argv[2],'w').write(str(child))\n"
                "time.sleep(60)\n")
            watcher = subprocess.Popen([sys.executable, "-B", "-c", script, str(marker), str(ready)],
                                       cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                watcher.kill()
                watcher.wait(timeout=2)
                deadline = time.monotonic() + 3
                observed = None
                while time.monotonic() < deadline:
                    try:
                        observed = marker.read_text()
                    except FileNotFoundError:
                        pass
                    if observed == "released":
                        break
                    time.sleep(0.02)
                self.assertEqual(observed, "released")
            finally:
                if watcher.poll() is None:
                    watcher.kill()
                    watcher.wait(timeout=2)

    def test_initial_local_capture_failure_recovers_without_reconnecting(self):
        environment = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_SOCKET_PATH": "/tmp/fake-only"}
        api = mock.Mock()
        api.capture.side_effect = [dynamic.Unavailable(), None]
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch("config.configured_host", return_value=True), \
                mock.patch.object(dynamic, "Herdr", return_value=api), \
                mock.patch.object(dynamic, "process_starts", return_value={100: "shell birth"}), \
                mock.patch.object(dynamic.signal, "signal"), \
                mock.patch.object(dynamic.os, "kill"), \
                mock.patch.object(dynamic, "guardian", return_value=(123, 456)), \
                mock.patch.object(dynamic.os, "close"), \
                mock.patch.object(dynamic.os, "waitpid", return_value=(456, 0)), \
                mock.patch.object(dynamic.time, "sleep") as sleep, \
                mock.patch.object(dynamic, "watch") as watch:
            self.assertEqual(dynamic.main(["watch", "--host", "remote-box", "--shell-pid", "100", "--source", "custom:mosherdr.test"]), 0)
        self.assertEqual(api.capture.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertEqual(watch.call_args.kwargs["host"], "remote-box")
        api.release.assert_called_once()

    def test_shell_exit_during_initial_evidence_loss_stops_startup(self):
        environment = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_SOCKET_PATH": "/tmp/fake-only"}
        api = mock.Mock()
        api.capture.side_effect = dynamic.Unavailable()
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch("config.configured_host", return_value=True), \
                mock.patch.object(dynamic, "Herdr", return_value=api), \
                mock.patch.object(dynamic, "process_starts", return_value={100: "shell birth"}), \
                mock.patch.object(dynamic.signal, "signal"), \
                mock.patch.object(dynamic.os, "kill", side_effect=[None, ProcessLookupError()]), \
                mock.patch.object(dynamic.time, "sleep"), \
                mock.patch.object(dynamic, "guardian") as guardian:
            self.assertEqual(dynamic.main(["watch", "--host", "remote-box", "--shell-pid", "100", "--source", "custom:mosherdr.test"]), 0)
        guardian.assert_not_called()
        api.capture.assert_called_once()

    def test_nonherdr_or_explicit_agent_never_calls_ipc(self):
        for env in ({}, {"HERDR_ENV": "0"}, {"HERDR_ENV": "1", "HERDR_AGENT": ""}):
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(dynamic.Herdr, "rpc") as rpc:
                self.assertEqual(dynamic.main(["watch", "--host", "remote-box", "--shell-pid", "100", "--source", "custom:mosherdr.test"]), 1)
                rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
