"""Explicit typed tool wrappers over existing Index and RepositoryTools services."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from itertools import islice
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agentforge.core.inference import ToolDefinition
from agentforge.index.service import ProjectIndex
from agentforge.projects.errors import UnsafeProjectPath
from agentforge.tools.errors import InvalidToolArgument, SensitivePath
from agentforge.tools.policy import query_text, relative_path, require_public, utf8_size
from agentforge.tools.service import RepositoryTools


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def check_policy(self) -> None:
        """Validate safe syntax before any service/filesystem/Git call."""
        values = self.model_dump()
        for value in values.values():
            if isinstance(value, str):
                utf8_size(value)
        if "path" in values:
            require_public(relative_path(values["path"]))
        if "query" in values:
            query_text(values["query"], values.get("case_sensitive", False))


class MapArguments(Arguments):
    focus: str | None = Field(default=None, max_length=256)
    max_tokens: int = Field(default=1500, ge=1, le=4000)


class FindArguments(Arguments):
    query: str = Field(min_length=1, max_length=256)
    max_results: int = Field(default=20, ge=1, le=100)


class SymbolArguments(Arguments):
    symbol_id: str = Field(min_length=1, max_length=4096)


class RelationshipArguments(SymbolArguments):
    max_results: int = Field(default=30, ge=1, le=100)


class ListArguments(Arguments):
    path: str | None = Field(default=None, max_length=4096)
    max_results: int = Field(default=100, ge=1, le=1000)


class ReadArguments(Arguments):
    path: str = Field(min_length=1, max_length=4096)
    start_line: int = Field(default=1, ge=1, le=1_000_000)
    end_line: int | None = Field(default=None, ge=1, le=1_000_000)
    max_lines: int = Field(default=200, ge=1, le=1000)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("Invalid line range")
        return self


class GrepArguments(Arguments):
    query: str = Field(min_length=1, max_length=1024)
    path: str | None = Field(default=None, max_length=4096)
    case_sensitive: bool = False
    max_results: int = Field(default=30, ge=1, le=100)
    max_line_bytes: int = Field(default=300, ge=1, le=1000)


class SearchArguments(GrepArguments):
    glob: str | None = Field(default=None, min_length=1, max_length=256)

    def check_policy(self) -> None:
        super().check_policy()
        if self.glob is not None:
            query_text(self.glob, True)
            if len(self.glob.encode("utf-8")) > 256:
                raise ValueError("Invalid glob")


class DiffArguments(Arguments):
    path: str | None = Field(default=None, max_length=4096)
    staged: bool = False


@dataclass(frozen=True)
class Tool:
    definition: ToolDefinition
    arguments: type[Arguments]
    execute: Callable[[UUID, Arguments, int], object]


def json_text(value: object) -> str:
    """Stable UTF-8 JSON for context accounting and tool-result messages."""

    def structured(item):
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        raise TypeError("Tool results must be JSON values or dataclass records")

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=structured,
    )


def trace_arguments(arguments: Arguments) -> dict[str, JsonValue]:
    # Keep shape, limits and flags; free text can contain user/model secrets.
    def redacted(value):
        if isinstance(value, str):
            return "<redacted>"
        if isinstance(value, dict):
            return {key: redacted(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redacted(item) for item in value]
        return value

    return {key: redacted(value) for key, value in arguments.model_dump().items()}


def public_index_path(path: str) -> bool:
    try:
        require_public(relative_path(path))
    except (UnsafeProjectPath, InvalidToolArgument, SensitivePath):
        return False
    return True


def repository_toolset(
    index: ProjectIndex, repository: RepositoryTools
) -> dict[str, Tool]:
    """No runtime attribute lookup from a model-supplied name."""
    tools: dict[str, Tool] = {}

    def add(name, description, schema, execute):
        tools[name] = Tool(
            ToolDefinition(
                name=name,
                description=description,
                parameters=schema.model_json_schema(),
            ),
            schema,
            execute,
        )

    def bounded_items(items, count):
        selected = list(islice(items, count + 1))
        return {"items": selected[:count], "truncated": len(selected) > count}

    def symbol(project, identity):
        value = index.get_symbol(project, identity)
        require_public(relative_path(value.relative_path))
        return value

    def relationships(operation, project, args):
        symbol(project, args.symbol_id)
        edges = operation(project, args.symbol_id)
        visible = (
            edge
            for edge in edges
            if public_index_path(
                index.get_symbol(project, edge.source_id).relative_path
            )
            and (
                edge.target_id is None
                or public_index_path(
                    index.get_symbol(project, edge.target_id).relative_path
                )
            )
        )
        return bounded_items(visible, args.max_results)

    add(
        "get_project_map",
        "Cached structural navigation; read source for truth.",
        MapArguments,
        lambda p, a, b: {
            "map": index.render_project_map(
                p, a.focus, min(a.max_tokens, b // 6), path_filter=public_index_path
            )
        },
    )
    add(
        "find_symbol",
        "Find cached definitions; returns all matches up to a limit.",
        FindArguments,
        lambda p, a, b: bounded_items(
            (
                s
                for s in index.find_symbol(p, a.query)
                if public_index_path(s.relative_path)
            ),
            a.max_results,
        ),
    )
    add(
        "get_symbol",
        "Get cached symbol path and source line span.",
        SymbolArguments,
        lambda p, a, b: symbol(p, a.symbol_id),
    )
    for name, operation in (
        ("get_dependencies", index.get_dependencies),
        ("get_dependents", index.get_dependents),
        ("get_related_symbols", index.get_related_symbols),
    ):
        add(
            name,
            "Cached typed structural edges; not runtime dispatch proof.",
            RelationshipArguments,
            lambda p, a, b, operation=operation: relationships(operation, p, a),
        )
    # Reserve half the result budget for JSON escaping and metadata. Runtime checks
    # serialized size as well; no silently clipped JSON/evidence is sent to models.
    add(
        "list_files",
        "List public project-relative files.",
        ListArguments,
        lambda p, a, b: repository.list_files(
            p, **a.model_dump(), max_bytes=max(1, b // 2)
        ),
    )
    add(
        "read_file",
        "Read UTF-8 source, inclusive 1-based lines, with truncation metadata.",
        ReadArguments,
        lambda p, a, b: repository.read_file(
            p, **a.model_dump(), max_bytes=max(1, b // 2)
        ),
    )
    add(
        "search_code",
        "Literal search of working files, with paths and line numbers.",
        SearchArguments,
        lambda p, a, b: repository.search_code(
            p, **a.model_dump(), max_bytes=max(1, b // 2)
        ),
    )
    add(
        "git_grep",
        "Literal search of Git index contents (not unstaged/untracked text).",
        GrepArguments,
        lambda p, a, b: repository.git_grep(
            p, **a.model_dump(), max_bytes=max(1, b // 2)
        ),
    )
    add(
        "git_status",
        "Read-only Git status, scoped to this project.",
        ListArguments,
        lambda p, a, b: repository.git_status(
            p, **a.model_dump(), max_bytes=max(1, b // 2)
        ),
    )
    add(
        "git_diff",
        "Read-only staged or unstaged diff, scoped to this project.",
        DiffArguments,
        lambda p, a, b: repository.git_diff(
            p, **a.model_dump(), max_bytes=max(1, b // 2)
        ),
    )
    return tools
