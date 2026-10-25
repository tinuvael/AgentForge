"""Exec a fixed host command after fchdir, without a threaded preexec_fn.

Used only with operator-selected argv and inherited authorized descriptors.
The process keeps its PID/session, so existing timeout/group cleanup still owns it.
"""

import os
import signal
import sys
from threading import Lock


def command(argv, fd):
    return [sys.executable, "-I", __file__, str(fd), *argv]


def group_stopper(process, *, signal_group=None):
    """Serialize Darwin group cleanup across output readers and the owner.

    Darwin may report EPERM for a second concurrent kill while the already-killed
    group is exiting. Only a successful group kill (or ESRCH) completes cleanup;
    actual permission failures still propagate. Do not repeat successful signals.
    """
    lock = Lock()
    stopped = False
    if signal_group is None:

        def signal_group():
            os.killpg(process.pid, signal.SIGKILL)

    def stop():
        nonlocal stopped
        with lock:
            if stopped:
                return
            try:
                signal_group()
            except PermissionError as error:
                # A naturally exiting Darwin group leader can also cause EPERM
                # until reaped. Retry only after proving that this parent exited;
                # surviving descendants still require a successful group signal.
                from subprocess import TimeoutExpired

                try:
                    process.wait(timeout=0.25)
                except TimeoutExpired:
                    raise error from None
                try:
                    signal_group()
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            stopped = True

    return stop


if __name__ == "__main__":
    descriptor = int(sys.argv[1])
    os.fchdir(descriptor)
    os.close(descriptor)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
