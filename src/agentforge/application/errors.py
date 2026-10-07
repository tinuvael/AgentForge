"""Safe transport-independent operation errors."""

from agentforge.application.contracts import ErrorCode


class ServiceError(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)
