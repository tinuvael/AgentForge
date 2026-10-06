"""Transport-independent Project Registry operations."""

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from agentforge.db.projects import ProjectRepository
from agentforge.projects.errors import (
    InvalidProjectName,
    InvalidProjectPath,
    ProjectAlreadyRegistered,
    ProjectNotFound,
    UnsafeProjectPath,
)
from agentforge.projects.filesystem import anchored_root
from agentforge.projects.git import inspect_git
from agentforge.projects.models import Project, ProjectInspection
from agentforge.projects.paths import (
    canonical_project_root,
    resolve_project_path,
)


class ProjectRegistry:
    def __init__(
        self, repository: ProjectRepository, *, base_directory: str | Path | None = None
    ) -> None:
        self._repository = repository
        # Capture once so later process chdir calls cannot change registration.
        self._base_directory = canonical_project_root(
            Path.cwd() if base_directory is None else base_directory,
            base_directory=Path.cwd(),
        )

    def register_project(self, name: str, root_path: str | Path) -> Project:
        name = name.strip()
        if not name or len(name) > 255:
            raise InvalidProjectName("Project name must contain 1 to 255 characters")
        root = canonical_project_root(root_path, base_directory=self._base_directory)
        if self._repository.find_by_root(root) is not None:
            raise ProjectAlreadyRegistered("Project root is already registered")
        try:
            observed = root.stat()
        except OSError:
            raise InvalidProjectPath("Project root cannot be observed") from None
        return self._repository.add(
            Project(
                uuid4(), name, root, datetime.now(UTC), observed.st_dev, observed.st_ino
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
        return resolve_project_path(self.get_project(project_id).root_path, candidate)

    @contextmanager
    def open_root(self, project_id: UUID | str):
        """Provide a no-follow descriptor for the registered directory identity.

        Legacy registrations without an observed identity must be re-registered;
        silently trusting today's directory would authorize a replacement root.
        """
        project = self.get_project(project_id)
        if project.root_device is None or project.root_inode is None:
            raise UnsafeProjectPath(
                "Project must be re-registered for safe file access"
            )
        with anchored_root(
            project.root_path, (project.root_device, project.root_inode)
        ) as fd:
            yield project, fd

    @staticmethod
    def _id(project_id: UUID | str) -> UUID:
        try:
            return project_id if isinstance(project_id, UUID) else UUID(project_id)
        except (ValueError, TypeError, AttributeError):
            raise ProjectNotFound("Project ID is not registered") from None
