"""Mechanical checks that keep the tutoring core predictable."""

import ast
import re
import sys
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).parents[1] / "src" / "mali"
REPOSITORY_ROOT = CORE_ROOT.parents[2]
PROMPT_ROOT = REPOSITORY_ROOT / "app" / "src" / "mali_app" / "prompts"
UI_ROOT = REPOSITORY_ROOT / "app" / "src" / "mali_app"
BANNED_CORE_MODULES = frozenset(
    {
        "asyncio",
        "concurrent",
        "http",
        "io",
        "json",
        "logging",
        "os",
        "pathlib",
        "random",
        "secrets",
        "socket",
        "subprocess",
        "time",
        "urllib",
        "uuid",
    }
)
FORBIDDEN_REPOSITORY_TERMS = (
    "antimatroid",
    "bayesian",
    "doignon",
    "falmagne",
    "half-split",
    "knowledge space",
    "knowledge state",
    "learning space",
    "posterior",
    "quasi-ordinal",
    "surmise",
    "well-graded",
)
FORBIDDEN_PRODUCT_TERMS = (
    "agent",
    "item",
    "guard",
    "transition",
    "structure",
    "knowledge state",
    "fringe",
    "estimate",
    "posterior",
)


def _core_files() -> tuple[Path, ...]:
    return tuple(sorted(CORE_ROOT.glob("*.py")))


def test_core_imports_only_standard_library_modules() -> None:
    standard_library = sys.stdlib_module_names | {"mali"}
    violations: list[str] = []
    for source_file in _core_files():
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported_names: tuple[str, ...]
            if isinstance(node, ast.Import):
                imported_names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_names = (node.module,)
            else:
                continue
            for imported_name in imported_names:
                if imported_name.split(".", maxsplit=1)[0] not in standard_library:
                    violations.append(f"{source_file.name}: {imported_name}")
    assert not violations, "non-standard imports: " + ", ".join(violations)


def test_core_does_not_import_boundary_modules() -> None:
    violations: list[str] = []
    for source_file in _core_files():
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = (node.module,)
            else:
                continue
            for name in names:
                if name.split(".", maxsplit=1)[0] in BANNED_CORE_MODULES:
                    violations.append(f"{source_file.name}: {name}")
    assert not violations, "boundary imports: " + ", ".join(violations)


def _is_third_party_path(path: Path) -> bool:
    # ignore virtualenvs, site-packages and common build dirs inside the repo
    forbidden = {"site-packages", ".venv", "venv", "env", ".env", ".tox", ".eggs"}
    return any(
        part in forbidden or part.startswith(".") and part != "." for part in path.parts
    )


def test_repository_does_not_suppress_type_errors() -> None:
    suppression = "type" + ": ignore"
    matches = [
        path
        for path in REPOSITORY_ROOT.rglob("*.py")
        if suppression in path.read_text(encoding="utf-8")
        and not _is_third_party_path(path)
    ]
    assert not matches, "type suppressions: " + ", ".join(map(str, matches))


@pytest.mark.parametrize("term", FORBIDDEN_REPOSITORY_TERMS)
def test_public_text_uses_product_vocabulary(term: str) -> None:
    public_files = (REPOSITORY_ROOT / "README.md", *PROMPT_ROOT.glob("*.md"))
    violations = [
        path
        for path in public_files
        if term in path.read_text(encoding="utf-8").lower()
    ]
    assert not violations, f"reserved term {term!r} in: {violations}"


def _ui_copy_sources() -> tuple[tuple[Path, str], ...]:
    assets = tuple(
        (path, path.read_text(encoding="utf-8"))
        for directory in (UI_ROOT / "templates", UI_ROOT / "static")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    )
    source = UI_ROOT / "web.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    literals = "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    return (*assets, (source, literals))


@pytest.mark.parametrize("term", FORBIDDEN_PRODUCT_TERMS)
def test_student_and_teacher_copy_uses_only_learning_vocabulary(term: str) -> None:
    violations = [
        path
        for path, text in _ui_copy_sources()
        if re.search(rf"\b{re.escape(term)}\b", text, flags=re.IGNORECASE)
    ]
    assert not violations, f"product term {term!r} in: {violations}"
