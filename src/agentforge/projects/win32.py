"""Focused ctypes Win32 read/identity wrapper. Imported safely on POSIX.

No pathname I/O fallback. All API errors are handled by the security backend.
"""

import ctypes as c
from dataclasses import dataclass

from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.identity import RootIdentity

DWORD = c.c_uint32
HANDLE = c.c_void_p
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class _FileTime(c.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]

    @property
    def ticks(self):
        return self.low | self.high << 32


class _HandleInfo(c.Structure):
    _fields_ = [
        ("attributes", DWORD),
        ("created", _FileTime),
        ("accessed", _FileTime),
        ("written", _FileTime),
        ("volume", DWORD),
        ("size_high", DWORD),
        ("size_low", DWORD),
        ("links", DWORD),
        ("index_high", DWORD),
        ("index_low", DWORD),
    ]


class _FileIdInfo(c.Structure):
    _fields_ = [("volume", c.c_uint64), ("file_id", c.c_ubyte * 16)]


@dataclass(frozen=True)
class ObjectInfo:
    identity: RootIdentity
    attributes: int
    size: int = 0
    created: int = 0
    written: int = 0
    links: int = 1

    @property
    def directory(self):
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def reparse(self):
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT)

    @property
    def st_mode(self):
        import stat

        return (
            stat.S_IFLNK
            if self.reparse
            else (stat.S_IFDIR if self.directory else stat.S_IFREG)
        )


class Win32:
    def __init__(self):
        if not hasattr(c, "WinDLL"):
            raise UnsafeProjectPath("Win32 filesystem APIs are unavailable")
        self.api = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": (
                HANDLE,
                [c.c_wchar_p, DWORD, DWORD, HANDLE, DWORD, DWORD, HANDLE],
            ),
            "CloseHandle": (c.c_int, [HANDLE]),
            "GetFileType": (DWORD, [HANDLE]),
            "GetFileInformationByHandle": (c.c_int, [HANDLE, c.POINTER(_HandleInfo)]),
            "GetFileInformationByHandleEx": (c.c_int, [HANDLE, c.c_int, HANDLE, DWORD]),
            "GetFinalPathNameByHandleW": (DWORD, [HANDLE, c.c_wchar_p, DWORD, DWORD]),
            "GetVolumeInformationByHandleW": (
                c.c_int,
                [
                    HANDLE,
                    c.c_wchar_p,
                    DWORD,
                    c.POINTER(DWORD),
                    c.POINTER(DWORD),
                    c.POINTER(DWORD),
                    c.c_wchar_p,
                    DWORD,
                ],
            ),
            "GetDriveTypeW": (DWORD, [c.c_wchar_p]),
            "CompareStringOrdinal": (
                c.c_int,
                [c.c_wchar_p, c.c_int, c.c_wchar_p, c.c_int, c.c_int],
            ),
            "ReadFile": (c.c_int, [HANDLE, HANDLE, DWORD, c.POINTER(DWORD), HANDLE]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.api, name)
            function.restype = result
            function.argtypes = arguments

    @staticmethod
    def _error():
        raise c.WinError(c.get_last_error())

    def open(self, path):
        # GENERIC_READ; FILE_SHARE_READ only. No DELETE or WRITE sharing: ancestors
        # cannot be renamed/reparsed and files cannot be replaced/edited while held.
        # OPEN_EXISTING; BACKUP_SEMANTICS | OPEN_REPARSE_POINT (never follow leaf).
        handle = self.api.CreateFileW(path, 0x80000000, 1, None, 3, 0x02200000, None)
        if handle == c.c_void_p(-1).value:
            self._error()
        return handle

    def close(self, handle):
        if not self.api.CloseHandle(handle):
            self._error()

    def info(self, handle):
        if self.api.GetFileType(handle) != 1:  # FILE_TYPE_DISK only
            raise UnsafeProjectPath("Only disk filesystem objects are supported")
        basic = _HandleInfo()
        stable = _FileIdInfo()
        if not self.api.GetFileInformationByHandle(handle, c.byref(basic)):
            self._error()
        if not self.api.GetFileInformationByHandleEx(
            handle, 18, c.byref(stable), c.sizeof(stable)
        ):
            self._error()  # FileIdInfo required; never fall back to a pathname ID.
        if not any(stable.file_id):
            raise UnsafeProjectPath("Stable filesystem identity is unavailable")
        return ObjectInfo(
            RootIdentity(
                "windows", f"{stable.volume:016x}", bytes(stable.file_id).hex()
            ),
            basic.attributes,
            basic.size_low | basic.size_high << 32,
            basic.created.ticks,
            basic.written.ticks,
            basic.links,
        )

    def final_path(self, handle):
        buffer = c.create_unicode_buffer(32768)
        length = self.api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            self._error()
        return buffer.value

    def same_path(self, left, right):
        # Windows ordinal ignore-case, NOT Unicode casefold (e.g. ß != ss).
        result = self.api.CompareStringOrdinal(left, -1, right, -1, True)
        if not result:
            self._error()
        return result == 2  # CSTR_EQUAL

    def require_supported_volume(self, handle, anchor):
        if self.api.GetDriveTypeW(anchor) != 3:  # DRIVE_FIXED, no SMB/UNC/removable
            raise UnsafeProjectPath("Windows roots require a local fixed NTFS volume")
        filesystem = c.create_unicode_buffer(32)
        if not self.api.GetVolumeInformationByHandleW(
            handle, None, 0, None, None, None, filesystem, len(filesystem)
        ):
            self._error()
        if filesystem.value != "NTFS":
            raise UnsafeProjectPath("Windows roots require NTFS identity and sharing")

    def require_case_insensitive(self, handle):
        flags = DWORD()
        if not self.api.GetFileInformationByHandleEx(
            handle, 23, c.byref(flags), c.sizeof(flags)
        ):
            self._error()  # FileCaseSensitiveInfo required (modern Windows 10/11).
        if flags.value:
            raise UnsafeProjectPath(
                "Case-sensitive Windows directories are unsupported"
            )

    def read(self, handle, size):
        buffer = c.create_string_buffer(size)
        used = DWORD()
        if not self.api.ReadFile(handle, buffer, size, c.byref(used), None):
            self._error()
        return buffer.raw[: used.value]
