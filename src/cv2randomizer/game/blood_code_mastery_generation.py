"""Set the mastery requirement on each installed Blood Code Article."""

from __future__ import annotations

import json
import math
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command
from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    IoStoreAssetExtractor,
    materialize_extracted_assets,
)
from cv2randomizer.game.blood_code_audit import installed_blood_code_articles
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION


BLOOD_CODE_ARTICLE_ROOT = "/Game/Blueprint/Article/BloodCode/"
LOW_BLOOD_CODE_MASTERY_APPROPRIATE_LEVEL = 1


class BloodCodeMasteryError(RuntimeError):
    """Raised when Blood Code mastery data cannot be patched safely."""


# Validate patch response
def _validate_patch_response(
    response: Any,
    expected_packages: list[str],
    expected_proficiency: float | None,
    target_appropriate_level: int,
) -> tuple[float, float]:
    """Validate helper output by package identity, independent of sort implementation."""
    if not isinstance(response, dict):
        raise BloodCodeMasteryError("UAssetAPI Blood Code mastery helper returned an invalid result object.")
    reported_packages = response.get("patchedPackages")
    expected_set = {package.casefold() for package in expected_packages}
    reported_set = (
        {package.casefold() for package in reported_packages if isinstance(package, str)}
        if isinstance(reported_packages, list)
        else set()
    )
    validation_errors: list[str] = []
    if (
        not isinstance(reported_packages, list)
        or len(reported_packages) != len(expected_packages)
        or reported_set != expected_set
    ):
        missing = sorted(expected_set - reported_set)
        unexpected = sorted(reported_set - expected_set)
        validation_errors.append(
            f"package inventory mismatch (reported={len(reported_packages) if isinstance(reported_packages, list) else 'invalid'}, "
            f"expected={len(expected_packages)}, missing={missing[:2]}, unexpected={unexpected[:2]})"
        )
    if response.get("patchedCount") != len(expected_packages):
        validation_errors.append(f"patched count {response.get('patchedCount')!r} != {len(expected_packages)}")
    target_proficiency = response.get("targetProficiency")
    lou01_original_proficiency = response.get("lou01OriginalProficiency")
    valid_target = (
        isinstance(target_proficiency, (int, float))
        and not isinstance(target_proficiency, bool)
        and math.isfinite(target_proficiency)
        and target_proficiency > 0
    )
    valid_lou01 = (
        isinstance(lou01_original_proficiency, (int, float))
        and not isinstance(lou01_original_proficiency, bool)
        and math.isfinite(lou01_original_proficiency)
        and lou01_original_proficiency > 0
    )
    if not valid_target:
        validation_errors.append(f"invalid target proficiency {target_proficiency!r}")
    if not valid_lou01:
        validation_errors.append(f"invalid Lou 1 source proficiency {lou01_original_proficiency!r}")
    if valid_target and expected_proficiency is not None and target_proficiency != expected_proficiency:
        validation_errors.append(
            f"target proficiency {target_proficiency!r} != requested {expected_proficiency!r}"
        )
    if valid_target and valid_lou01 and expected_proficiency is None and target_proficiency != lou01_original_proficiency:
        validation_errors.append(
            f"target proficiency {target_proficiency!r} does not match Lou 1 source {lou01_original_proficiency!r}"
        )
    if response.get("targetAppropriateLevel") != target_appropriate_level:
        validation_errors.append(
            f"target appropriate level {response.get('targetAppropriateLevel')!r} != {target_appropriate_level!r}"
        )
    if response.get("reopenedAndValidated") is not True:
        validation_errors.append("helper did not confirm reopen validation")
    if validation_errors:
        raise BloodCodeMasteryError(
            "UAssetAPI did not validate every requested Blood Code Article: "
            + "; ".join(validation_errors)
            + "."
        )
    return float(target_proficiency), float(lou01_original_proficiency)


@dataclass(frozen=True, slots=True)
class BloodCodeMasteryResult:
    output_files: tuple[Path, ...]
    patched_packages: tuple[str, ...]
    target_proficiency: float
    lou01_original_proficiency: float
    target_appropriate_level: int
    reopened: bool


class BloodCodeMasterySerializer:
    """Patch and reopen Blood Code Article DataAssets through UAssetAPI."""

    # Initialize this object
    def __init__(
        self,
        *,
        helper_project: Path | None = None,
        dotnet_executable: str = "dotnet",
        engine_version: str = SUPPORTED_ENGINE_VERSION,
        timeout_seconds: float = 1800,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
    ) -> None:
        self.helper_project = (helper_project or resolve_uassetapi_helper()).resolve()
        self.dotnet_executable = dotnet_executable
        self.engine_version = engine_version
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        if engine_version != SUPPORTED_ENGINE_VERSION:
            raise ValueError(f"Blood Code mastery serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Patch the prepared asset package
    def patch(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        packages: tuple[tuple[str, Path, Path], ...],
        target_proficiency: float | None = None,
        target_appropriate_level: int = LOW_BLOOD_CODE_MASTERY_APPROPRIATE_LEVEL,
    ) -> BloodCodeMasteryResult:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise BloodCodeMasteryError("Blood Code mastery helper, workspace, or CV2.usmap is missing.")
        if target_proficiency is not None and (
            not math.isfinite(target_proficiency) or target_proficiency <= 0
        ):
            raise BloodCodeMasteryError("Blood Code mastery EXP must be a positive finite number.")
        if (
            isinstance(target_appropriate_level, bool)
            or not isinstance(target_appropriate_level, int)
            or target_appropriate_level <= 0
        ):
            raise BloodCodeMasteryError("Blood Code mastery appropriate level must be a positive integer.")
        validated = _validate_packages(root, packages)
        document = {
            "targetProficiency": target_proficiency,
            "targetAppropriateLevel": target_appropriate_level,
            "packages": [
                {"virtualPackage": virtual, "inputFile": str(source), "outputFile": str(output)}
                for virtual, source, output in validated
            ],
        }
        manifest = root / f".cv2-blood-code-mastery-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project,
            self.dotnet_executable,
            "patch-blood-code-mastery",
            str(root),
            str(mappings),
            self.engine_version,
            str(manifest),
        )
        try:
            try:
                completed = self.runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    cwd=root,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise BloodCodeMasteryError(f"Could not run UAssetAPI Blood Code mastery helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise BloodCodeMasteryError(f"UAssetAPI Blood Code mastery patch failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise BloodCodeMasteryError("UAssetAPI Blood Code mastery helper returned no result.")
            try:
                response: Any = json.loads(lines[-1])
            except json.JSONDecodeError as error:
                raise BloodCodeMasteryError("UAssetAPI Blood Code mastery helper returned invalid JSON.") from error
            expected_packages = [virtual for virtual, _source, _output in validated]
            target_proficiency, lou01_original_proficiency = _validate_patch_response(
                response, expected_packages, target_proficiency, target_appropriate_level
            )
            output_files = tuple(
                Path(value).resolve(strict=True)
                for value in response.get("outputFiles", ())
                if isinstance(value, str)
            )
            expected_files = {
                path.resolve()
                for _virtual, _source, output in validated
                for path in _package_sidecars(output)
                if path.is_file()
            }
            if not output_files or {str(path).casefold() for path in output_files} != {
                str(path).casefold() for path in expected_files
            }:
                raise BloodCodeMasteryError("UAssetAPI returned an unexpected Blood Code mastery sidecar inventory.")
            if any(not _is_within(path, root) or path.stat().st_size <= 0 for path in output_files):
                raise BloodCodeMasteryError("Blood Code mastery output is empty or escaped the generation workspace.")
            return BloodCodeMasteryResult(
                output_files, tuple(expected_packages), target_proficiency, lou01_original_proficiency,
                target_appropriate_level, True
            )
        finally:
            manifest.unlink(missing_ok=True)


# List blood code mastery packages
def blood_code_mastery_packages(inventory: PackageInventory) -> tuple[str, ...]:
    """Return installed player Blood Code Articles, excluding debug assets."""
    return tuple(
        package for package in installed_blood_code_articles(inventory)
        if "/Debug/" not in package
    )


# Apply blood code mastery
def apply_blood_code_mastery(
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    package_inventory: PackageInventory,
    serializer: BloodCodeMasterySerializer,
    progress: Callable[[str, int, int], None] | None = None,
) -> BloodCodeMasteryResult:
    """Patch all installed, non-debug Blood Code Articles with one low EXP value."""
    packages = blood_code_mastery_packages(package_inventory)
    if not packages:
        raise BloodCodeMasteryError("No installed Blood Code Article packages were found.")
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    try:
        if progress:
            progress("extracting-blood-code-mastery", 0, len(packages))
        extracted = extractor.extract_assets(
            game_root,
            packages,
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        by_virtual = {asset.virtual_path.casefold(): asset for asset in extracted}
        if set(by_virtual) != {package.casefold() for package in packages}:
            raise BloodCodeMasteryError("Extraction returned a missing or unexpected Blood Code Article package.")
        materialized = materialize_extracted_assets(
            extracted,
            root / "source" / "blood-code-mastery" / "Content",
        )
        materialized_by_virtual = {asset.virtual_path.casefold(): asset for asset in materialized}
        patch_requests: list[tuple[str, Path, Path]] = []
        for package in packages:
            asset = materialized_by_virtual.get(package.casefold())
            if asset is None:
                raise BloodCodeMasteryError(f"Blood Code Article source was not materialized: {package}.")
            relative = Path(*package.removeprefix("/Game/").split("/"))
            output = output_root / relative.with_suffix(".uasset")
            if output.exists() or any(path.exists() for path in _package_sidecars(output)):
                raise BloodCodeMasteryError(f"Refusing to overwrite an existing staged Blood Code Article: {package}.")
            patch_requests.append((package, asset.primary_file, output))
        if progress:
            progress("patching-blood-code-mastery", 0, len(patch_requests))
        try:
            result = serializer.patch(
                work_root=root,
                mappings_path=mappings_path,
                packages=tuple(patch_requests),
                target_appropriate_level=LOW_BLOOD_CODE_MASTERY_APPROPRIATE_LEVEL,
            )
        except Exception:
            for _package, _source, output in patch_requests:
                for path in _package_sidecars(output):
                    path.unlink(missing_ok=True)
            raise
        if progress:
            progress("patching-blood-code-mastery", len(patch_requests), len(patch_requests))
        return result
    except (AssetExtractionError, OSError, ValueError) as error:
        if isinstance(error, BloodCodeMasteryError):
            raise
        raise BloodCodeMasteryError(f"Could not prepare Blood Code mastery Article packages: {error}") from error


# Read inventory for blood code mastery
def inventory_for_blood_code_mastery(
    *,
    game_root: Path,
    cached_inventory: PackageInventory,
    prepared_packages: tuple[str, ...],
    installed_extractor: IoStoreAssetExtractor,
) -> PackageInventory:
    """Use READY articles when complete; otherwise inspect the installed package list."""
    if prepared_packages and all(cached_inventory.contains(package) for package in prepared_packages):
        return cached_inventory
    return installed_extractor.inspect_package_inventory(game_root)


# Validate packages
def _validate_packages(root: Path, packages: tuple[tuple[str, Path, Path], ...]) -> tuple[tuple[str, Path, Path], ...]:
    if not isinstance(packages, tuple) or not packages or len(packages) > 100:
        raise BloodCodeMasteryError("Blood Code mastery patch must contain 1 to 100 installed packages.")
    ordered = tuple(sorted(packages, key=lambda item: item[0].casefold()))
    names = [item[0].casefold() for item in ordered]
    if len(names) != len(set(names)):
        raise BloodCodeMasteryError("Blood Code mastery package names must be unique.")
    for virtual, source, output in ordered:
        if not virtual.startswith(BLOOD_CODE_ARTICLE_ROOT) or "/Debug/" in virtual:
            raise BloodCodeMasteryError(f"Unsupported Blood Code mastery package: {virtual}.")
        source = source.expanduser().resolve(strict=True)
        output = output.expanduser().resolve()
        if not source.is_file() or not _is_within(source, root) or not _is_within(output, root) or source == output:
            raise BloodCodeMasteryError("Blood Code mastery input and output must be separate files inside the workspace.")
        if source.suffix.casefold() != ".uasset" or output.suffix.casefold() != ".uasset":
            raise BloodCodeMasteryError("Blood Code mastery package headers must use the .uasset extension.")
    return tuple((virtual, source.expanduser().resolve(strict=True), output.expanduser().resolve())
                 for virtual, source, output in ordered)


# Build package sidecars
def _package_sidecars(header: Path) -> tuple[Path, ...]:
    return (header, *(header.with_suffix(extension) for extension in (".uexp", ".ubulk", ".uptnl")))


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return path.resolve() != root.resolve()
    except (OSError, ValueError):
        return False
