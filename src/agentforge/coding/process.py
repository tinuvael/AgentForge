"""Bounded process execution on the host. This is NOT an OS/network sandbox."""

import asyncio
import os
import signal
import subprocess
from time import monotonic


def conservative_environment():
    # No HOME/USERPROFILE, credential, provider, SSH, proxy or GIT inheritance.
    # Executables are absolute; PATH is a fixed OS helper path, never repo/cwd.
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
    }
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment.update(
            SystemRoot=system_root, WINDIR=system_root, PATH=system_root + r"\System32"
        )
    else:
        environment["PATH"] = "/usr/bin:/bin"
    return environment


def git_environment():
    environment = conservative_environment()
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="",
        GIT_ATTR_NOSYSTEM="1",
        GIT_PAGER="cat",
        GIT_CONFIG_SYSTEM=os.devnull,
        LC_ALL="C",
    )
    return environment


class WindowsJob:
    """Kill-on-close Job, assigned before the suspended validation can execute."""

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class Limits(c.Structure):
            _fields_ = [
                ("time1", c.c_int64),
                ("time2", c.c_int64),
                ("flags", w.DWORD),
                ("min_ws", c.c_size_t),
                ("max_ws", c.c_size_t),
                ("active", w.DWORD),
                ("affinity", c.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class Counters(c.Structure):
            _fields_ = [("values", c.c_uint64 * 6)]

        class Extended(c.Structure):
            _fields_ = [
                ("basic", Limits),
                ("io", Counters),
                ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t),
                ("peak_process", c.c_size_t),
                ("peak_job", c.c_size_t),
            ]

        self.api = c.WinDLL("kernel32", use_last_error=True)
        for name, result, arguments in (
            ("CreateJobObjectW", w.HANDLE, [w.LPVOID, w.LPCWSTR]),
            ("SetInformationJobObject", w.BOOL, [w.HANDLE, c.c_int, w.LPVOID, w.DWORD]),
            ("AssignProcessToJobObject", w.BOOL, [w.HANDLE, w.HANDLE]),
            ("TerminateJobObject", w.BOOL, [w.HANDLE, w.UINT]),
            ("CloseHandle", w.BOOL, [w.HANDLE]),
        ):
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, arguments
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Validation Job unavailable")
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(
            self.handle, 9, c.byref(limits), c.sizeof(limits)
        ):
            self.close()
            raise OSError("Validation Job policy unavailable")

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError("Validation Job assignment unavailable")

    def stop(self):
        self.api.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


async def validation_process(
    argv, *, cwd, timeout, cap, cancellation, pass_fds=(), on_cancel=None
):
    """No model-controlled argv. Drain pipes concurrently and kill the whole group.

    Windows Job assignment uses a suspended process and NtResumeProcess; unsupported
    Job semantics fail closed before repository code runs.
    """
    from agentforge.coding.models import ValidationRun

    started = monotonic()
    job = WindowsJob() if os.name == "nt" else None
    process = None
    try:
        options = (
            {"creationflags": 0x00000004}
            if job
            else {"start_new_session": True, "pass_fds": pass_fds}
        )
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=conservative_environment(),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            **options,
        )
        if job:
            job.assign(process)
            import ctypes as c

            resume = c.WinDLL("ntdll").NtResumeProcess
            resume.argtypes, resume.restype = [c.c_void_p], c.c_long
            if resume(int(process._handle)) != 0:
                raise OSError("Validation could not resume safely")

        def stop():
            if job:
                job.stop()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        buffers = [bytearray(), bytearray()]
        truncated = [False, False]

        def drain(stream, index):
            try:
                while chunk := stream.read(8192):
                    remaining = max(0, cap - len(buffers[index]))
                    buffers[index].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated[index] = True
                        # Do not drain infinite output forever; oversize is a
                        # factual non-success process outcome, with explicit flags.
                        stop()
            finally:
                stream.close()

        readers = [
            asyncio.create_task(asyncio.to_thread(drain, process.stdout, 0)),
            asyncio.create_task(asyncio.to_thread(drain, process.stderr, 1)),
        ]
        timed_out = cancelled = False

        def outcome(*, interrupted=False):
            from agentforge.tools.policy import clip

            out, out_partial = clip(display_text(bytes(buffers[0])), cap)
            err, err_partial = clip(display_text(bytes(buffers[1])), cap)
            return ValidationRun(
                name="",
                exit_code=process.returncode,
                duration_seconds=monotonic() - started,
                timed_out=timed_out,
                cancelled=cancelled or interrupted,
                stdout=out,
                stderr=err,
                stdout_truncated=truncated[0] or out_partial,
                stderr_truncated=truncated[1] or err_partial,
            )

        try:
            while process.poll() is None:
                if cancellation.cancelled:
                    cancelled = True
                    stop()
                    break
                if monotonic() - started >= timeout:
                    timed_out = True
                    stop()
                    break
                await asyncio.sleep(0.02)
            # Kill remaining descendants even when the immediate parent exits;
            # otherwise an orphan may retain pipes and outlive a nominal timeout.
            stop()
            await asyncio.to_thread(process.wait)
        except BaseException as error:
            stop()
            await asyncio.to_thread(process.wait)
            await asyncio.shield(asyncio.gather(*readers))
            if isinstance(error, asyncio.CancelledError) and on_cancel is not None:
                on_cancel(outcome(interrupted=True))
            raise
        finally:
            await asyncio.shield(asyncio.gather(*readers))
        return outcome()
    finally:
        if job:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            job.close()
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def display_text(data):
    """Plain untrusted text: strip ANSI/control sequences; templates still escape."""
    import re

    text = data.decode("utf-8", errors="replace")
    text = re.sub(
        r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))", "", text
    )
    return "".join(
        char
        for char in text
        if char in "\n\r\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159)
    )
