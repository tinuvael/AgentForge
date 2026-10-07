"""Mutation primitives built on the shipped identity/volume/handle API."""

import ctypes as c
import ntpath

from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.win32 import DWORD, HANDLE, Win32


class Win32Mutation(Win32):
    def __init__(self):
        super().__init__()
        signatures = {
            "WriteFile": (c.c_int, [HANDLE, HANDLE, DWORD, c.POINTER(DWORD), HANDLE]),
            "FlushFileBuffers": (c.c_int, [HANDLE]),
            "SetFilePointerEx": (
                c.c_int,
                [HANDLE, c.c_int64, c.POINTER(c.c_int64), DWORD],
            ),
            "SetEndOfFile": (c.c_int, [HANDLE]),
            "SetFileInformationByHandle": (c.c_int, [HANDLE, c.c_int, HANDLE, DWORD]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, arguments

    def open_mutation(self, path, *, create=False, directory=False):
        # GENERIC_READ | GENERIC_WRITE | DELETE, FILE_SHARE_READ only. Existing
        # handles granting write/delete must also permit our exclusive access.
        # CREATE_NEW/OPEN_EXISTING; BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
        access = 0x80000000 | 0x10000 | (0 if directory else 0x40000000)
        handle = self.api.CreateFileW(
            path, access, 1, None, 1 if create else 3, 0x02200000, None
        )
        if handle == c.c_void_p(-1).value:
            self._error()
        return handle

    def rewrite(self, handle, data):
        if not self.api.SetFilePointerEx(handle, 0, None, 0):
            self._error()
        if not self.api.SetEndOfFile(handle):
            self._error()
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + 65536]
            buffer = c.create_string_buffer(chunk)
            used = DWORD()
            if (
                not self.api.WriteFile(handle, buffer, len(chunk), c.byref(used), None)
                or not used.value
            ):
                self._error()
            offset += used.value
        if not self.api.FlushFileBuffers(handle):
            self._error()

    def delete_handle(self, handle):
        disposition = c.c_int(1)  # FILE_DISPOSITION_INFO.DeleteFile (BOOL)
        if not self.api.SetFileInformationByHandle(
            handle, 4, c.byref(disposition), c.sizeof(disposition)
        ):
            self._error()

    def _remove(self, parent, name, expected, directory):
        backend = parent.backend
        backend.validate_parts((name,))
        backend._verify(parent)
        path = ntpath.join(parent.path, name)
        handle = self.open_mutation("\\\\?\\" + path, directory=directory)
        try:
            observed = self.info(handle)
            if (
                observed.identity != expected
                or observed.reparse
                or observed.directory != directory
                or observed.links != 1
                or not self.same_path(self.final_path(handle), "\\\\?\\" + path)
            ):
                raise UnsafeProjectPath("Managed Windows cleanup object changed")
            self.delete_handle(handle)
        finally:
            self.close(handle)

    def remove_file(self, parent, name, expected):
        self._remove(parent, name, expected, False)

    def remove_directory(self, parent, name, expected):
        self._remove(parent, name, expected, True)
