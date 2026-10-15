"""File ownership and bounded process helpers for the installer."""

import hashlib
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess

class InstallError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    path = Path(os.path.abspath(path))
    for entry in [path] + list(path.parents):
        if entry.is_symlink():
            raise InstallError("symlink destination refused: " + str(entry))
    if path.exists() and not path.is_file():
        raise InstallError("non-file destination refused: " + str(path))
    return path


def _parent_fd(path, *, create=False):
    """Pin each directory without following links, including during creation."""
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def private_directory(path, *, exclusive=False):
    """Create a private directory; never chmod an existing user directory."""
    path = Path(os.path.abspath(path))
    parent = _parent_fd(path, create=True)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
        except FileExistsError:
            if exclusive:
                raise InstallError("backup destination already exists")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise InstallError("installation directory must be owned by this user with mode 0700")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def safe_unlink(path):
    path = safe_path(path)
    parent = _parent_fd(path)
    try:
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def open_lock(path):
    path = safe_path(path)
    parent = _parent_fd(path)
    try:
        descriptor = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        os.close(descriptor)
        raise InstallError("unsafe installation lock")
    return os.fdopen(descriptor, "r+b")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode,
            info.st_mtime_ns, info.st_ctime_ns)


def snapshot(path):
    path = safe_path(path)
    try:
        parent = _parent_fd(path)
    except FileNotFoundError:
        return b"", None
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return b"", None
        with os.fdopen(descriptor, "rb") as stream:
            initial = os.fstat(stream.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise InstallError("non-file source refused: " + str(path))
            data = stream.read()
            after = os.fstat(stream.fileno())
            named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (_identity(initial) != _identity(after)
                    or _identity(after) != _identity(named) or len(data) != after.st_size):
                raise InstallError("concurrent file change while reading: " + str(path))
        return data, {"sha256": digest(data), "mode": stat.S_IMODE(after.st_mode)}
    finally:
        os.close(parent)


def fingerprint(path):
    return snapshot(path)[1]


def run_bounded(command, *, timeout, input=None, cleanup_grace=1, **options):
    """Keep verification children away from the terminal and clean up on failure."""
    if input is not None:
        options["stdin"] = subprocess.PIPE
    process = subprocess.Popen(command, start_new_session=True, **options)
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_signum, _frame):
        # The outer verification runner may stop this process while it owns a
        # separate shell session. Unwind that session before this process exits.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except BaseException:
        # SIGINT lets a nested Python runner unwind its own shell session first.
        # Every signal targets only the session/process group created above.
        old_interrupt = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=cleanup_grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            # A child can exit before its own descendants. Remove any that
            # remain in this runner's group, even when communicate succeeded.
            try:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                signal.signal(signal.SIGINT, old_interrupt)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def atomic(path, data, mode):
    path = safe_path(path)
    parent = _parent_fd(path, create=True)
    temporary = ".mosherdr-" + secrets.token_hex(12)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        # Replace within the pinned parent, so a concurrent symlink substitution
        # cannot redirect this write into another directory.
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)
