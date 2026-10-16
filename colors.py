#!/usr/bin/env python3
"""A bounded shell handshake and an owned, per-pane terminal palette bridge.

No application is launched or identified here. No terminal input is read.
"""

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import sys
import time

from config import ConfigError, load_config
from palette import Bridge, CompatError, discover, rgb, truecolor


EXPECTED = (CompatError, ConfigError, OSError, ValueError, UnicodeError,
            subprocess.TimeoutExpired)
HANDSHAKE_SECONDS = 4.0
CHECK_SECONDS = 1.0


def profile(config=None):
    data = load_config() if config is None else config
    colors = data.get("colors", {})
    if colors.get("enabled") is not True:
        return None
    try:
        defaults = rgb(colors["foreground"]), rgb(colors["background"])
        if "palette" not in colors:
            return defaults
        values = colors["palette"]
        if not isinstance(values, list) or len(values) != 16:
            raise ValueError("palette must contain 16 colors")
        return (*defaults, tuple(rgb(value) for value in values))
    except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError) as exc:
        raise CompatError("a complete explicit palette is required") from exc


def state_directory(environ):
    base = environ.get("XDG_STATE_HOME")
    if base and not Path(base).is_absolute():
        raise CompatError("state directory must be absolute")
    return (Path(base) if base else Path.home() / ".local/state") / "mosherdr/colors"


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise CompatError("color state directory is not privately owned")
    path.chmod(0o700)
    return path


def state_paths(directory, target):
    key = hashlib.sha256((target.socket + "\0" + target.pane).encode()).hexdigest()[:16]
    return directory / (key + ".lock"), directory / (key + ".sock")


def open_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise CompatError("color state lock is not privately owned")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def response(target):
    return b"R\n" if truecolor(target) else b"P\n"


def request(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(HANDSHAKE_SECONDS)
        client.connect(str(path))
        data = b""
        while len(data) < 2:
            chunk = client.recv(3)
            if not chunk:
                break
            data += chunk
        return data


def read_ready(fd, timeout=HANDSHAKE_SECONDS):
    deadline = time.monotonic() + timeout
    data = b""
    while len(data) < 2:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise CompatError("color bridge handshake timed out")
        chunk = os.read(fd, 3)
        if not chunk:
            raise CompatError("color bridge did not acknowledge startup")
        data += chunk
    if data not in (b"R\n", b"P\n"):
        raise CompatError("invalid color bridge acknowledgement")
    return data


def start_owner(lock_fd, environ, launcher=None):
    """Transfer the held lock to one daemon; its pipe acknowledges OSC reports."""
    ready_read, ready_write = os.pipe()
    process = None
    launcher = launcher or subprocess.Popen
    try:
        process = launcher([sys.executable, "-B", str(Path(__file__).resolve()), "_serve",
                            str(lock_fd), str(ready_write)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=environ,
                           pass_fds=(lock_fd, ready_write), start_new_session=True)
        os.close(ready_write)
        ready_write = None
        return read_ready(ready_read)
    except BaseException:
        if process is not None:
            # Only terminate the helper that this invocation just created.
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        raise
    finally:
        os.close(ready_read)
        if ready_write is not None:
            os.close(ready_write)


def ensure(environ=None, config=None, runtime=None, launcher=None):
    environ = os.environ if environ is None else environ
    if profile(config) is None:
        return None
    target = discover(environ)
    directory = private_directory(state_directory(environ) if runtime is None else runtime)
    lock_path, socket_path = state_paths(directory, target)
    # A closing old owner may briefly retain the lock. Retry once after its
    # negative acknowledgement; never signal a PID from a state file.
    for attempt in range(2):
        fd = open_lock(lock_path)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    result = request(socket_path)
                    if result not in (b"R\n", b"P\n"):
                        raise CompatError("color owner no longer matches this pane")
                except EXPECTED:
                    if attempt:
                        raise
                    time.sleep(0.1)
                    continue
            else:
                result = start_owner(fd, environ, launcher)
            return "truecolor" if result == b"R\n" and "NO_COLOR" not in environ else "palette"
        finally:
            os.close(fd)
    return None


def remove_socket(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise CompatError("color endpoint is not a privately owned socket")
    path.unlink()


def serve(lock_fd, ready_fd, environ=None, bridge_factory=None):
    """Keep reports alive only while the exact verified client remains eligible."""
    environ = os.environ if environ is None else environ
    bridge_factory = bridge_factory or Bridge
    bridge = None
    endpoint = None
    path = None
    bound = False
    previous = {}

    def interrupted(_signum, _frame):
        raise CompatError("color owner interrupted")

    try:
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            previous[signum] = signal.signal(signum, interrupted)
        colors = profile()
        if colors is None:
            return
        target = discover(environ)
        directory = private_directory(state_directory(environ))
        _, path = state_paths(directory, target)
        # The inherited descriptor holds the exclusive per-pane lock throughout.
        remove_socket(path)
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint.bind(str(path))
        bound = True
        path.chmod(0o600)
        endpoint.listen(4)
        endpoint.settimeout(0.2)
        bridge = bridge_factory(target, *colors)
        bridge.start()
        if profile() != colors or discover(environ, owner=target) != target:
            raise CompatError("color target changed during startup")
        bridge.drain(0)
        os.write(ready_fd, response(target))
        os.close(ready_fd)
        ready_fd = None
        checked = time.monotonic()
        while True:
            if bridge is not None:
                bridge.drain(0)
            client = None
            try:
                client, _ = endpoint.accept()
                client.settimeout(0.5)
            except socket.timeout:
                pass
            try:
                now = time.monotonic()
                if client is not None or now - checked >= CHECK_SECONDS:
                    if profile() != colors:
                        break
                    eligible = discover(environ, owner=target)
                    if eligible is not None and eligible != target:
                        break
                    if eligible is None and bridge is not None:
                        bridge.close()
                        bridge = None
                    elif eligible is not None and bridge is None:
                        bridge = bridge_factory(target, *colors)
                        bridge.start()
                        if profile() != colors:
                            break
                        if discover(environ, owner=target) != target:
                            bridge.close()
                            bridge = None
                    checked = now
                if client is not None and bridge is not None:
                    bridge.drain(0)
                    try:
                        client.sendall(response(target))
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # A canceled shell handshake does not own this daemon.
            finally:
                if client is not None:
                    client.close()
    except EXPECTED:
        pass  # Expected unsupported paths are silent; no terminal text is logged.
    finally:
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        try:
            if ready_fd is not None:
                os.close(ready_fd)
            try:
                if bridge is not None:
                    bridge.close()
            finally:
                try:
                    if endpoint is not None:
                        endpoint.close()
                finally:
                    try:
                        if bound:
                            remove_socket(path)
                    finally:
                        os.close(lock_fd)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["ensure"]:
            result = ensure()
            if result is not None:
                print(result)
            return 0
        if len(args) == 3 and args[0] == "_serve" and all(item.isdigit() for item in args[1:]):
            serve(int(args[1]), int(args[2]))
            return 0
    except EXPECTED:
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
