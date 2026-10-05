"""Decide which tests to run for a change, and say why."""

from __future__ import annotations

import ast
import fnmatch
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .config import Config
from .functions import MODULE, FunctionIndex
from .gitdiff import FileChange, changes, git, is_ancestor, show
from .testmap import TestMap


@dataclass
class Selection:
    tests: set[str] = field(default_factory=set)
    test_files: set[str] = field(default_factory=set)  # changed test files: run whole, including new tests
    run_all: str | None = None  # reason, when everything must run
    uncovered: list[str] = field(default_factory=list)  # changed functions that no recorded test ran
    why: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # test -> changes that picked it
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def pick(self, tests: set[str], reason: str) -> None:
        self.tests |= tests
        for t in tests:
            self.why[t].add(reason)

    def includes(self, nodeid: str) -> bool:
        """Should this collected test run? Unknown tests (new, renamed, new parameters) always run."""
        if self.run_all:
            return True
        return nodeid in self.tests or _file_of(nodeid) in self.test_files

    def chosen(self, tmap: TestMap) -> set[str]:
        if self.run_all:
            return set(tmap.tests)
        return self.tests | {t for t in tmap.tests if _file_of(t) in self.test_files}

    def estimated_seconds(self, tmap: TestMap) -> float:
        return sum(tmap.durations.get(t, 0.0) for t in self.chosen(tmap))

    def priority(self, nodeid: str, tmap: TestMap) -> tuple[int, int, float]:
        """Sort key: tests most likely to fail first, so a broken change fails fast.

        Changed or new tests first, then tests touching the most changed code, then quick ones."""
        in_changed_file = 0 if _file_of(nodeid) in self.test_files else 1
        return (in_changed_file, -len(self.why.get(nodeid, ())), tmap.durations.get(nodeid, 0.0))


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _match(path: str, patterns: list[str]) -> bool:
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns)


@dataclass
class _Stmt:
    start: int
    end: int
    dump: str
    names: set[str]


class _Version:
    """One version of a Python file: which function each line is in, and what each piece of code is."""

    def __init__(self, src: str):
        self.text = src.splitlines()
        self.index = FunctionIndex(src)
        if not self.index.ok:
            raise SyntaxError("does not parse")
        tree = ast.parse(src)
        self.funcs: dict[str, str] = {}
        self.stmts: list[_Stmt] = []
        self._walk(tree.body, [], in_class=False)

    def _walk(self, body: list[ast.stmt], prefix: list[str], in_class: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = [*prefix, node.name]
                self.funcs[".".join(name)] = ast.dump(node)
                self._nested(node, name)
                continue
            if isinstance(node, ast.ClassDef):
                header_end = node.body[0].lineno - 1 if node.body else node.end_lineno or node.lineno
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                header = ast.dump(
                    ast.ClassDef(
                        name=node.name,
                        bases=node.bases,
                        keywords=node.keywords,
                        body=[],
                        decorator_list=node.decorator_list,
                        type_params=getattr(node, "type_params", []),
                    )
                )
                self.stmts.append(_Stmt(start, max(start, header_end), header, {node.name}))
                self._walk(node.body, [*prefix, node.name], in_class=True)
                continue
            names: set[str] = set()
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = {(a.asname or a.name).split(".")[0] for a in node.names}
            else:
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            if in_class and prefix:
                names = {prefix[0]}  # a class attribute is reached through its (top-level) class
            self.stmts.append(_Stmt(node.lineno, node.end_lineno or node.lineno, ast.dump(node), names))

    def _nested(self, fn: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(fn):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = [*prefix, child.name]
                self.funcs[".".join(name)] = ast.dump(child)
                self._nested(child, name)
            elif isinstance(child, ast.ClassDef):
                self._nested(child, [*prefix, child.name])
            else:
                self._nested(child, prefix)

    def code_lines(self, lines: set[int]) -> set[int]:
        out = set()
        for ln in lines:
            code = self.text[ln - 1].strip() if 0 < ln <= len(self.text) else ""
            if code and not code.startswith("#"):
                out.add(ln)  # blank lines and comments can't change behaviour
        return out

    def stmts_at(self, lines: set[int]) -> list[_Stmt]:
        return [s for s in self.stmts if any(s.start <= ln <= s.end for ln in lines)]


def _analyse(repo: str, base: str, head: str | None, ch: FileChange) -> tuple[set[str], set[str]]:
    """(changed function names, top-level names whose module-level code changed) for one .py file.

    Code that parses to the same syntax tree on both sides (reformatting, comments) is not a change."""
    old = _Version(show(repo, base, ch.old_path) or "") if ch.old_path else None
    new = _Version(show(repo, head, ch.new_path) or "") if ch.new_path else None
    funcs: set[str] = set()
    old_hits: list[_Stmt] = []
    new_hits: list[_Stmt] = []
    for v, lines, hits in ((old, ch.old_lines, old_hits), (new, ch.new_lines, new_hits)):
        if v is None:
            continue
        lines = v.code_lines(lines)
        for ln in lines:
            name = v.index.at(ln)
            if name != MODULE:
                funcs.add(name)
        hits.extend(v.stmts_at({ln for ln in lines if v.index.at(ln) == MODULE}))
    if old and new:
        funcs = {f for f in funcs if old.funcs.get(f) != new.funcs.get(f) or f not in old.funcs}
    old_dumps = Counter(s.dump for s in old_hits)
    new_dumps = Counter(s.dump for s in new_hits)
    differ = set((old_dumps - new_dumps) + (new_dumps - old_dumps))
    names: set[str] = set()
    for s in old_hits + new_hits:
        if s.dump in differ:
            names |= s.names or {MODULE}
    return funcs, names


class _ImportGraph:
    """Who imports what, so a changed module-level name leads to the files that use it.

    A file uses name ``N`` of module ``M`` if it does ``from M import N`` (or ``*``), or imports
    ``M`` itself and reads ``M.N``. Re-exports through package ``__init__`` files are followed.
    Built lazily, once per selection, from the version of the code being tested."""

    def __init__(self, repo: str, rev: str | None):
        self.repo, self.rev = repo, rev
        self._built = False
        self.module_of: dict[str, str] = {}  # path -> dotted module name
        # module -> [(file, imported name or None for the module itself, local alias)]
        self.importers: dict[str, list[tuple[str, str | None, str]]] = defaultdict(list)
        self.attrs: dict[str, dict[str, set[str]]] = {}  # file -> local name -> attributes read on it

    def _build(self) -> None:
        self._built = True
        if self.rev is None:
            listing = git(self.repo, "ls-files", "--cached", "--others", "--exclude-standard")
        else:
            listing = git(self.repo, "ls-tree", "-r", "--name-only", self.rev)
        paths = [p for p in listing.splitlines() if p.endswith(".py")]
        packages = {os.path.dirname(p) for p in paths if os.path.basename(p) == "__init__.py"}
        for p in paths:
            parts = [os.path.splitext(os.path.basename(p))[0]]
            d = os.path.dirname(p)
            while d in packages:
                parts.insert(0, os.path.basename(d))
                d = os.path.dirname(d)
            if parts[-1] == "__init__":
                parts.pop()
            self.module_of[p] = ".".join(parts)
        for p in paths:
            try:
                tree = ast.parse(show(self.repo, self.rev, p) or "")
            except (SyntaxError, ValueError):
                continue
            pkg = self.module_of[p] if p.endswith("__init__.py") else self.module_of[p].rpartition(".")[0]
            attrs: dict[str, set[str]] = defaultdict(set)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        self.importers[a.name].append((p, None, a.asname or a.name.split(".")[0]))
                        if a.asname is None:  # "import a.b.c" binds "a"; record each parent too
                            bits = a.name.split(".")
                            for i in range(1, len(bits)):
                                self.importers[".".join(bits[:i])].append((p, None, bits[0]))
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        up = pkg.split(".") if pkg else []
                        up = up[: len(up) - (node.level - 1)] if node.level > 1 else up
                        base = ".".join([*up, *([base] if base else [])])
                    for a in node.names:
                        self.importers[base].append((p, a.name, a.asname or a.name))
                        # "from pkg import mod" imports a module too
                        self.importers[f"{base}.{a.name}"].append((p, None, a.asname or a.name))
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    attrs[node.value.id].add(node.attr)
            self.attrs[p] = attrs

    def imported(self, path: str) -> bool:
        """Does any file in the repo import this one?"""
        if not self._built:
            self._build()
        module = self.module_of.get(path)
        if not module:
            return False
        return any(m == module or m.startswith(module + ".") for m in self.importers)

    def users(self, path: str, names: set[str]) -> set[str]:
        """Files that use any of ``names`` defined at module level in ``path``."""
        if not self._built:
            self._build()
        module = self.module_of.get(path)
        if module is None:
            return set()
        found: set[str] = set()
        todo = [(module, frozenset(names))]
        seen: set[tuple[str, frozenset[str]]] = set()
        while todo:
            mod, ns = todo.pop()
            if (mod, ns) in seen:
                continue
            seen.add((mod, ns))
            for f, imported, alias in self.importers.get(mod, ()):
                if imported is None:
                    if ns & self.attrs.get(f, {}).get(alias, set()) or not ns:
                        found.add(f)
                elif imported == "*" or imported in ns:
                    found.add(f)
                    if f.endswith("__init__.py"):  # re-exported: follow importers of the package
                        todo.append((self.module_of[f], frozenset({alias} if imported != "*" else ns)))
        return found


def select(repo: str, tmap: TestMap, base: str, head: str | None = None, cfg: Config | None = None) -> Selection:
    """Tests affected by the changes from ``base`` to ``head`` (``None`` = the working tree)."""
    cfg = cfg or Config.load(repo)
    sel = Selection()
    if not is_ancestor(repo, tmap.commit, base):
        sel.warnings.append(
            f"the map was recorded at {tmap.commit[:9]}, which is not an ancestor of {base}; "
            "changes between them are not traced (record a fresh map on your main branch)"
        )
    names = _ImportGraph(repo, head)
    renamed = _renames_since(repo, tmap.commit, head)
    for ch in changes(repo, base, head):
        try:
            _select_file(repo, tmap, base, head, ch, cfg, sel, names, renamed)
        except SyntaxError:
            sel.run_all = f"could not parse {ch.path}"
    return sel


def _renames_since(repo: str, since: str, head: str | None) -> dict[str, str]:
    """Files renamed since the map was recorded: new path -> path the map knows them by."""
    try:
        out = git(repo, "diff", "-M", "--name-status", "--diff-filter=R", "-z", since, *([head] if head else []))
    except Exception:
        return {}
    parts = out.split("\0")
    renames: dict[str, str] = {}
    i = 0
    while i + 2 < len(parts):
        if parts[i].startswith("R"):
            renames[parts[i + 2]] = parts[i + 1]
        i += 3
    return renames


def _select_file(
    repo: str,
    tmap: TestMap,
    base: str,
    head: str | None,
    ch: FileChange,
    cfg: Config,
    sel: Selection,
    names: _ImportGraph,
    renamed: dict[str, str],
) -> None:
    path = ch.path
    if _match(path, cfg.run_all):
        sel.run_all = f"{path} changed"
        return
    if not path.endswith(".py"):
        # Data files: tests seen reading them run, whatever the file type.
        readers = tmap.tests_reading(ch.old_path or path) | tmap.tests_reading(path)
        if readers:
            sel.pick(readers, f"{path} (read by these tests)")
        elif not _match(path, cfg.ignore):
            sel.notes.append(f"{path}: no recorded test reads it")
        return
    if _match(path, cfg.ignore):
        return

    readers = tmap.tests_reading(ch.old_path or path) | tmap.tests_reading(path)
    if readers:  # .py files read as data, or imported lazily during a test
        sel.pick(readers, f"{path} (opened by these tests)")
    is_test = _match(path, cfg.test_files)
    if is_test and ch.new_path:
        sel.test_files.add(ch.new_path)
    if ch.new_path is None:  # deleted module: whatever used it is affected (and its importers changed too)
        sel.pick(tmap.tests_for_file(ch.old_path or ""), f"{ch.old_path} deleted")
        return
    if ch.old_path is None and not is_test:
        sel.notes.append(f"{path}: new module; only code that imports it can reach it, and that code changed too")
    # the map knows functions under the old path; look new-side names up there too (renames)
    map_path = ch.old_path or ch.new_path or path
    map_path = renamed.get(map_path, map_path)
    try:
        funcs, bound = _analyse(repo, base, head, ch)
    except SyntaxError:
        # Not valid Python. Inside a package it's broken source: anything importing it breaks,
        # so run everything. Elsewhere it's test data (e.g. formatter test cases): its readers ran above.
        if os.path.exists(os.path.join(repo, os.path.dirname(path), "__init__.py")) or is_test:
            sel.run_all = f"{path} does not parse"
        elif not readers:
            sel.notes.append(f"{path}: not valid Python and no recorded test reads it")
        return
    for name in sorted(funcs):
        key = f"{map_path}::{name}"
        found = tmap.tests_for(key)
        if found:
            sel.pick(found, key)
        elif not is_test:
            sel.uncovered.append(f"{path}::{name}")
    if not bound:
        return
    # Module-level code changed: imports, constants, class attributes. Pick tests that ran this
    # file's functions, plus tests that ran code in any file that uses the changed names.
    if os.path.basename(path) == "conftest.py":
        scope = os.path.dirname(path)
        sel.pick({t for t in tmap.tests if t.startswith(scope + "/") or not scope}, f"{path} module level (conftest scope)")
        return
    users: set[str] = {map_path} | names.users(path, bound - {MODULE})
    picked: set[str] = set()
    for u in users:
        picked |= tmap.tests_for_file(u)
    if picked:
        sel.pick(picked, f"{path} module level ({', '.join(sorted(bound - {MODULE})) or 'statements'})")
    elif not is_test and not readers:
        if not names.imported(path) and not tmap.knows_path(map_path):
            # Nothing imports it and no test ran it: a script or a .py file used as data (a formatter's
            # test case, say). Tests that read it were picked above; tests generated from a new
            # one aren't in the map, so they run anyway.
            sel.notes.append(f"{path}: not imported by anything, treated as data")
        else:
            sel.run_all = f"module-level change in {path}, and no recorded test ran code that uses it"
