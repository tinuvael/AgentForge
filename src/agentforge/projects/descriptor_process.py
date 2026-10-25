"""Exec a fixed host command after fchdir, without a threaded preexec_fn.

Used only with operator-selected argv and inherited authorized descriptors.
The process keeps its PID/session, so existing timeout/group cleanup still owns it.
"""

import os
import sys


def command(argv, fd):
    return [sys.executable, "-I", __file__, str(fd), *argv]


if __name__ == "__main__":
    descriptor = int(sys.argv[1])
    os.fchdir(descriptor)
    os.close(descriptor)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
