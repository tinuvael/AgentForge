"""Filesystem validation shared by the registry and future scoped tools."""

from pathlib import Path

from agentforge.projects.errors import InvalidProjectPath, UnsafeProjectPath


def canonical_project_root(path: str | Path, *, base_directory: Path) -> Path:
    """Resolve relative roots against an explicit base, following symlinks."""
    try:
        if not str(path).strip():
            raise InvalidProjectPath("Project root must not be empty")
        supplied = Path(path)
        root = (base_directory / supplied).resolve(strict=True)
        if not root.is_dir():
            raise InvalidProjectPath("Project root must be an existing directory")
        return root
    except (OSError, RuntimeError, ValueError):
        raise InvalidProjectPath("Project root cannot be resolved") from None


def validate_registered_root(root: Path) -> Path:
    """Fail closed if a stored canonical root vanished or became a symlink."""
    if not root.is_absolute():
        raise InvalidProjectPath("Registered project root must be absolute")
    current = canonical_project_root(root, base_directory=root.parent)
    if current != root:
        raise InvalidProjectPath("Registered project root has changed location")
    return current


def resolve_project_path(root: Path, candidate: str | Path) -> Path:
    """Resolve an existing candidate and require containment in the stored root.

    Relative candidates are rooted at the project, absolute candidates are checked
    identically. This is validation at a point in time, not protection against a
    concurrent filesystem replacement between validation and a future tool's I/O.
    """
    try:
        resolved_root = validate_registered_root(root)
        resolved = (resolved_root / candidate).resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            raise UnsafeProjectPath("Candidate path escapes the project root")
        return resolved
    except (InvalidProjectPath, OSError, RuntimeError, ValueError):
        raise UnsafeProjectPath("Project path cannot be safely resolved") from None
