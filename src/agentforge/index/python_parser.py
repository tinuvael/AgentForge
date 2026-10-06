"""Python AST extraction only; no filesystem, SQLAlchemy or model calls."""

import ast
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from agentforge.index.models import ParsedFile, ParsedRelationship, Symbol

Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def module_name(relative_path: str) -> str:
    parts = list(PurePosixPath(relative_path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or "__root__"


class _Bindings(ast.NodeVisitor):
    """Conservative lexical blockers, without descending into child scopes."""

    def __init__(self, node: ast.Module | Definition):
        self.definitions: dict[str, list[Definition]] = defaultdict(list)
        self.blocked: set[str] = set()
        self.assigned: set[str] = set()
        self.attributes: set[str] = set()
        self.direct = {id(statement) for statement in node.body}
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            self.blocked.update(
                arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]
            )
            self.blocked.update(arg.arg for arg in (args.vararg, args.kwarg) if arg)
        for statement in node.body:
            self.visit(statement)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.blocked.add(node.id)
            self.assigned.add(node.id)

    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.ctx, ast.Store | ast.Del):
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                self.attributes.add(node.attr)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: Definition):
        self.definitions[node.name].append(node)
        self.assigned.add(node.name)
        if id(node) not in self.direct or node.decorator_list:
            self.blocked.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        pass

    def visit_Import(self, node: ast.Import):
        names = {alias.asname or alias.name.split(".")[0] for alias in node.names}
        self.blocked.update(names)
        self.assigned.update(names)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        names = {alias.asname or alias.name for alias in node.names}
        self.blocked.update(names)
        self.assigned.update(names)
        if any(alias.name == "*" for alias in node.names):
            # A star import may replace any local binding.
            self.blocked.add("*")

    def visit_Global(self, node: ast.Global):
        self.blocked.update(node.names)
        self.assigned.update(node.names)

    visit_Nonlocal = visit_Global

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.name:
            self.blocked.add(node.name)
            self.assigned.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs):
        if node.name:
            self.blocked.add(node.name)
            self.assigned.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar):
        if node.name:
            self.blocked.add(node.name)
            self.assigned.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping):
        if node.rest:
            self.blocked.add(node.rest)
            self.assigned.add(node.rest)
        self.generic_visit(node)


@dataclass
class _Scope:
    node: ast.Module | Definition
    symbol: Symbol
    parent: "_Scope | None"
    bindings: _Bindings
    children: dict[int, "_Scope"] = field(default_factory=dict)


class _Extractor(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.Module):
        self.path = path
        self.module = module_name(path)
        symbol = Symbol(
            "module:" + path,
            path,
            "module",
            self.module,
            self.module,
            1,
            max((getattr(n, "end_lineno", 1) for n in tree.body), default=1),
            # The root initializer has a display namespace, not a known absolute
            # Python package name. Its children can still resolve relative imports.
            import_candidate=path != "__init__.py",
        )
        self.scope = _Scope(tree, symbol, None, _Bindings(tree))
        self.root = self.scope
        self.symbols = [symbol]
        self.relationships: list[ParsedRelationship] = []

    def visit_FunctionDef(self, node: Definition):
        parent = self.scope
        kind = (
            "class"
            if isinstance(node, ast.ClassDef)
            else ("method" if parent.symbol.kind == "class" else "function")
        )
        qualified = parent.symbol.qualified_name + "." + node.name
        identity = f"{self.path}:{kind}:{qualified}:{node.lineno}"
        symbol = Symbol(
            hashlib.sha256(identity.encode()).hexdigest(),
            self.path,
            kind,
            node.name,
            qualified,
            node.lineno,
            node.end_lineno,
            parent.symbol.id,
            import_candidate=(
                parent.symbol.kind == "module"
                and self.path != "__init__.py"
                and node.name not in parent.bindings.blocked
                and "*" not in parent.bindings.blocked
                and len(parent.bindings.definitions[node.name]) == 1
            ),
        )
        self.symbols.append(symbol)
        self.relationships.append(
            ParsedRelationship(parent.symbol.id, "contains", qualified, symbol.id)
        )
        scope = _Scope(node, symbol, parent, _Bindings(node))
        parent.children[id(node)] = scope
        self.scope = scope
        for statement in node.body:
            self.visit(statement)
        self.scope = parent

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            text = (
                "import " + alias.name + (" as " + alias.asname if alias.asname else "")
            )
            self.relationships.append(
                ParsedRelationship(self.scope.symbol.id, "imports", text, alias.name)
            )

    def visit_ImportFrom(self, node: ast.ImportFrom):
        textual_module = "." * node.level + (node.module or "")
        base = node.module or ""
        root_relative = False
        if node.level:
            package = self.module.split(".")
            if PurePosixPath(self.path).name != "__init__.py":
                package.pop()
            if node.level > len(package):
                base = ""  # Relative import escapes the known root-relative namespace.
            else:
                prefix = package[: len(package) - node.level + 1]
                if self.path == "__init__.py":
                    prefix = []
                    root_relative = True
                base = ".".join([*prefix, *base.split(".")])
                base = base.rstrip(".")
        for alias in node.names:
            text = f"from {textual_module} import {alias.name}"
            if alias.asname:
                text += " as " + alias.asname
            key = None
            if alias.name != "*" and (base or root_relative):
                key = f"{base}.{alias.name}" if base else alias.name
            self.relationships.append(
                ParsedRelationship(self.scope.symbol.id, "imports", text, key)
            )

    def visit_Lambda(self, node):
        pass


class _Calls(ast.NodeVisitor):
    def __init__(self, extractor: _Extractor):
        self.extractor = extractor
        self.scope = extractor.root

    def visit_FunctionDef(self, node: Definition):
        parent = self.scope
        self.scope = parent.children[id(node)]
        for statement in node.body:
            self.visit(statement)
        self.scope = parent

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def _definition(self, scope: _Scope, name: str) -> Symbol | None:
        bindings = scope.bindings
        if name in bindings.blocked or "*" in bindings.blocked:
            return None
        definitions = bindings.definitions.get(name, [])
        if len(definitions) == 1:
            return scope.children[id(definitions[0])].symbol
        return None

    def _local(self, name: str) -> Symbol | None:
        scope = self.scope
        # Bare names in a class body have different runtime lookup rules; omit.
        if scope.symbol.kind == "class":
            return None
        while scope:
            if scope.symbol.kind != "class":
                if name in scope.bindings.blocked or "*" in scope.bindings.blocked:
                    return None
                if name in scope.bindings.definitions:
                    return self._definition(scope, name)
            scope = scope.parent
        return None

    def _method(self, name: str) -> Symbol | None:
        scope = self.scope
        node = scope.node
        if scope.symbol.kind != "method" or not scope.parent:
            return None
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return None
        args = [*node.args.posonlyargs, *node.args.args]
        if not args or args[0].arg != "self" or node.decorator_list:
            return None
        class_node = scope.parent.node
        if not isinstance(class_node, ast.ClassDef):
            return None
        if (
            class_node.bases
            or class_node.keywords
            or class_node.decorator_list
            or "__getattribute__" in scope.parent.bindings.definitions
        ):
            return None
        if "self" in scope.bindings.assigned or "*" in scope.bindings.blocked:
            return None
        # Methods changed through self anywhere in this class are uncertain.
        if any(
            name in child.bindings.attributes
            for child in scope.parent.children.values()
        ):
            return None
        return self._definition(scope.parent, name)

    def visit_Call(self, node: ast.Call):
        target = None
        text = None
        if isinstance(node.func, ast.Name):
            text = node.func.id
            target = self._local(text)
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            text = "self." + node.func.attr
            target = self._method(node.func.attr)
        if text:
            self.extractor.relationships.append(
                ParsedRelationship(
                    self.scope.symbol.id, "calls", text, target.id if target else None
                )
            )
        self.generic_visit(node)

    def visit_Lambda(self, node):
        pass

    # Comprehensions have implicit scopes; don't infer their bindings in v1.
    visit_ListComp = visit_Lambda
    visit_SetComp = visit_Lambda
    visit_DictComp = visit_Lambda
    visit_GeneratorExp = visit_Lambda


def parse_python(relative_path: str, content: bytes) -> ParsedFile:
    """Bytes let AST honor Python's encoding cookies; source is never executed."""
    tree = ast.parse(content, filename=relative_path)
    extractor = _Extractor(relative_path, tree)
    extractor.visit(tree)
    _Calls(extractor).visit(tree)
    return ParsedFile(
        extractor.module, tuple(extractor.symbols), tuple(extractor.relationships)
    )
