"""Static operator configuration for the synthetic Codex Worker."""

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SSHCodexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    host: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$")
    user: str = Field(pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    project_id: UUID
    ssh_executable: Path = Field(repr=False)
    identity_file: Path = Field(repr=False)
    known_hosts_file: Path = Field(repr=False)

    @field_validator("ssh_executable", "identity_file", "known_hosts_file")
    @classmethod
    def safe_absolute_path(cls, path: Path) -> Path:
        if not path.is_absolute() or any(
            char in '%$"~' or ord(char) < 32 for char in str(path)
        ):
            raise ValueError("Supply an absolute, literal local path")
        return path

    def check_local_files(self) -> None:
        """Run under the service identity. Never read or print credential bytes."""
        for path in (self.ssh_executable, self.identity_file, self.known_hosts_file):
            if not path.is_file():
                raise OSError("Required SSH file is unavailable")
            with path.open("rb"):
                pass


class SSHRepositoryPin(BaseModel):
    """Identity agreed independently by the Gateway and the Linux launcher."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    repository: str = Field(
        max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
