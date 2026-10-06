"""Deterministic structural map ranking and whole-line budget enforcement."""

from collections import Counter, defaultdict

from agentforge.index.models import IndexSnapshot, Symbol


def approximate_tokens(text: str) -> int:
    """One approximate token per three UTF-8 bytes, rounded up.

    This is a conservative size heuristic, not a model tokenizer guarantee.
    """
    return (len(text.encode("utf-8")) + 2) // 3


def render_map(snapshot: IndexSnapshot, focus: str | None, max_tokens: int) -> str:
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens < 0
    ):
        raise ValueError("max_tokens must be a nonnegative integer")
    limit = max_tokens * 3
    if not limit:
        return ""
    symbols = {symbol.id: symbol for symbol in snapshot.symbols}
    degree: Counter[str] = Counter()
    neighbors: dict[str, set[str]] = defaultdict(set)
    for edge in snapshot.relationships:
        if edge.target_id and edge.kind != "contains":
            degree[edge.source_id] += 1
            degree[edge.target_id] += 1
            neighbors[edge.source_id].add(edge.target_id)
            neighbors[edge.target_id].add(edge.source_id)
    query = focus.strip().casefold() if focus else ""

    def relevance(symbol: Symbol) -> int:
        if not query:
            return 0
        if symbol.name.casefold() == query:
            return 100
        if symbol.qualified_name.casefold() == query:
            return 90
        if query in symbol.name.casefold():
            return 70
        if query in symbol.qualified_name.casefold():
            return 60
        if query in symbol.relative_path.casefold():
            return 40
        return 0

    scores = {s.id: relevance(s) for s in symbols.values()}
    for identity, score in list(scores.items()):
        if score:
            for neighbor in neighbors[identity]:
                scores[neighbor] = max(scores[neighbor], 20)
    files: dict[str, list[Symbol]] = defaultdict(list)
    for symbol in symbols.values():
        if not query or scores[symbol.id]:
            files[symbol.relative_path].append(symbol)

    # Sort whole subtrees, then emit preorder, so methods cannot appear under a
    # different class when importance/focus reorders top-level definitions.
    subtree_scores = dict(scores)
    for symbol in symbols.values():
        parent = symbols.get(symbol.parent_id)
        while parent:
            subtree_scores[parent.id] = max(
                subtree_scores[parent.id], scores[symbol.id]
            )
            parent = symbols.get(parent.parent_id)

    def ordered_definitions(path: str):
        selected = {symbol.id for symbol in files[path]}
        for identity in list(selected):
            parent = symbols.get(symbols[identity].parent_id)
            while parent and parent.kind != "module":
                selected.add(parent.id)
                parent = symbols.get(parent.parent_id)
        children: dict[str | None, list[Symbol]] = defaultdict(list)
        for identity in selected:
            symbol = symbols[identity]
            if symbol.kind != "module":
                parent = symbols.get(symbol.parent_id)
                key = parent.id if parent and parent.kind != "module" else None
                children[key].append(symbol)
        stack = [None]
        while stack:
            identity = stack.pop()
            if identity is not None:
                yield symbols[identity]
            ranked = sorted(
                children[identity],
                key=lambda s: (
                    -subtree_scores[s.id],
                    -degree[s.id],
                    s.start_line,
                    s.id,
                ),
            )
            stack.extend(s.id for s in reversed(ranked))

    def top_level(symbol: Symbol) -> bool:
        parent = symbols.get(symbol.parent_id)
        return parent is None or parent.kind == "module"

    def file_rank(path: str):
        items = files[path]
        return (
            -max(scores[s.id] for s in items),
            -sum(degree[s.id] for s in items),
            -sum(s.kind != "module" and top_level(s) for s in items),
            path.count("/"),
            path,
        )

    output: list[str] = []
    used = 0
    emitted: set[str] = set()
    for path in sorted(files, key=file_rank):
        header_emitted = False
        definitions = list(ordered_definitions(path))
        # Modules without selected definitions still deserve a path entry.
        candidates = definitions or [files[path][0]]
        for symbol in candidates:
            chain = []
            current = symbol
            while current and current.kind != "module":
                chain.append(current)
                current = symbols.get(current.parent_id)
            chain.reverse()
            lines = []
            if not header_emitted:
                stale = any(s.stale for s in files[path])
                lines.append(path + (" [stale: parse error]" if stale else "") + "\n")
            for depth, ancestor in enumerate(chain, 1):
                if ancestor.id in emitted:
                    continue
                suffix = "" if ancestor.kind == "class" else "()"
                lines.append(
                    "  " * depth
                    + f"{ancestor.kind} {ancestor.name}{suffix}"
                    + f" :{ancestor.start_line}\n"
                )
            chunk = "".join(lines)
            size = len(chunk.encode("utf-8"))
            if used + size > limit:
                continue
            output.append(chunk)
            used += size
            header_emitted = True
            emitted.update(s.id for s in chain)
    return "".join(output)
