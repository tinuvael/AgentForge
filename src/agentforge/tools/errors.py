"""Safe public failures; ProjectNotFound/UnsafeProjectPath retain their meanings."""


class RepositoryToolError(Exception):
    """Repository capability failed without exposing backend diagnostics."""


class InvalidToolArgument(RepositoryToolError):
    pass


class PathNotFound(RepositoryToolError):
    pass


class UnsupportedTextFile(RepositoryToolError):
    pass


class SensitivePath(RepositoryToolError):
    pass


class RepositoryIOError(RepositoryToolError):
    pass


class GitUnavailable(RepositoryToolError):
    pass


class NotGitRepository(RepositoryToolError):
    pass


class GitTimeout(RepositoryToolError):
    pass


class GitFailure(RepositoryToolError):
    pass
