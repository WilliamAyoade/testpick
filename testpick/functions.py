"""Find which function (by qualified name) each line of a Python file belongs to."""
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


class FunctionIndex:
    """Maps a line number to the innermost function or method containing it.

    Lines outside any function (imports, constants, class bodies) map to "<module>":
    a change there can affect anything that imports the file.
    """

    def __init__(self, source: str):
        self.spans: list[Span] = []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            self.ok = False
            return
        self.ok = True
        self._walk(tree, [])
        # sort so that inner functions come after outer ones that start earlier
        self.spans.sort(key=lambda s: (s.start, -s.end))
        self._starts = [s.start for s in self.spans]

    def _walk(self, node: ast.AST, prefix: list[str]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + [child.name]
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                self.spans.append(Span(".".join(name), start, child.end_lineno or child.lineno))
                self._walk(child, name)
            elif isinstance(child, ast.ClassDef):
                self._walk(child, prefix + [child.name])
            else:
                self._walk(child, prefix)

    def at(self, line: int) -> str:
        # innermost span containing line: scan candidates that start at or before it
        i = bisect_right(self._starts, line)
        best: Span | None = None
        for s in reversed(self.spans[:i]):
            if s.start <= line <= s.end and (best is None or s.end - s.start < best.end - best.start):
                best = s
        return best.qualname if best else MODULE

    def names(self) -> set[str]:
        return {s.qualname for s in self.spans}
