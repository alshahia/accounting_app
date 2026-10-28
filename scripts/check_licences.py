"""Licence gate for the phase-0 dependency set (analysis/24 §7).

Run from the repository root:

    uv run python scripts/check_licences.py

Exit code 0 means every installed Python distribution and every pnpm package
maps to an allow-listed licence, or to one of the named exceptions in
docs/09-decisions/0002-phase-0-licence-exceptions.md. Anything else fails the
gate. `--quiet` prints only the summary.
"""

from __future__ import annotations

import argparse
import sys

from licensing import (
    ALLOWED,
    EXCEPTIONS,
    all_components,
    committed_tree_problems,
    missing_from_environment,
)

EXIT_OK = 0
EXIT_VIOLATION = 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Check dependency licences against the allow-list")
    parser.add_argument("--quiet", action="store_true", help="print only the summary")
    args = parser.parse_args()

    components, exceptions_used, problems = all_components()
    # The scan reads the installed environment; this proves that environment is
    # exactly the committed dependency set, so the result belongs to the tree.
    problems = problems + committed_tree_problems(components)
    python_count = sum(1 for component in components if component.scope == "python")
    node_count = sum(1 for component in components if component.scope == "node")

    if not args.quiet:
        print("Allowed licences:", ", ".join(sorted(ALLOWED)))
        print("Case-by-case licences:", ", ".join(sorted(EXCEPTIONS)))
        for component in components:
            print(
                f"  {component.scope:6} {component.name:32} {component.version:12} "
                f"{component.expression or '<missing>'}"
            )

    absent = missing_from_environment()
    if absent and not args.quiet:
        print(
            "Note: pinned for other platforms and not installed here:",
            ", ".join(absent),
        )

    if exceptions_used:
        print("Exceptions used (decision docs/09-decisions/0002-phase-0-licence-exceptions.md):")
        for exception in exceptions_used:
            print(f"  - {exception}")

    if problems:
        print("LICENCE GATE FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_VIOLATION

    print(f"licence-ok: {python_count} Python + {node_count} Node packages, 0 violations")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
