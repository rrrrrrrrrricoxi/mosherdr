#!/usr/bin/env python3
"""Report a visible remote agent through its exact local Herdr mosh pane.

The local watcher owns only its unique custom report. The remote probe reads
tmux/process/socket metadata; neither side reads terminal contents or prompts.
"""

import argparse
import ctypes
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time


PANE = re.compile(r"w[123456789ABCDEFGHJKMNPQRSTVWXYZ0]+:p[123456789ABCDEFGHJKMNPQRSTVWXYZ0]+\Z")
SOURCE = re.compile(r"custom:mosherdr\.[A-Za-z0-9.-]{1,100}\Z")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
REMOTE = "~/.local/share/mosherdr/runtime/agent.py"
STATES = {"unknown", "idle", "working", "blocked"}
AGENTS = ("codex", "claude", "pi")
LIMIT = 1024 * 1024


class Unavailable(Exception):
    pass


class ConnectionGone(Unavailable):
    """A successful metadata read confirms the original foreground job is gone."""


def command(argv, timeout=3):
    """All probe children have no terminal input and a finite deadline."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            output, _ = process.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=1)
            raise
        result = subprocess.CompletedProcess(argv, process.returncode, output)
        if result.returncode or len(result.stdout) > LIMIT:
            raise Unavailable()
        return result.stdout.decode("utf-8", "strict").strip()
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise Unavailable() from exc


def tool(name):
    for path in (shutil.which(name), "/opt/homebrew/bin/" + name,
                 "/usr/local/bin/" + name, "/usr/sbin/" + name):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise Unavailable()


def address(value):
    value = ipaddress.ip_address(value)
    return str(value.ipv4_mapped if isinstance(value, ipaddress.IPv6Address)
               and value.ipv4_mapped else value)


def process_table(text):
    """ps comm is executable metadata, not command arguments."""
    result = {}
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6:
            raise Unavailable()
        try:
            pid, parent, group, foreground = map(int, fields[:4])
        except ValueError as exc:
            raise Unavailable() from exc
        if pid in result:
            raise Unavailable()
        result[pid] = (parent, group, foreground, fields[4], fields[5])
    return result


def mosh_ancestor(pid, processes):
    seen = set()
    while pid > 1 and pid not in seen and len(seen) < 128:
        seen.add(pid)
        row = processes.get(pid)
        if row is None:
            return None
        if os.path.basename(row[4]) == "mosh-server":
            return pid
        pid = row[0]
    return None


def argv_for(pid):
    """Return argv only; Darwin's kernel buffer also contains unparsed env bytes."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(LIMIT)
        data = ctypes.create_string_buffer(LIMIT)
        if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0) != 0:
            raise Unavailable()
        raw = data.raw[:size.value]
        if len(raw) < 4:
            raise Unavailable()
        count = int.from_bytes(raw[:4], sys.byteorder, signed=True)
        # argc, executable path, zero padding, then exactly argc arguments.
        tail = raw[4:]
        boundary = tail.find(b"\0")
        if boundary < 0 or not 0 < count < 16384:
            raise Unavailable()
        arguments = tail[boundary:].lstrip(b"\0").split(b"\0", count)
        if len(arguments) <= count:
            raise Unavailable()
        return [part.decode("utf-8", "strict") for part in arguments[:count]]
    with open("/proc/" + str(pid) + "/cmdline", "rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT or not raw.endswith(b"\0"):
        raise Unavailable()
    return [part.decode("utf-8", "strict") for part in raw[:-1].split(b"\0")]


def mosh_endpoint(argv):
    """Read an actual mosh-client argv, including Mosh 1.4's opaque title."""
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(word, str) for word in argv)
            or os.path.basename(argv[0]) != "mosh-client"):
        raise Unavailable()
    args = argv[1:]
    if len(args) == 3 and args[0].startswith("-# "):
        args = args[1:]
    elif len(args) == 4 and args[0] == "-#":
        args = args[2:]
    if (len(args) != 2 or not args[1].isascii() or not args[1].isdigit()
            or len(args[1]) > 5 or not 1 <= int(args[1]) <= 65535
            or "%" in args[0]):
        raise Unavailable()
    try:
        return address(args[0]), int(args[1])
    except ValueError as exc:
        raise Unavailable() from exc


def process_starts(pids):
    """Read both birth tokens in one bounded ps call, without argv or env."""
    output = command(["ps", "-p", ",".join(str(pid) for pid in pids),
                      "-o", "pid=,lstart="])
    starts = {}
    for line in output.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit() or int(fields[0]) in starts:
            raise Unavailable()
        starts[int(fields[0])] = fields[1]
    if set(starts) != set(pids):
        raise Unavailable()
    return starts


def wrapper_client(processes, root, shell_pid, group):
    """Resolve only the known foreground wrapper's bounded descendant tree."""
    wrapper, shell = processes.get(root), processes.get(shell_pid)
    if (wrapper is None or shell is None or shell[2] != group
            or os.path.basename(wrapper[4]) != "moshdrop"
            or wrapper[1:3] != (group, group)
            or any(flag in wrapper[3] for flag in "TZ")):
        raise Unavailable()
    # Confirm shell ownership without searching below the outer shell.
    parent, ancestry = wrapper[0], set()
    while parent != shell_pid:
        if parent <= 1 or parent in ancestry or len(ancestry) >= 16:
            raise Unavailable()
        ancestry.add(parent)
        row = processes.get(parent)
        if row is None:
            raise Unavailable()
        parent = row[0]
    children = {}
    for pid, row in processes.items():
        children.setdefault(row[0], []).append(pid)
    queue, seen, clients = [(root, 0)], set(), []
    while queue:
        pid, depth = queue.pop(0)
        if pid in seen or len(seen) >= 64:
            raise Unavailable()
        seen.add(pid)
        row = processes[pid]
        name = os.path.basename(row[4])
        if name == "mosh-client":
            clients.append(pid)
        descendants = children.get(pid, [])
        # A shell nested below the wrapper is outside the confirmed launch path.
        if name in ("sh", "bash", "zsh", "dash", "fish", "ksh", "tcsh", "csh"):
            if descendants:
                raise Unavailable()
        if depth >= 16 and descendants:
            raise Unavailable()
        queue.extend((child, depth + 1) for child in descendants)
    if len(clients) != 1:
        raise Unavailable()
    client = clients[0]
    row = processes[client]
    if row[1] <= 1 or row[1] != row[2] or any(flag in row[3] for flag in "TZ"):
        raise Unavailable()
    return client


def label(name):
    name = os.path.basename(name).strip().lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".js"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return {"codex": "codex", "claude": "claude", "claude-code": "claude",
            "pi": "pi"}.get(name)


def classify(pid, executable):
    direct = label(executable)
    if direct:
        return direct
    argv = argv_for(pid)
    if argv and label(argv[0]):
        return label(argv[0])
    if os.path.basename(executable).lower() not in ("node", "bun"):
        return None
    args = argv[1:]
    values = {"-r", "--require", "--loader", "--import", "--experimental-loader",
              "--inspect-port", "-W", "-X", "-S", "-L", "-o"}
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--":
            index += 1
            break
        if (word.split("=", 1)[0] in ("-e", "--eval", "-p", "--print")
                or word.startswith(("-e", "-p")) and not word.startswith("--")):
            return None
        if word in values:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            break
    if index >= len(args):
        return None
    script = args[index]
    direct = label(script)
    if direct:
        return direct
    if script.endswith("/node_modules/@anthropic-ai/claude-code/cli.js"):
        return "claude"
    if any(script.endswith("/node_modules/@earendil-works/pi-coding-agent/" + path)
           for path in ("dist/cli.js", "dist/bundle/cli.js")):
        return "pi"
    if script.endswith("/node_modules/@mariozechner/pi-coding-agent/dist/cli.js"):
        return "pi"
    return None


def socket_matches(text, host, port):
    for line in text.splitlines():
        if not line.startswith("n") or "->" in line:
            continue
        ip, separator, value = line[1:].rpartition(":")
        if not separator or value != str(port):
            continue
        ip = ip.strip("[]")
        if ip in ("*", "0.0.0.0", "::") or address(ip) == address(host):
            return True
    return False


def probe(host, port):
    """Locate the actual tmux client by its mosh server's bound UDP socket."""
    if not 1 <= port <= 65535:
        raise Unavailable()
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if len(connection) != 4 or address(connection[2]) != address(host):
        raise Unavailable()
    tmux = tool("tmux")

    def read(*args):
        return command([tmux, "-N", *args])

    clients_format = ("#{client_pid}\t#{client_control_mode}\t#{session_id}"
                      "\t#{window_id}\t#{pane_id}")
    all_rows = read("list-clients", "-F", clients_format).splitlines()
    processes = process_table(command(["ps", "-axww", "-o",
                                      "pid=,ppid=,pgid=,tpgid=,stat=,comm="]))
    matches, checked = [], {}
    for line in all_rows:
        client = line.split("\t")
        if len(client) != 5:
            raise Unavailable()
        if client[1] == "1":  # The palette bridge has an unattached control client.
            continue
        if (not client[0].isdigit() or client[1] != "0"
                or not re.fullmatch(r"\$[0-9]+", client[2])
                or not re.fullmatch(r"@[0-9]+", client[3])
                or not re.fullmatch(r"%[0-9]+", client[4])):
            raise Unavailable()
        server = mosh_ancestor(int(client[0]), processes)
        if server is None:
            continue
        if server not in checked:
            try:
                sockets = command([tool("lsof"), "-nP", "-a", "-p", str(server),
                                   "-iUDP", "-Fn"])
                checked[server] = socket_matches(sockets, host, port)
            except Unavailable:
                checked[server] = False
        if checked[server]:
            matches.append((client, server))
    if len(matches) != 1:
        raise Unavailable()
    client, server = matches[0]
    client_pid, sid = int(client[0]), client[2]
    pane = read("display-message", "-p", "-t", client[4],
                "#{pane_id}\t#{window_id}\t#{pane_dead}\t#{pane_pid}"
                "\t#{pane_in_mode}").split("\t")
    # The selected client's pane is authoritative. A linked window's global
    # session/active-pane formats can describe a different client.
    if (len(pane) != 5 or pane[:2] != [client[4], client[3]]
            or pane[2] != "0" or not pane[3].isdigit() or pane[4] != "0"):
        raise Unavailable()
    shell = processes.get(int(pane[3]))
    if shell is None or shell[2] <= 1:
        raise Unavailable()
    foreground, agents = shell[2], set()
    for pid, row in processes.items():
        if (row[1] != foreground or row[2] != foreground
                or any(flag in row[3] for flag in "TZ")):
            continue
        parent, seen = pid, set()
        while parent in processes and parent not in seen and len(seen) < 128:
            if parent == int(pane[3]):
                kind = classify(pid, row[4])
                if kind:
                    agents.add(kind)
                break
            seen.add(parent)
            parent = processes[parent][0]
    if len(agents) > 1:
        raise Unavailable()
    births = command(["ps", "-p", str(client_pid) + "," + str(server),
                      "-o", "pid=,lstart="]).splitlines()
    starts = {}
    for line in births:
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            raise Unavailable()
        starts[int(fields[0])] = fields[1]
    if set(starts) != {client_pid, server}:
        raise Unavailable()
    if read("list-clients", "-F", clients_format).splitlines() != all_rows:
        raise Unavailable()
    return {"agent": next(iter(agents), None),
            "connection": [client_pid, starts[client_pid], server, starts[server]]}


class Herdr:
    def __init__(self, path, pane, source):
        self.path, self.pane, self.source = path, pane, source
        self.seq = 0
        self.agent = None
        self.terminal_id = None

    def rpc(self, method, params):
        pane = params.get("pane_id", self.pane)
        if method == "pane.get":
            args = ["pane", "get", pane]
        elif method == "pane.list":
            args = ["pane", "list"]
        elif method == "pane.process_info":
            args = ["pane", "process-info", "--pane", pane]
        elif method == "agent.explain":
            return {"explain": json.loads(command([tool("herdr"), "agent", "explain",
                                                    self.pane, "--json"], timeout=2))}
        elif method in ("pane.report_agent", "pane.release_agent"):
            args = ["pane", "report-agent" if method == "pane.report_agent" else "release-agent",
                    pane, "--source", self.source, "--agent", params["agent"], "--seq", str(params["seq"])]
            if method == "pane.report_agent":
                args += ["--state", params["state"]]
        else:
            raise Unavailable()
        output = command([tool("herdr"), *args], timeout=2)
        # Herdr's send_ok_request returns exit status only for these mutations.
        # Read commands still require their JSON response envelopes.
        if method in ("pane.report_agent", "pane.release_agent") and not output:
            return {}
        envelope = json.loads(output)
        if (not isinstance(envelope, dict) or envelope.get("error")
                or not isinstance(envelope.get("result"), dict)):
            raise Unavailable()
        return envelope["result"]

    def capture(self):
        pane = self.rpc("pane.get", {"pane_id": self.pane})["pane"]
        if pane.get("pane_id") != self.pane or not pane.get("terminal_id"):
            raise Unavailable()
        self.terminal_id = pane["terminal_id"]

    def local(self, shell_pid):
        pane = self.rpc("pane.get", {"pane_id": self.pane})["pane"]
        info = self.rpc("pane.process_info", {"pane_id": self.pane})["process_info"]
        if (not isinstance(pane, dict) or not isinstance(info, dict)
                or not isinstance(pane.get("pane_id"), str)
                or not isinstance(pane.get("terminal_id"), str) or not pane["terminal_id"]
                or not isinstance(info.get("pane_id"), str)
                or type(info.get("shell_pid")) is not int
                or not isinstance(info.get("foreground_processes"), list)
                or any(not isinstance(row, dict) for row in info["foreground_processes"])):
            raise Unavailable()
        if (pane["pane_id"] != self.pane
                or (self.terminal_id is not None and pane["terminal_id"] != self.terminal_id)
                or info["pane_id"] != self.pane or info["shell_pid"] != shell_pid):
            raise ConnectionGone()
        rows = [row for row in info["foreground_processes"]
                if os.path.basename(row.get("name", "")) in ("mosh-client", "moshdrop")]
        if not rows:
            raise ConnectionGone()
        if len(rows) != 1:
            raise Unavailable()
        if os.path.basename(rows[0]["name"]) == "moshdrop":
            return self.wrapped_local(shell_pid, pane, info, rows[0])
        pid, group = rows[0].get("pid"), info.get("foreground_process_group_id")
        if type(pid) is not int or type(group) is not int or min(pid, group) <= 1:
            raise Unavailable()
        ps = ["ps", "-axww", "-o", "pid=,ppid=,pgid=,tpgid=,stat=,comm="]
        processes = process_table(command(ps))
        row, shell = processes.get(pid), processes.get(shell_pid)
        if (row is None or shell is None or shell[2] != group
                or row[1:3] != (group, group)
                or os.path.basename(row[4]) != "mosh-client"
                or any(flag in row[3] for flag in "TZ")):
            raise ConnectionGone()
        starts = process_starts((pid,))
        endpoint = mosh_endpoint(argv_for(pid))
        # Pin process birth as well as endpoint; PID reuse must never recover
        # the old report into a replacement command.
        fresh = process_table(command(ps))
        fresh_row, fresh_shell = fresh.get(pid), fresh.get(shell_pid)
        if (fresh_row is None or fresh_shell is None or fresh_shell[2] != group
                or fresh_row[:3] != row[:3] or fresh_row[4] != row[4]
                or any(flag in fresh_row[3] for flag in "TZ")
                or process_starts((pid,)) != starts
                or mosh_endpoint(argv_for(pid)) != endpoint):
            raise ConnectionGone()
        return (pane["terminal_id"], pid, group, *endpoint, starts[pid])

    def wrapped_local(self, shell_pid, pane, info, wrapper):
        root, group = wrapper.get("pid"), info.get("foreground_process_group_id")
        if (type(root) is not int or type(group) is not int or min(root, group) <= 1):
            raise Unavailable()
        ps = ["ps", "-axww", "-o", "pid=,ppid=,pgid=,tpgid=,stat=,comm="]
        processes = process_table(command(ps))
        client = wrapper_client(processes, root, shell_pid, group)
        starts = process_starts((root, client))
        root_argv = argv_for(root)
        if not root_argv or os.path.basename(root_argv[0]) != "moshdrop":
            raise Unavailable()
        endpoint = mosh_endpoint(argv_for(client))
        # Process reads and Herdr IPC are separate snapshots. Reject any observed
        # replacement, reparenting, foreground change, or endpoint change.
        fresh = process_table(command(ps))
        if (wrapper_client(fresh, root, shell_pid, group) != client
                or any(fresh[pid][:3] != processes[pid][:3]
                       or fresh[pid][4] != processes[pid][4] for pid in (root, client))
                or process_starts((root, client)) != starts
                or argv_for(root) != root_argv
                or mosh_endpoint(argv_for(client)) != endpoint):
            raise Unavailable()
        fresh_pane = self.rpc("pane.get", {"pane_id": self.pane})["pane"]
        fresh_info = self.rpc("pane.process_info", {"pane_id": self.pane})["process_info"]
        fresh_rows = [row for row in fresh_info.get("foreground_processes", [])
                      if os.path.basename(row.get("name", "")) in ("mosh-client", "moshdrop")]
        if (fresh_pane.get("pane_id") != self.pane
                or fresh_pane.get("terminal_id") != pane["terminal_id"]
                or fresh_info.get("pane_id") != self.pane
                or fresh_info.get("shell_pid") != shell_pid
                or fresh_info.get("foreground_process_group_id") != group
                or len(fresh_rows) != 1 or fresh_rows[0].get("pid") != root
                or os.path.basename(fresh_rows[0].get("name", "")) != "moshdrop"):
            raise Unavailable()
        return (pane["terminal_id"], client, group, *endpoint,
                root, starts[root], starts[client])

    def report(self, state):
        self.seq += 1
        self.rpc("pane.report_agent", {"pane_id": self.pane, "source": self.source,
                                      "agent": self.agent, "state": state, "seq": self.seq})

    def release(self):
        pane = self.pane
        if self.terminal_id is not None:
            panes = self.rpc("pane.list", {})["panes"]
            matches = [item["pane_id"] for item in panes
                       if item.get("terminal_id") == self.terminal_id]
            if len(matches) != 1:
                raise Unavailable()
            pane = matches[0]
        # The guardian and shell cleanup may not know which kind was last
        # reported. The exact source+agent guard makes these releases safe.
        failed = False
        for agent in AGENTS:
            self.seq += 1
            try:
                self.rpc("pane.release_agent", {"pane_id": pane, "source": self.source,
                                               "agent": agent, "seq": self.seq})
            except (Unavailable, ValueError, KeyError, TypeError):
                failed = True
        if failed:
            raise Unavailable()

    def explain(self):
        return self.rpc("agent.explain", {"target": self.pane})["explain"]


def remote(identity, *, host):
    if not HOST.fullmatch(host):
        raise Unavailable()
    words = ["python3", "-B", REMOTE, "probe", "--address", identity[3], "--port", str(identity[4])]
    # The sole unquoted word is a fixed ~/ path, expanded by the remote shell.
    script = "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin " + " ".join(
        word if word == REMOTE else shlex.quote(word) for word in words)
    result = json.loads(command(["ssh", "-n", "-o", "BatchMode=yes", "-o",
                                 "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=3",
                                 "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=2",
                                 "-o", "ServerAliveCountMax=1", host, script], timeout=8))
    if (not isinstance(result, dict) or result.get("agent") not in (*AGENTS, None)
            or not isinstance(result.get("connection"), list)
            or len(result["connection"]) != 4
            or any(type(result["connection"][index]) is not int
                   or result["connection"][index] <= 1 for index in (0, 2))
            or result["connection"][0] == result["connection"][2]
            or any(not isinstance(result["connection"][index], str)
                   or not 0 < len(result["connection"][index]) <= 128 for index in (1, 3))):
        raise Unavailable()
    return result


def release(api):
    try:
        api.release()
        return True
    except (Unavailable, ValueError, KeyError, TypeError):
        return False


def guardian(api):
    """A pipe owner releases even if the watcher receives SIGKILL."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(writer)
        for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            signal.signal(signum, signal.SIG_IGN)
        try:
            while os.read(reader, 1):
                pass
            api.seq = 2**62
            for _ in range(3):
                if release(api):
                    break
        finally:
            os._exit(0)
    os.close(reader)
    return writer, pid


def watch(api, shell_pid, *, host, interval=1, stop=None, clock=time.monotonic,
          sleep=time.sleep):
    """Hide unverifiable reports, retry evidence loss, and never migrate identity."""
    local_id = remote_id = None
    claimed = False
    delay = interval
    while stop is None or not stop():
        failed = False
        try:
            try:
                current = api.local(shell_pid)
            except ConnectionGone:
                if local_id is not None:
                    break
                # preexec can precede an arbitrarily slow Mosh SSH handshake.
                raise Unavailable()
            if local_id is None:
                local_id = current
            if current != local_id:
                break
            result = remote(local_id, host=host)
            if stop is not None and stop():
                break
            if remote_id is None:
                remote_id = result["connection"]
            if result["connection"] != remote_id:
                break
            # Revalidate even an empty remote result after potentially slow SSH.
            if api.local(shell_pid) != local_id:
                break
            if stop is not None and stop():
                break
            if result["agent"] is not None:
                if claimed and result["agent"] != api.agent:
                    if not release(api):
                        raise Unavailable()
                    claimed = False
                api.agent = result["agent"]
                if not claimed:
                    claimed = True
                    api.report("unknown")
                explanation = api.explain()
                state = explanation.get("state")
                if explanation.get("agent") != api.agent or state not in STATES:
                    raise Unavailable()
                if (not explanation.get("skip_state_update", True)
                        and (stop is None or not stop())):
                    api.report(state)
            elif claimed:
                claimed = not release(api)
                if claimed:
                    raise Unavailable()
        except ConnectionGone:
            break
        except (Unavailable, OSError, ValueError, KeyError, TypeError):
            failed = True
            if claimed:
                claimed = not release(api)
        if stop is not None and stop():
            break
        delay = min(10, max(interval, delay * 2)) if failed else interval
        sleep(delay)
    if claimed:
        release(api)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("watch", "probe", "cleanup", "configured"))
    parser.add_argument("--host")
    parser.add_argument("--address")
    parser.add_argument("--port", type=int)
    parser.add_argument("--shell-pid", type=int)
    parser.add_argument("--source")
    args = parser.parse_args(argv)
    if args.mode in ("watch", "configured"):
        if not HOST.fullmatch(args.host or ""):
            return 1
        from config import configured_host
        try:
            if not configured_host(args.host):
                return 1
        except (OSError, ValueError):
            return 1
        if args.mode == "configured":
            return 0
    if args.mode == "probe":
        def expired(_signum, _frame):
            raise Unavailable()
        previous = signal.signal(signal.SIGALRM, expired)
        signal.alarm(6)
        try:
            print(json.dumps(probe(args.address or "", args.port or 0)))
            return 0
        except (Unavailable, OSError, ValueError, TypeError):
            return 1
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
    pane, path = os.environ.get("HERDR_PANE_ID", ""), os.environ.get("HERDR_SOCKET_PATH", "")
    if (os.environ.get("HERDR_ENV") != "1" or "HERDR_AGENT" in os.environ
            or not PANE.fullmatch(pane) or not os.path.isabs(path)
            or not SOURCE.fullmatch(args.source or "")):
        return 1
    api = Herdr(path, pane, args.source)
    if args.mode == "cleanup":
        api.seq = 2**62
        release(api)
        return 0
    if not args.shell_pid or args.shell_pid <= 1:
        return 1
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    shell_birth = None

    def should_stop():
        if stopped:
            return True
        try:
            os.kill(args.shell_pid, 0)
        except OSError:
            return True
        if shell_birth is not None:
            try:
                return process_starts((args.shell_pid,))[args.shell_pid] != shell_birth
            except (Unavailable, OSError, ValueError, KeyError, TypeError):
                # Failure to read a live process is temporary evidence loss.
                pass
        return False

    delay = 1
    while not should_stop():
        try:
            if shell_birth is None:
                shell_birth = process_starts((args.shell_pid,))[args.shell_pid]
            api.capture()
            break
        except (Unavailable, OSError, ValueError, KeyError, TypeError):
            time.sleep(delay)
            delay = min(10, delay * 2)
    else:
        return 0
    if should_stop():
        return 0
    writer, child = guardian(api)
    try:
        watch(api, args.shell_pid, host=args.host, stop=should_stop)
    finally:
        release(api)
        os.close(writer)
        # Guardian RPC is bounded; never wait indefinitely on cleanup.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if os.waitpid(child, os.WNOHANG)[0]:
                break
            time.sleep(0.05)
    return 0


if __name__ == "__main__":
    sys.exit(main())
