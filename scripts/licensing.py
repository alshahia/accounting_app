"""Dependency licence collection shared by the gate and the SBOM generator.

Phase 0 (day 10). The allow-list and the exception policy live in
analysis/24-engineering-guardrails.md §7 and
docs/09-decisions/0002-phase-0-licence-exceptions.md. This module turns the
installed Python environment and the pnpm dependency tree into one list of
components with a canonical licence expression.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
DECISION_EXCEPTIONS = ROOT / "docs" / "09-decisions" / "0002-phase-0-licence-exceptions.md"

ALLOWED = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "0BSD",
        "Unlicense",
        "CC0-1.0",
        "PostgreSQL",
        "Zlib",
    }
)

# Case-by-case licences: allowed only while the decision record exists.
EXCEPTIONS = {
    "MPL-2.0": DECISION_EXCEPTIONS,
    "LGPL-3.0": DECISION_EXCEPTIONS,
    "PSF-2.0": DECISION_EXCEPTIONS,
}

# Metadata variants that describe the same permissive family; kept small and
# explicit so the gate never silently widens the allow-list.
ALIASES = {
    "mit license": "MIT",
    "mit-0": "MIT",
    "mit-cmu": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "simplified bsd": "BSD-2-Clause",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "mpl-2": "MPL-2.0",
    "cc0": "CC0-1.0",
    "psfl": "PSF-2.0",
    "psf license": "PSF-2.0",
    "public domain": "Unlicense",
    "zlib license": "Zlib",
    "python software foundation license": "PSF-2.0",
}

# Distribution metadata gaps, pinned by version and verified by hand against the
# installed licence files (or the upstream metadata where the wheel omits them).
PACKAGE_OVERRIDES: dict[str, str] = {
    "python-bidi": "LGPL-3.0",  # dist-info COPYING.LESSER is LGPL-3.0
    "certifi": "MPL-2.0",  # License field says MPL-2.0
    "pillow": "MIT",  # License-Expression MIT-CMU, a permissive MIT variant
    "reportlab": "BSD-3-Clause",  # "BSD license (see license.txt ...)"
    "colorama": "BSD-3-Clause",
    "nodeenv": "BSD-3-Clause",
    "pyright": "MIT",
}

EXPRESSION_SPLIT = re.compile(r"\s+(?:AND|OR|WITH)\s+|[()]")

# PEP 503 normalisation: names differing only in case, "-", "_" or "." are the
# same distribution ("boolean.py" is pinned as "boolean-py"), so parity checks
# and lockfile lookups must compare this form, never the display name.
PEP503_SEPARATORS = re.compile(r"[-_.]+")


def normalized_name(name: str) -> str:
    """The PEP 503 form of a distribution name."""
    return PEP503_SEPARATORS.sub("-", name).lower()


@dataclass(frozen=True)
class Component:
    """One dependency with its canonical licence expression."""

    name: str
    version: str
    scope: str
    expression: str
    source: str


def canonical_token(raw: str) -> str:
    """Map one licence token or a free-form licence string to an SPDX-ish id."""
    token = raw.strip().strip("()").strip()
    if token == "":
        return ""
    if token in ALLOWED or token in EXCEPTIONS:
        return token
    key = token.lower()
    if key in ALIASES:
        return ALIASES[key]
    if "mozilla public license" in key or key in {"mpl2", "mpl-2"}:
        return "MPL-2.0"
    if "lesser general public license" in key or "lgpl" in key:
        if "2.1" in key:
            return "LGPL-2.1"
        return "LGPL-3.0"
    if "python software foundation" in key or key == "psf":
        return "PSF-2.0"
    if "bsd" in key:
        return "BSD-2-Clause" if "2" in key and "3" not in key else "BSD-3-Clause"
    if "apache" in key:
        return "Apache-2.0"
    if "mit" in key:
        return "MIT"
    if key == "isc":
        return "ISC"
    return token


def canonical_expression(raw: str | None) -> str:
    """Normalise a licence expression, preserving AND/OR so each token is checked."""
    if raw is None:
        return ""
    text = raw.strip()
    if text == "":
        return ""
    # A licence expression is short; a full licence *text* in the License field
    # (pip-api ships the whole Apache text there) must go through the fuzzy
    # token mapper, never through the AND/OR splitter.
    if "\n" not in text and len(text) <= 120 and EXPRESSION_SPLIT.search(text):
        parts = [part.strip() for part in EXPRESSION_SPLIT.split(text) if part.strip()]
        return " AND ".join(canonical_token(part) for part in parts)
    return canonical_token(text)


def expression_tokens(expression: str) -> list[str]:
    """Return every licence token inside an expression."""
    return [token for token in expression.split(" AND ") if token]


def _python_expression(distribution: metadata.Distribution) -> str:
    name = (distribution.metadata.get("Name") or "").lower()
    if name in PACKAGE_OVERRIDES:
        return PACKAGE_OVERRIDES[name]
    expression = distribution.metadata.get("License-Expression")
    if expression:
        return canonical_expression(expression)
    license_field = distribution.metadata.get("License")
    if license_field and license_field.strip():
        return canonical_expression(license_field)
    classifiers = distribution.metadata.get_all("Classifier") or []
    for classifier in classifiers:
        if classifier.startswith("License ::"):
            return canonical_expression(classifier.rsplit("::", 1)[-1].strip())
    return ""


def python_components() -> list[Component]:
    """Every installed Python distribution, sorted by name."""
    components: list[Component] = []
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        components.append(
            Component(
                name=name,
                version=distribution.version or "0",
                scope="python",
                expression=_python_expression(distribution),
                source="importlib.metadata",
            )
        )
    return sorted(components, key=lambda component: component.name.lower())


def locked_python_names() -> set[str]:
    """Names pinned in uv.lock, used to spot a package the environment misses."""
    lock_path = ROOT / "uv.lock"
    if not lock_path.exists():
        return set()
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    return {
        normalized_name(str(package.get("name", "")))
        for package in data.get("package", [])
        if package.get("name")
    }


def _pnpm_command() -> list[str]:
    executable = shutil.which("pnpm")
    if executable is None:
        raise RuntimeError("pnpm is not on PATH; install Node.js and pnpm first")
    if executable.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", executable]
    return [executable]


def node_components() -> list[Component]:
    """Every pnpm package with its licence, from `pnpm licenses list --json`."""
    result = subprocess.run(
        [*_pnpm_command(), "licenses", "list", "--json"],
        cwd=WEB_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pnpm licenses failed:\n{result.stderr.strip()}")
    data = json.loads(result.stdout or "{}")
    components: list[Component] = []
    for licence, packages in data.items():
        for package in packages:
            for version in package.get("versions", []):
                components.append(
                    Component(
                        name=str(package.get("name", "")),
                        version=str(version),
                        scope="node",
                        expression=canonical_expression(licence),
                        source="pnpm licenses",
                    )
                )
    return sorted(components, key=lambda component: component.name.lower())


def all_components() -> tuple[list[Component], list[str], list[str]]:
    """Return components, used exceptions and problems (missing licence)."""
    components = python_components() + node_components()
    exceptions_used: list[str] = []
    problems: list[str] = []
    for component in components:
        tokens = expression_tokens(component.expression)
        if not tokens:
            problems.append(f"{component.scope}:{component.name} has no licence metadata")
            continue
        for token in tokens:
            if token in ALLOWED:
                continue
            if token in EXCEPTIONS and EXCEPTIONS[token].exists():
                label = f"{token} ({component.name})"
                if label not in exceptions_used:
                    exceptions_used.append(label)
                continue
            problems.append(
                f"{component.scope}:{component.name}@{component.version} uses {token!r}"
            )
    return components, sorted(exceptions_used), problems


def locked_node_pairs() -> set[tuple[str, str]]:
    """(name, version) pairs pinned in the committed web/pnpm-lock.yaml."""
    lock_path = WEB_ROOT / "pnpm-lock.yaml"
    if not lock_path.exists():
        return set()
    pairs: set[tuple[str, str]] = set()
    in_packages = False
    for raw in lock_path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("packages:"):
            in_packages = True
            continue
        if not in_packages:
            continue
        if raw and not raw.startswith(" "):
            break
        match = re.match(r"^  '?([^']+?)'?:$", raw)
        if match is None:
            continue
        name, _, version = match.group(1).rpartition("@")
        if name and version:
            pairs.add((name, version))
    return pairs


def committed_tree_problems(components: list[Component]) -> list[str]:
    """Problems proving the scanned environment is the committed dependency set.

    The gate reads licence metadata from the environment that
    `uv sync --locked` and `pnpm install --frozen-lockfile` produced. Without
    this check the result is a property of this machine: an extra package
    installed by hand is scanned, a package deleted from the environment is
    silently skipped, and CI cannot reproduce either. Every scanned component
    must therefore be pinned in the committed lockfile, or the gate fails.
    """
    problems: list[str] = []
    locked_python = locked_python_names()
    locked_node = locked_node_pairs()
    for component in components:
        if component.scope == "python":
            key = normalized_name(component.name)
            if locked_python and key not in locked_python:
                problems.append(
                    f"python:{component.name} is installed but not pinned in uv.lock "
                    "(run uv sync --locked)"
                )
        elif locked_node and (component.name, component.version) not in locked_node:
            problems.append(
                f"node:{component.name}@{component.version} is installed but not pinned in "
                "web/pnpm-lock.yaml (run pnpm install --frozen-lockfile)"
            )
    return problems


def missing_from_environment() -> list[str]:
    """Packages pinned in uv.lock but absent here (other platforms' extras)."""
    installed = {normalized_name(component.name) for component in python_components()}
    return sorted(locked_python_names() - installed)
