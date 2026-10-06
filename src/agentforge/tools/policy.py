"""Validated small contracts and defense-in-depth secret exclusions."""

from pathlib import PurePosixPath

from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.exclusions import excluded_directory
from agentforge.tools.errors import InvalidToolArgument, SensitivePath

DEFAULT_OUTPUT_BYTES = 64 * 1024
HARD_OUTPUT_BYTES = 1024 * 1024
DEFAULT_FILES = 1000
HARD_FILES = 10_000
DEFAULT_LINES = 1000
HARD_LINES = 10_000
DEFAULT_RESULTS = 100
HARD_RESULTS = 1000
DEFAULT_LINE_BYTES = 500
HARD_LINE_BYTES = 2000
FILE_SCAN_BYTES = 2 * 1024 * 1024
TOTAL_SEARCH_BYTES = 64 * 1024 * 1024
SCAN_ENTRIES = 100_000

_PRIVATE_DIRECTORIES = {".secrets", ".ssh", ".aws", ".gnupg", ".git", ".hg", ".svn"}
_PRIVATE_NAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}


def limit(value: int, hard: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= hard:
        raise InvalidToolArgument(f"{name} must be an integer from 1 to {hard}")
    return value


def utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        raise InvalidToolArgument("Arguments must contain valid UTF-8 text") from None


def relative_path(path: str | None) -> tuple[str, ...]:
    if path is None or path == ".":
        return ()
    if not isinstance(path, str) or not path or utf8_size(path) > 4096:
        raise InvalidToolArgument("Path must be a bounded project-relative string")
    if (
        PurePosixPath(path).is_absolute()
        or ".." in path.split("/")
        or "\\" in path
        or ":" in path
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
    ):
        raise UnsafeProjectPath("Path must remain relative to the registered project")
    return tuple(PurePosixPath(path).parts)


def sensitive(parts: tuple[str, ...]) -> bool:
    for component in parts:
        name = component.lower()
        if (
            name in _PRIVATE_DIRECTORIES
            or name in _PRIVATE_NAMES
            or name == ".env"
            or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
        ):
            return True
    return False


def require_public(parts: tuple[str, ...]) -> None:
    if sensitive(parts):
        raise SensitivePath("Sensitive repository paths are denied")


def automatic(parts: tuple[str, ...]) -> bool:
    return not sensitive(parts) and not any(excluded_directory(p) for p in parts[:-1])


def query_text(query: str, case_sensitive: bool) -> None:
    if (
        not isinstance(query, str)
        or not query
        or utf8_size(query) > 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in query)
        or type(case_sensitive) is not bool
    ):
        raise InvalidToolArgument("Query must be 1 to 1024 UTF-8 bytes on one line")


def clip(text: str, budget: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    return encoded[:budget].decode("utf-8", errors="ignore"), len(encoded) > budget
