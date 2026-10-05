"""Name every function in a Python file, and find which function a given line belongs to."""

from __future__ import annotations

import ast
from bisect import bisect_right
from dataclasses import dataclass

MODULE = "<module>"


@dataclass(frozen=True)
class Span:
    qualname: str
    start: int  # first line, including decorators
    end: int


def normalize_qualname(qualname: str) -> str:
    """Turns a code object's ``__qualname__`` into the name the source index uses.

    ``f.<locals>.g`` -> ``f.g``; lambdas, comprehensions and generator expressions belong to
    the function they are written in: ``A.m.<locals>.<lambda>`` -> ``A.m``.
    """
    parts = [p for p in qualname.split(".") if p != "<locals>"]
    while parts and parts[-1].startswith("<"):
        parts.pop()
    return ".".join(parts) or MODULE


class FunctionIndex:
    """Maps a line number to the innermost function or method containing it.

    Lines outside any function (imports, constants, class bodies) map to ``<module>``: they
    run once at import time, so a change there can affect anything that uses the file.
    """

    def __init__(self, source: str):
        self.spans: list[Span] = []
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            self.ok = False
            self._starts: list[int] = []
            return
        self.ok = True
        self._walk(tree, [])
        self.spans.sort(key=lambda s: (s.start, -s.end))
        self._starts = [s.start for s in self.spans]

    def _walk(self, node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = [*prefix, child.name]
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                self.spans.append(Span(".".join(name), start, child.end_lineno or child.lineno))
                self._walk(child, name)
            elif isinstance(child, ast.ClassDef):
                self._walk(child, [*prefix, child.name])
            else:
                self._walk(child, prefix)

    def at(self, line: int) -> str:
        i = bisect_right(self._starts, line)
        best: Span | None = None
        for s in reversed(self.spans[:i]):
            if s.start <= line <= s.end and (best is None or s.end - s.start < best.end - best.start):
                best = s
        return best.qualname if best else MODULE

    def names(self) -> set[str]:
        return {s.qualname for s in self.spans}
