"""Transport-independent Project Registry operations."""

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from agentforge.db.projects import ProjectRepository
from agentforge.projects.backends import SafeFilesystemBackend, select_backend
from agentforge.projects.errors import (
    InvalidProjectName,
    InvalidProjectPath,
    ProjectAlreadyRegistered,
    ProjectNotFound,
    UnsafeProjectPath,
)
from agentforge.projects.git import inspect_git
from agentforge.projects.models import Project, ProjectInspection


class ProjectRegistry:
    def __init__(
        self,
        repository: ProjectRepository,
        *,
        base_directory: str | Path | None = None,
        filesystem: SafeFilesystemBackend | None = None,
    ) -> None:
        self._repository = repository
        self.filesystem = filesystem if filesystem is not None else select_backend()
        # Capture once so later process chdir calls cannot change registration.
        self._base_directory = self.filesystem.canonical_root(
            Path.cwd() if base_directory is None else base_directory,
            base_directory=Path.cwd(),
        )

    def register_project(self, name: str, root_path: str | Path) -> Project:
        name = name.strip()
        if not name or len(name) > 255:
            raise InvalidProjectName("Project name must contain 1 to 255 characters")
        root = self.filesystem.canonical_root(
            root_path, base_directory=self._base_directory
        )
        if self._repository.find_by_root(root) is not None:
            raise ProjectAlreadyRegistered("Project root is already registered")
        try:
            observed = self.filesystem.observe_root(root)
        except OSError:
            raise InvalidProjectPath("Project root cannot be observed") from None
        return self._repository.add(
            Project(
                uuid4(),
                name,
                root,
                datetime.now(UTC),
                int(observed.volume) if observed.kind == "posix" else None,
                int(observed.file_id) if observed.kind == "posix" else None,
                observed if observed.kind != "posix" else None,
            )
        )

    def count_projects(self) -> int:
        return self._repository.count()

    def list_projects(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[Project]:
        if (
            limit is not None and (type(limit) is not int or not 1 <= limit <= 1000)
        ) or (type(offset) is not int or offset < 0):
            raise ValueError("Invalid Project list bounds")
        return self._repository.list(limit=limit, offset=offset)

    def get_project(self, project_id: UUID | str) -> Project:
        project = self._repository.get(self._id(project_id))
        if project is None:
            raise ProjectNotFound("Project ID is not registered")
        return project

    def remove_project(self, project_id: UUID | str) -> None:
        if not self._repository.remove(self._id(project_id)):
            raise ProjectNotFound("Project ID is not registered")

    def inspect_project(self, project_id: UUID | str) -> ProjectInspection:
        with self.open_root(project_id) as (project, root_fd):
            inspection = ProjectInspection(project=project, git=inspect_git(root_fd))
        return inspection

    def resolve_path(self, project_id: UUID | str, candidate: str | Path) -> Path:
        project = self.get_project(project_id)
        return self.filesystem.resolve_path(
            project.root_path, candidate, expected=project.filesystem_identity
        )

    @contextmanager
    def open_root(self, project_id: UUID | str):
        """Provide a platform capability for the registered directory identity.

        Legacy registrations without an observed identity must be re-registered;
        silently trusting today's directory would authorize a replacement root.
        """
        project = self.get_project(project_id)
        if project.filesystem_identity is None:
            raise UnsafeProjectPath(
                "Project must be re-registered for safe file access"
            )
        with self.filesystem.anchored_root(
            project.root_path, project.filesystem_identity
        ) as fd:
            yield project, fd

    @staticmethod
    def _id(project_id: UUID | str) -> UUID:
        try:
            return project_id if isinstance(project_id, UUID) else UUID(project_id)
        except (ValueError, TypeError, AttributeError):
            raise ProjectNotFound("Project ID is not registered") from None
