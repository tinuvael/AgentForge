"""Public Project Registry failures, independent of transport and database APIs."""


class ProjectError(Exception):
    """Base class for project operations."""


class InvalidProjectPath(ProjectError):
    """The supplied project root is not an accessible existing directory."""


class InvalidProjectName(ProjectError):
    """A human-readable project name must not be blank."""


class ProjectAlreadyRegistered(ProjectError):
    """The canonical directory already has a registry entry."""


class ProjectNotFound(ProjectError):
    """No project exists with the supplied stable ID."""


class UnsafeProjectPath(ProjectError):
    """A candidate escapes the root or cannot be safely resolved."""


class ProjectStorageError(ProjectError):
    """Persistence failed; database details are not part of the public contract."""
