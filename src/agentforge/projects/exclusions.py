"""Shared automatic traversal policy; explicit source access is separate."""

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
        "dist",
        "build",
        "vendor",
        "third_party",
        ".idea",
        ".vscode",
        ".cache",
        ".secrets",
        ".ipynb_checkpoints",
        ".eggs",
        "site-packages",
    }
)


def excluded_directory(name: str) -> bool:
    return name in EXCLUDED_DIRECTORIES or name.endswith(".egg-info")
