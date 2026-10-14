"""Run the fast, repository-only production-readiness checks.

Use ``python tools/final_audit.py --full`` for the complete fixture suite.
Real-game Extract, packaged Generate, and gameplay checks remain manual release gates.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_USMAP_SHA256 = "af0ae89ff49d274e2c0e3a350ed09f67c1765183908e134029611480be316984"
FAST_TESTS = (
    "tests/test_core.py",
    "tests/test_determinism.py",
    "tests/test_game_data_cache.py",
    "tests/test_content_availability.py",
    "tests/test_content_classification.py",
    "tests/test_configuration_matrix.py",
    "tests/test_important_items.py",
    "tests/test_world_loot_randomizer.py",
    "tests/test_world_loot_generation.py",
    "tests/test_material_randomizer.py",
    "tests/test_material_generation.py",
    "tests/test_shop_randomizer.py",
    "tests/test_shop_generation.py",
    "tests/test_enemy_drop_randomizer.py",
    "tests/test_enemy_drop_generation.py",
    "tests/test_blood_code_generation.py",
    "tests/test_booster_generation.py",
    "tests/test_extractor_randomizer.py",
    "tests/test_initial_inventory.py",
    "tests/test_jail_catalog_randomizer.py",
    "tests/test_defensive_gadgets.py",
    "tests/test_bequeathed_formae.py",
    "tests/test_bequeathed_formae_generation.py",
    "tests/test_installer.py",
    "tests/test_packaging.py",
    "tests/test_resources.py",
    "tests/test_subprocesses.py",
    "tests/test_spoiler.py",
    "tests/test_world_loot_binary_patch.py",
)
_LOCAL_PATH = re.compile(r"(?i)C:\\(?:Hiroshi|Users)\\")


# Run the external helper command
def _run(label: str, command: list[str]) -> bool:
    print(f"\n[{label}] {subprocess.list2cmdline(command)}", flush=True)
    try:
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    except OSError as error:
        print(f"Could not start {label}: {error}", file=sys.stderr, flush=True)
        return False
    if result.returncode:
        print(f"{label} failed with exit code {result.returncode}.", file=sys.stderr, flush=True)
        return False
    return True


# Read static checks
def _static_checks() -> bool:
    errors: list[str] = []
    mapping = PROJECT_ROOT / "workspace" / "research_assets" / "CV2.usmap"
    if not mapping.is_file():
        errors.append("Canonical workspace/research_assets/CV2.usmap is missing.")
    elif hashlib.sha256(mapping.read_bytes()).hexdigest() != CANONICAL_USMAP_SHA256:
        errors.append("Canonical CV2.usmap SHA-256 does not match the pinned project resource.")

    source_files = [path for path in (PROJECT_ROOT / "src").rglob("*.py")]
    source_files.extend((PROJECT_ROOT / "tools" / "uassetapi-worldloot").rglob("*.cs"))
    source_files.extend((PROJECT_ROOT / "scripts").glob("*.ps1"))
    for path in source_files:
        if any(part.casefold() in {"bin", "obj", "__pycache__"} for part in path.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            errors.append(f"Could not read production source {path.relative_to(PROJECT_ROOT)}: {error}")
            continue
        if _LOCAL_PATH.search(content):
            errors.append(f"Machine-specific Windows path in {path.relative_to(PROJECT_ROOT)}.")
        if path.suffix == ".py":
            try:
                tree = ast.parse(content, filename=str(path))
            except SyntaxError as error:
                errors.append(f"Python syntax error in {path.relative_to(PROJECT_ROOT)}:{error.lineno}.")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute) and node.func.attr == "system":
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                        errors.append(f"os.system() call in {path.relative_to(PROJECT_ROOT)}:{node.lineno}.")
                if any(
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                ):
                    errors.append(f"shell=True in {path.relative_to(PROJECT_ROOT)}:{node.lineno}.")

    if errors:
        print("\n[static safety checks] FAILED", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return False
    print("\n[static safety checks] passed: pinned mapping, no developer-local source paths, no shell=True/os.system")
    return True


# Start the application
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="run every pytest test instead of the critical fixture subset",
    )
    parser.add_argument(
        "--skip-dotnet",
        action="store_true",
        help="skip the UAssetAPI helper Release build (not suitable for release validation)",
    )
    args = parser.parse_args()

    if not _static_checks():
        return 1
    if not _run("Python compileall", [sys.executable, "-m", "compileall", "-q", "src", "tests", "tools"]):
        return 1

    tests = ["-q"] if args.full else ["-q", *FAST_TESTS]
    if not _run("Python tests", [sys.executable, "-m", "pytest", *tests]):
        return 1

    if not args.skip_dotnet:
        dotnet = shutil.which("dotnet")
        if dotnet is None:
            print("The .NET SDK is required; pass --skip-dotnet only for Python-only checks.", file=sys.stderr)
            return 1
        helper_project = "tools/uassetapi-worldloot/UAssetAPIWorldLoot.csproj"
        if not _run("UAssetAPI helper Release build", [dotnet, "build", helper_project, "--configuration", "Release"]):
            return 1

    print("\nRepository audit checks passed. Real-game and gameplay checks are listed in docs/release_checklist.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
