"""Prepare only the dedicated state volume, then run the command without root."""

import ctypes
import os
from pathlib import Path
import stat
import sys
import uuid

UID = GID = 10001


def open_directory(path):
    # Open each component without following symlinks, including parent folders.
    if (
        not path.is_absolute()
        or path == Path("/")
        or any(
            path == base or base in path.parents
            for base in (Path("/app"), Path("/music"), Path("/tmp"))
        )
    ):
        raise ValueError("Use a dedicated absolute configuration directory.")
    if ".." in path.parts:
        raise ValueError("Configuration paths must not contain .. components.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def repair(fd, device):
    info = os.fstat(fd)
    if info.st_dev != device:
        raise ValueError("Nested filesystem mounts are not supported inside configuration storage.")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise ValueError("Hard-linked configuration files are not supported.")
    mode = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
    if (info.st_uid, info.st_gid) != (UID, GID):
        os.fchown(fd, UID, GID)
    if stat.S_IMODE(info.st_mode) != mode:
        os.fchmod(fd, mode)
    if not stat.S_ISDIR(info.st_mode):
        return
    for name in os.listdir(fd):
        entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not (stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)):
            raise ValueError(
                "Configuration storage must contain only regular files and directories; symlinks are not supported."
            )
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(entry.st_mode):
            flags |= os.O_DIRECTORY
        child = os.open(name, flags, dir_fd=fd)
        try:
            current = os.fstat(child)
            if (current.st_dev, current.st_ino) != (entry.st_dev, entry.st_ino):
                raise ValueError(
                    "Configuration storage changed during startup; stop other writers and retry."
                )
            if not (stat.S_ISDIR(current.st_mode) or stat.S_ISREG(current.st_mode)):
                raise ValueError("Unsupported configuration file type.")
            repair(child, device)
        finally:
            os.close(child)


def drop_privileges():
    os.setgroups([])
    os.setgid(GID)
    os.setuid(UID)
    # Prevent the launched process from regaining privileges through exec.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "Could not prevent privilege escalation")


def main():
    path = Path(os.environ.get("HARMONIA_DATA_DIR") or "/config")
    fd = None
    try:
        fd = open_directory(path)
        if os.geteuid() == 0:
            repair(fd, os.fstat(fd).st_dev)
            drop_privileges()
        elif (os.geteuid(), os.getegid()) != (UID, GID):
            raise ValueError(
                "Run startup as root for automatic repair, or use UID/GID 10001 with preconfigured permissions."
            )
        name = ".startup-write-check-" + uuid.uuid4().hex
        probe = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        os.close(probe)
        os.unlink(name, dir_fd=fd)
    except (OSError, ValueError) as error:
        print(f"Harmonia cannot prepare configuration storage at {path}: {error}", file=sys.stderr)
        print(
            "Ensure the dedicated configuration mount is writable by UID/GID 10001. "
            "For NFS or filesystems that block ownership changes, prepare ownership on the storage server. "
            "Do not change ownership of the music library.",
            file=sys.stderr,
        )
        return 1
    finally:
        if fd is not None:
            os.close(fd)
    command = sys.argv[1:] or ["python", "-m", "harmonia", "web"]
    os.execvp(command[0], command)


if __name__ == "__main__":
    sys.exit(main())
