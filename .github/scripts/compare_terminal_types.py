"""Compare complete native mypy diagnostics against the unchanged source base."""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from functools import cache
from pathlib import Path

Diagnostic = tuple[str, str, str, str]
ERROR = re.compile(r"^(.+):([0-9]+): error: (.+) \[([a-z-]+)\]$")
SUMMARY = re.compile(r"^Found ([0-9]+) errors? in ")


@cache
def definitions(path: Path) -> tuple[tuple[int, int, str], ...]:
    """Read enclosing definitions once from each immutable checkout file."""
    if not path.is_file():
        return ()
    result = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            result.append((node.lineno, node.end_lineno or node.lineno, node.name))
    return tuple(sorted(result))


def owner(path: Path, line: int) -> str:
    """Name the enclosing definitions so line shifts cannot hide new errors."""
    return (
        ".".join(name for start, end, name in definitions(path) if start <= line <= end)
        or "<module>"
    )


def diagnostics(report: Path, root: Path) -> Counter[Diagnostic]:
    """Read every error and reject incomplete or differently formatted output."""
    result: Counter[Diagnostic] = Counter()
    expected = None
    success = False
    for line in report.read_text(encoding="utf-8").splitlines():
        summary = SUMMARY.match(line)
        if summary:
            expected = int(summary[1])
        success |= line.startswith("Success: no issues found")
        if ": error:" not in line:
            continue
        match = ERROR.match(line)
        if match is None:
            raise ValueError(f"Unrecognized diagnostic: {line}")
        file, number, message, code = match.groups()
        result[(file, owner(root / file, int(number)), message, code)] += 1
    if expected is None and success:
        expected = 0
    if expected is None or sum(result.values()) != expected:
        raise ValueError(f"Incomplete mypy report: {report}")
    return result


def main() -> None:
    baseline_log, candidate_log, baseline_root, candidate_root = map(Path, sys.argv[1:])
    baseline = diagnostics(baseline_log, baseline_root)
    candidate = diagnostics(candidate_log, candidate_root)
    introduced = candidate - baseline
    print(
        json.dumps(
            {
                "baseline_errors": sum(baseline.values()),
                "candidate_errors": sum(candidate.values()),
                "introduced": [
                    {
                        "file": key[0],
                        "owner": key[1],
                        "message": key[2],
                        "code": key[3],
                        "count": count,
                    }
                    for key, count in sorted(introduced.items())
                ],
            },
            indent=2,
        )
    )
    raise SystemExit(1 if introduced else 0)


if __name__ == "__main__":
    main()
