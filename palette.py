#!/usr/bin/env python3
"""Agent-independent tmux default-color reports for a verified Mosh client."""

import argparse
import os
import re
import selectors
import subprocess
import time
import uuid
from dataclasses import dataclass


class CompatError(Exception):
    pass


def rgb(value):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise argparse.ArgumentTypeError("colour must be #RRGGBB (quote the #)")
    return value.lower()


def colour_report(pane, code, value):
    channels = [value[i:i + 2] * 2 for i in (1, 3, 5)]
    return pane + ":\x1b]" + str(code) + ";rgb:" + "/".join(channels) + "\x1b\\"


def tmux_base(socket):
    return ["tmux", "-N", "-S", socket]


def tmux_read(socket, args):
    try:
        result = subprocess.run(tmux_base(socket) + args, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=1, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompatError("tmux query failed or timed out; no server was started") from exc
    if result.returncode:
        # Do not echo arbitrary tmux errors or content from hooks.
        raise CompatError("tmux query failed; verify the existing server, pane, and tmux version")
    if len(result.stdout) > 65536:
        raise CompatError("tmux query exceeded output limit")
    return result.stdout.decode("utf-8", "strict").rstrip("\n")


@dataclass(frozen=True)
class Target:
    socket: str
    pane: str
    session: str
    client: str
    term: str
    features: str
    window: str = ""
    pid: int = 0
    birth: str = ""


def process_parents(runner=None):
    """Read process identity only, never process arguments or environment."""
    runner = runner or subprocess.run
    result = runner(["ps", "-axo", "pid=,ppid=,lstart=,comm="], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=1, check=False)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise CompatError("process ancestry unavailable")
    parents = {}
    for line in result.stdout.decode("utf-8", "strict").splitlines():
        fields = line.strip().split(None, 7)
        if len(fields) != 8 or not fields[0].isdigit() or not fields[1].isdigit():
            raise CompatError("process ancestry unavailable")
        parents[int(fields[0])] = (int(fields[1]), fields[7], " ".join(fields[2:7]))
    return parents


def has_mosh_ancestor(pid, parents):
    visited = set()
    for _ in range(128):
        if pid <= 1 or pid in visited or pid not in parents:
            return False
        visited.add(pid)
        parent, command = parents[pid][:2]
        if os.path.basename(command) == "mosh-server":
            return True
        pid = parent
    return False


def discover(environ, read=None, parents=None, owner=None):
    """Fail closed unless one exact visual client can supply this pane's palette.

    A second viewer can have different terminal defaults. A remote profile cannot
    identify that viewer's palette, so do not report a shared default in that case.
    For an existing owner, return None while its original live client is inactive
    or conflicted. A missing/replaced client or pane raises CompatError instead.
    """
    read = read or tmux_read
    pane = environ.get("TMUX_PANE", "")
    parts = environ.get("TMUX", "").rsplit(",", 2)
    if (not re.fullmatch(r"%[0-9]+", pane) or len(parts) != 3
            or not parts[0].startswith("/")
            or not all(re.fullmatch(r"[0-9]+", part) for part in parts[1:])):
        raise CompatError("an existing tmux pane is required")
    socket = parts[0]
    fields = read(socket, ["display-message", "-p", "-t", pane,
                           "#{pane_id}\t#{session_id}\t#{window_id}"]).split("\t")
    if (len(fields) != 3 or fields[0] != pane
            or not re.fullmatch(r"\$[0-9]+", fields[1])
            or not re.fullmatch(r"@[0-9]+", fields[2])):
        raise CompatError("tmux did not resolve the exact pane")
    _, session, window = fields
    # Query all clients: linked windows can be visible in a different session.
    clients = read(socket, ["list-clients", "-F",
                           "#{client_name}\t#{client_control_mode}\t#{session_id}"
                           "\t#{client_termname}\t#{client_termfeatures}"
                           "\t#{client_pid}\t#{window_id}\t#{pane_id}"])
    viewers = []
    visual = []
    for line in clients.splitlines():
        fields = line.split("\t")
        if len(fields) != 8:
            raise CompatError("unexpected tmux client metadata")
        name, control, attached, term, features, pid, visible, selected = fields
        if control not in ("0", "1"):
            raise CompatError("unknown tmux client type")
        if control == "0":
            if not name or not re.fullmatch(r"@[0-9]+", visible):
                raise CompatError("unknown visual client")
            visual.append(fields)
            if visible == window:
                viewers.append(fields)
    if owner is None:
        if len(viewers) != 1:
            raise CompatError("pane palette has no unique visual client")
        candidate = viewers[0]
    else:
        if (socket, pane, session, window) != (owner.socket, owner.pane, owner.session, owner.window):
            raise CompatError("original pane context no longer exists")
        matches = [row for row in visual if row[0] == owner.client]
        if len(matches) != 1:
            raise CompatError("original visual client no longer exists")
        candidate = matches[0]
    name, _, attached, term, features, pid, _, selected = candidate
    if not pid.isdigit():
        raise CompatError("visual client has no process identity")
    parents = process_parents() if parents is None else parents
    if not has_mosh_ancestor(int(pid), parents):
        raise CompatError("visual client has no verified Mosh ancestor")
    identity = parents.get(int(pid), ())
    if len(identity) != 3 or not identity[2]:
        raise CompatError("visual client has no process birth token")
    target = Target(socket, pane, session, name, term, features, window, int(pid), identity[2])
    if owner is not None and target != owner:
        raise CompatError("original visual client identity changed")
    if len(viewers) != 1 or viewers[0] != candidate or attached != session or selected != pane:
        if owner is not None:
            return None
        raise CompatError("visual client does not select the exact pane")
    return target


def truecolor(target):
    # tmux reports negotiated capabilities; TERM name alone proves nothing.
    return "RGB" in target.features.split(",")


class Protocol:
    """Validate guards without retaining or displaying pane content."""
    GUARD = re.compile(rb"%(begin|end|error) ([0-9]+) ([0-9]+) ([0-9]+)$")

    def __init__(self, marker):
        self.marker = marker.encode()
        self.active = None
        self.marker_seen = False
        self.marker_done = False
        self.wait_started = False
        self.successes = 0
        self.buffer = b""

    @property
    def ready(self):
        return self.marker_done and self.wait_started

    def feed(self, data):
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if len(line) > 65536:
                raise CompatError("control protocol line exceeded limit")
            self.line(line.rstrip(b"\r"))
        if len(self.buffer) > 65536:
            raise CompatError("control protocol line exceeded limit")

    def line(self, line):
        if line.startswith(b"%exit"):
            raise CompatError("tmux colour bridge exited")
        match = self.GUARD.fullmatch(line)
        if not match:
            if self.active is not None and line == self.marker:
                self.marker_seen = True
            return
        kind, *identity = match.groups()
        identity = tuple(identity)
        if kind == b"begin":
            if self.active is not None:
                raise CompatError("nested control protocol guard")
            self.active = identity
            self.marker_seen = False
            if self.marker_done:
                self.wait_started = True
        else:
            if identity != self.active:
                raise CompatError("unmatched control protocol guard")
            self.active = None
            if kind == b"error":
                raise CompatError("tmux rejected a colour bridge command")
            self.successes += 1
            if self.marker_seen:
                if self.successes < 3:
                    raise CompatError("colour reports were not acknowledged before marker")
                self.marker_done = True


class PanePalette:
    """Supply a missing pane palette; preserve overrides and later edits."""

    def __init__(self, target, values):
        self.target = target
        self.values = values
        self.owned = {}
        self.restore_empty = False

    def read(self, inherited=False):
        flags = "-pA" if inherited else "-p"
        output = tmux_read(self.target.socket, ["show-options", flags, "-t",
                           self.target.pane, "pane-colours"])
        result = {}
        for line in output.splitlines():
            if line == "pane-colours":
                continue
            match = re.fullmatch(r"pane-colours\[([0-9]+)\](?:\*)? (.+)", line)
            if not match:
                raise CompatError("unexpected pane palette metadata")
            result[int(match[1])] = match[2].lower()
        return result

    def start(self):
        local = tmux_read(self.target.socket, ["show-options", "-p", "-t",
                          self.target.pane, "pane-colours"])
        self.restore_empty = local == "pane-colours"
        existing = self.read(inherited=True)
        if existing:
            return  # tmux arrays shadow inheritance as a whole, not per index.
        # Record before mutation so failed startup can restore its values.
        self.owned = dict(enumerate(self.values))
        tmux_read(self.target.socket, ["set-option", "-p", "-t", self.target.pane,
                  "pane-colours", " ".join(self.values)])

    def close(self):
        if not self.owned:
            return
        try:
            current = self.read()
            if current == self.owned:
                command = ["set-option", "-p" if self.restore_empty else "-pu",
                           "-t", self.target.pane, "pane-colours"]
                if self.restore_empty:
                    command.append("")
                tmux_read(self.target.socket, command)
                return
            for index, value in self.owned.items():
                if current.get(index) == value:
                    tmux_read(self.target.socket, ["set-option", "-pu", "-t",
                              self.target.pane, f"pane-colours[{index}]"])
        except (CompatError, OSError, UnicodeError):
            pass  # The pane or its server may already be gone.
        finally:
            self.owned.clear()


class Bridge:
    def __init__(self, target, fg, bg, palette=()):
        self.target = target
        self.channel = "mosherdr-palette-" + uuid.uuid4().hex
        self.protocol = Protocol(self.channel)
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.fg, self.bg = fg, bg
        self.palette = PanePalette(target, palette) if palette else None

    def argv(self):
        t = self.target
        return ["tmux", "-C", "-N", "-S", t.socket,
                "refresh-client", "-t", t.client, "-r", colour_report(t.pane, 10, self.fg), ";",
                "refresh-client", "-t", t.client, "-r", colour_report(t.pane, 11, self.bg), ";",
                "display-message", "-p", self.channel, ";", "wait-for", self.channel]

    def start(self, timeout=3.0):
        if self.palette is not None:
            self.palette.start()
        try:
            self.process = subprocess.Popen(self.argv(), stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            start_new_session=True)
        except OSError as exc:
            raise CompatError("could not start tmux colour bridge") from exc
        for stream in (self.process.stdout, self.process.stderr):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while not self.protocol.ready:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CompatError("tmux colour bridge acknowledgement timed out")
            self.drain(min(remaining, 0.1))
        self.drain(0)

    def drain(self, timeout):
        for key, _ in self.selector.select(timeout):
            chunk = os.read(key.fileobj.fileno(), 4096)
            if not chunk:
                self.selector.unregister(key.fileobj)
                continue
            if key.fileobj is self.process.stdout:
                self.protocol.feed(chunk)
            # Discard stderr; never echo arbitrary terminal or hook content.
        if self.process.poll() is not None:
            raise CompatError("tmux colour bridge stopped")

    def close(self):
        process, self.process = self.process, None
        try:
            if process is not None:
                try:
                    subprocess.run(tmux_base(self.target.socket) + ["wait-for", "-S", self.channel],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=1, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                process.stdin.close()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            self.selector.close()
            if self.palette is not None:
                self.palette.close()
