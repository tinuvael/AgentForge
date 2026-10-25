"""Native Darwin descriptor facts. No pathname returned here grants access."""

import ctypes as c
import os

from agentforge.projects.errors import UnsafeProjectPath


class StatFS(c.Structure):
    # Public 64-bit Darwin statfs ABI (sys/mount.h), on arm64 and x86_64.
    _fields_ = [
        ("bsize", c.c_uint32),
        ("iosize", c.c_int32),
        ("blocks", c.c_uint64 * 5),
        ("fsid", c.c_int32 * 2),
        ("owner", c.c_uint32),
        ("type", c.c_uint32),
        ("flags", c.c_uint32),
        ("subtype", c.c_uint32),
        ("name", c.c_char * 16),
        ("mount", c.c_char * 1024),
        ("source", c.c_char * 1024),
        ("reserved", c.c_uint32 * 8),
    ]


def filesystem(fd):
    value = StatFS()
    function = getattr(c.CDLL(None, use_errno=True), "fstatfs64", None)
    if function is None:
        raise UnsafeProjectPath("Darwin filesystem identity is unavailable")
    function.argtypes, function.restype = [c.c_int, c.POINTER(StatFS)], c.c_int
    if function(fd, c.byref(value)):
        raise UnsafeProjectPath("Darwin filesystem identity is unavailable")
    return value


def local_apfs(fd):
    value = filesystem(fd)
    if value.name != b"apfs" or not value.flags & 0x1000:  # MNT_LOCAL
        raise UnsafeProjectPath("Native macOS access requires local APFS")
    return tuple(value.fsid)


def descriptor_path(fd):
    import fcntl

    operation = getattr(fcntl, "F_GETPATH", None)
    if operation is None:
        raise UnsafeProjectPath("Darwin descriptor path identity is unavailable")
    return os.fsdecode(fcntl.fcntl(fd, operation, bytes(1024)).split(b"\0")[0])


def exact_name(fd, name):
    # APFS may resolve case and Unicode normalization aliases. Require the actual
    # directory-entry spelling before I/O, on both case-sensitive and insensitive
    # volumes. Filtering therefore cannot be bypassed through a normalized alias.
    if os.path.basename(descriptor_path(fd)) != name:
        raise UnsafeProjectPath("macOS path aliases are unsupported")
