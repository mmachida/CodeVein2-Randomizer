"""Extract, inspect, plan, and structurally patch confirmed gadget grants."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command

from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    IoStoreAssetExtractor,
    MaterializedAsset,
    materialize_extracted_assets,
)
from cv2randomizer.game.defensive_gadget_catalog import (
    DefensiveGadgetCatalog,
    DefensiveGadgetGrantSlot,
)
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION
from cv2randomizer.randomizer.defensive_gadgets import (
    DefensiveGadgetNpcPlan,
    DefensiveGadgetPlanError,
    build_defensive_gadget_npc_plan,
)
from cv2randomizer.randomizer.source_occurrences import RewardSourceOccurrence


class DefensiveGadgetGenerationError(RuntimeError):
    """Raised when a gadget grant source or patched package fails validation."""


@dataclass(frozen=True, slots=True)
class DefensiveGadgetGrantSnapshot:
    package: str
    export_index: int
    export_name: str
    bulk_index: int
    reward_index: int
    reward_package: str
    reward_asset: str
    count: int
    notification: bool


@dataclass(frozen=True, slots=True)
class DefensiveGadgetPreparation:
    plan: DefensiveGadgetNpcPlan
    catalog: DefensiveGadgetCatalog
    source_assets: tuple[MaterializedAsset, ...]
    snapshots: tuple[DefensiveGadgetGrantSnapshot, ...]


@dataclass(frozen=True, slots=True)
class DefensiveGadgetGenerationResult:
    plan: DefensiveGadgetNpcPlan
    output_files: tuple[Path, ...]
    patched_packages: tuple[str, ...]
    reopened: bool


class DefensiveGadgetSerializer:
    """Call the existing UAssetAPI helper for exact grant inspection and patching."""

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
            raise ValueError(f"Defensive Gadget serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Inspect extracted packages
    def inspect_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        package_files: Mapping[str, Path],
    ) -> tuple[DefensiveGadgetGrantSnapshot, ...]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        requests = []
        for package, raw_path in sorted(package_files.items(), key=lambda item: item[0].casefold()):
            path = raw_path.resolve(strict=True)
            if not _is_within(path, root) or not path.is_file():
                raise DefensiveGadgetGenerationError("Grant sources must be inside the generation workspace.")
            requests.append({"virtualPackage": package, "inputFile": str(path)})
        result = self._run_json(root, mappings, "inspect-defensive-gadget-grants", {"packages": requests})
        if not isinstance(result, list) or len(result) != len(requests):
            raise DefensiveGadgetGenerationError("UAssetAPI returned the wrong grant-package inspection count.")
        snapshots: list[DefensiveGadgetGrantSnapshot] = []
        for expected, output in zip(requests, result, strict=True):
            if not isinstance(output, dict) or output.get("virtualPackage") != expected["virtualPackage"]:
                raise DefensiveGadgetGenerationError("UAssetAPI inspected an unexpected gadget grant package.")
            if Path(str(output.get("inputFile", ""))).resolve() != Path(expected["inputFile"]):
                raise DefensiveGadgetGenerationError("UAssetAPI inspected an unexpected source file.")
            raw_grants = output.get("grants")
            if not isinstance(raw_grants, list) or output.get("grantCount") != len(raw_grants):
                raise DefensiveGadgetGenerationError("UAssetAPI omitted or miscounted inspected gadget grants.")
            for raw in raw_grants:
                snapshots.append(_parse_snapshot(raw, expected["virtualPackage"]))
        return tuple(snapshots)

    # Patch package reward data
    def patch_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        packages: Mapping[str, tuple[Path, Path, tuple[dict[str, Any], ...]]],
    ) -> list[dict[str, Any]]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        requests = []
        for package, (source, output, assignments) in sorted(packages.items(), key=lambda item: item[0].casefold()):
            source_path = source.resolve(strict=True)
            output_path = output.resolve()
            if not _is_within(source_path, root) or not _is_within(output_path, root) or source_path == output_path:
                raise DefensiveGadgetGenerationError("Gadget patch inputs and outputs must be separate workspace paths.")
            if output_path.exists() or any(path.exists() for path in _package_files(output_path)):
                raise DefensiveGadgetGenerationError(f"Refusing to overwrite gadget output {output_path.name}.")
            requests.append({"virtualPackage": package, "inputFile": str(source_path), "outputFile": str(output_path), "assignments": list(assignments)})
        result = self._run_json(root, mappings, "patch-defensive-gadget-grants", {"packages": requests})
        if not isinstance(result, list) or len(result) != len(requests):
            raise DefensiveGadgetGenerationError("UAssetAPI returned the wrong gadget patch package count.")
        for expected, output in zip(requests, result, strict=True):
            if not isinstance(output, dict) or output.get("virtualPackage") != expected["virtualPackage"]:
                raise DefensiveGadgetGenerationError("UAssetAPI patched an unexpected gadget grant package.")
            if Path(str(output.get("inputFile", ""))).resolve() != Path(expected["inputFile"]):
                raise DefensiveGadgetGenerationError("UAssetAPI patched an unexpected source package.")
            if Path(str(output.get("outputFile", ""))).resolve() != Path(expected["outputFile"]):
                raise DefensiveGadgetGenerationError("UAssetAPI wrote an unexpected staged gadget package.")
            if output.get("patchedSlots") != len(expected["assignments"]) or output.get("reopenedAndValidated") is not True:
                raise DefensiveGadgetGenerationError("UAssetAPI did not confirm reopened gadget grant validation.")
            files = output.get("outputFiles")
            if not isinstance(files, list) or not files:
                raise DefensiveGadgetGenerationError("UAssetAPI omitted staged package sidecars.")
            resolved = [Path(str(path)).resolve(strict=True) for path in files]
            header = Path(expected["outputFile"]).resolve()
            if header not in resolved or any(
                not _is_within(path, root) or path.stem.casefold() != header.stem.casefold()
                or path.suffix.casefold() not in {".uasset", ".uexp", ".ubulk", ".uptnl"}
                for path in resolved
            ):
                raise DefensiveGadgetGenerationError("UAssetAPI returned an invalid staged gadget sidecar inventory.")
            output["outputFiles"] = [str(path) for path in resolved]
        return result

    # Run helper and parse JSON
    def _run_json(self, root: Path, mappings: Path, operation: str, document: dict[str, Any]) -> Any:
        if not self.helper_project.is_file():
            raise DefensiveGadgetGenerationError(f"UAssetAPI helper project does not exist: {self.helper_project}.")
        manifest = root / f".cv2-defensive-gadget-{operation}-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        try:
            try:
                result = self.runner(
                    uassetapi_command(
                        self.helper_project, self.dotnet_executable, operation,
                        str(root), str(mappings), self.engine_version, str(manifest),
                    ),
                    check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=self.timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise DefensiveGadgetGenerationError(f"Could not run the UAssetAPI gadget helper: {error}.") from error
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-5000:]
                raise DefensiveGadgetGenerationError(f"UAssetAPI {operation} failed (exit {result.returncode}): {detail}")
            try:
                return json.loads(result.stdout)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise DefensiveGadgetGenerationError(f"UAssetAPI {operation} returned invalid JSON.") from error
        finally:
            manifest.unlink(missing_ok=True)

    # Check package paths stay contained
    @staticmethod
    def _validate_roots(work_root: Path, mappings_path: Path) -> tuple[Path, Path]:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file():
            raise DefensiveGadgetGenerationError("Workspace root or CV2.usmap mapping is invalid.")
        return root, mappings


# Prepare defensive gadget NPC plan
def prepare_defensive_gadget_npc_plan(
    catalog: DefensiveGadgetCatalog,
    seed: str | int,
    *,
    game_root: Path,
    work_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: DefensiveGadgetSerializer,
    mix_postgame_dlc_with_base: bool,
    reserved_reward_identities: Iterable[str] = (),
    source_occurrences: Iterable[RewardSourceOccurrence] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> DefensiveGadgetPreparation:
    root = work_root.resolve()
    active_grants = catalog.grants
    package_paths = tuple(sorted({slot.package_path for slot in active_grants}, key=str.casefold))
    try:
        if progress:
            progress("extracting-defensive-gadget-grants", 0, len(package_paths))
        extracted = extractor.extract_assets(
            game_root, package_paths, game_version=game_version,
            data_revision=data_revision, progress=progress,
        )
        if {asset.virtual_path.casefold() for asset in extracted} != {path.casefold() for path in package_paths}:
            raise DefensiveGadgetGenerationError("Asset extractor returned missing or unexpected grant packages.")
        materialized = materialize_extracted_assets(extracted, root / "source" / "Content")
        by_package = {asset.virtual_path.casefold(): asset for asset in materialized}
        package_files: dict[str, Path] = {}
        for virtual in package_paths:
            asset = by_package.get(virtual.casefold())
            if asset is None:
                raise DefensiveGadgetGenerationError(f"Missing materialized grant package {virtual}.")
            package_files[virtual] = asset.primary_file
            _validate_fingerprint(virtual, catalog.grants, asset)
        snapshots = serializer.inspect_packages(
            work_root=root, mappings_path=mappings_path, package_files=package_files,
        )
        _validate_snapshots(catalog.grants, snapshots)
        plan = build_defensive_gadget_npc_plan(
            catalog, seed, mix_postgame_dlc_with_base=mix_postgame_dlc_with_base,
            reserved_reward_identities=reserved_reward_identities,
            source_occurrences=source_occurrences,
        )
        if progress:
            progress("inspecting-defensive-gadget-grants", 1, 1)
        return DefensiveGadgetPreparation(plan, catalog, materialized, snapshots)
    except (AssetExtractionError, DefensiveGadgetPlanError, OSError, ValueError, KeyError) as error:
        if isinstance(error, DefensiveGadgetGenerationError):
            raise
        raise DefensiveGadgetGenerationError(f"Could not prepare Defensive Gadget NPC rewards: {error}") from error


# Apply defensive gadget NPC plan
def apply_defensive_gadget_npc_plan(
    preparation: DefensiveGadgetPreparation,
    *,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    serializer: DefensiveGadgetSerializer,
    progress: Callable[[str, int, int], None] | None = None,
) -> DefensiveGadgetGenerationResult:
    root = work_root.resolve(strict=True)
    output_root = output_content_root.resolve()
    if not _is_within(output_root, root) or output_root == (root / "source" / "Content").resolve():
        raise DefensiveGadgetGenerationError("Gadget output must be a separate Content tree inside the workspace.")
    materialized = {asset.virtual_path.casefold(): asset for asset in preparation.source_assets}
    package_assignments: dict[str, list[dict[str, Any]]] = {}
    for assignment in preparation.plan.assignments:
        if assignment.original.asset_path.casefold() == assignment.assigned.asset_path.casefold():
            continue
        slot = assignment.destination
        package_assignments.setdefault(slot.package_path, []).append({
            "exportIndex": slot.export_index,
            "exportName": slot.export_name,
            "bulkIndex": slot.bulk_index,
            "rewardIndex": slot.reward_index,
            "expectedPackage": slot.original_asset_path,
            "expectedAsset": slot.original_asset_path.rsplit("/", 1)[-1],
            "expectedCount": slot.count,
            "expectedNotification": slot.notification,
            "assignedPackage": assignment.assigned.asset_path,
            "assignedAsset": assignment.assigned.object,
        })
    if not package_assignments:
        return DefensiveGadgetGenerationResult(preparation.plan, (), (), False)
    requests: dict[str, tuple[Path, Path, tuple[dict[str, Any], ...]]] = {}
    for package, assignments in sorted(package_assignments.items(), key=lambda item: item[0].casefold()):
        source_asset = materialized.get(package.casefold())
        if source_asset is None:
            raise DefensiveGadgetGenerationError(f"Missing extracted source asset for {package}.")
        source = source_asset.primary_file.resolve(strict=True)
        output = output_root.joinpath(*package.split("/")[2:]).with_suffix(source.suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        if any(path.exists() for path in _package_files(output)):
            raise DefensiveGadgetGenerationError(f"Refusing to overwrite staged Defensive Gadget package {output.name}.")
        requests[package] = (source, output, tuple(assignments))

    committed: set[Path] = set()
    try:
        if progress:
            progress("patching-defensive-gadget-grants", 0, len(requests))
        results = serializer.patch_packages(
            work_root=root, mappings_path=mappings_path, packages=requests,
        )
        for index, result in enumerate(results, start=1):
            outputs = tuple(Path(value).resolve(strict=True) for value in result["outputFiles"])
            output_header = Path(result["outputFile"]).resolve()
            source_header = Path(result["inputFile"]).resolve()
            if output_header not in outputs or not _is_within(output_header, output_root):
                raise DefensiveGadgetGenerationError("UAssetAPI returned an invalid output package header.")
            if any(path.suffix.casefold() == ".uexp" for path in materialized[result["virtualPackage"].casefold()].files) and not any(
                path.suffix.casefold() == ".uexp" for path in outputs
            ):
                raise DefensiveGadgetGenerationError("Patched Defensive Gadget package omitted its .uexp sidecar.")
            committed.update(outputs)
            if progress:
                progress("patching-defensive-gadget-grants", index, len(requests))
        return DefensiveGadgetGenerationResult(
            preparation.plan,
            tuple(sorted(committed, key=lambda path: str(path).casefold())),
            tuple(sorted(package_assignments, key=lambda path: str(path).casefold())),
            True,
        )
    except Exception as error:
        for path in committed:
            if _is_within(path, output_root):
                path.unlink(missing_ok=True)
        if isinstance(error, DefensiveGadgetGenerationError):
            raise
        raise DefensiveGadgetGenerationError(f"Could not patch Defensive Gadget NPC grants: {error}") from error


# Check source file fingerprint
def _validate_fingerprint(package: str, grants: tuple[DefensiveGadgetGrantSlot, ...], asset: MaterializedAsset) -> None:
    expected = {ext: digest for grant in grants if grant.package_path.casefold() == package.casefold() for ext, digest in grant.source_sha256.items()}
    actual = {path.suffix.casefold(): _sha256(path) for path in asset.files if path.suffix.casefold() in {".uasset", ".uexp"}}
    if actual != expected:
        raise DefensiveGadgetGenerationError(f"Source grant package fingerprint changed for {package}.")


# Check staged package snapshots
def _validate_snapshots(grants: tuple[DefensiveGadgetGrantSlot, ...], snapshots: tuple[DefensiveGadgetGrantSnapshot, ...]) -> None:
    observed = {(item.package.casefold(), item.export_index, item.bulk_index, item.reward_index): item for item in snapshots}
    extracted_packages = {item.package.casefold() for item in snapshots}
    expected = {
        (slot.package_path.casefold(), slot.export_index, slot.bulk_index, slot.reward_index): slot
        for slot in grants if slot.package_path.casefold() in extracted_packages
    }
    if len(observed) != len(snapshots) or set(expected) != set(observed):
        raise DefensiveGadgetGenerationError("Extracted grant packages do not match the exact catalogued slots.")
    for key, slot in expected.items():
        current = observed[key]
        if (current.export_name, current.reward_package, current.reward_asset, current.count, current.notification) != (
            slot.export_name, slot.original_asset_path, slot.original_asset_path.rsplit("/", 1)[-1], slot.count, slot.notification,
        ):
            raise DefensiveGadgetGenerationError(f"Extracted Defensive Gadget grant differs from catalog at {slot.slot_id}.")


# Parse snapshot
def _parse_snapshot(raw: Any, expected_package: str) -> DefensiveGadgetGrantSnapshot:
    if not isinstance(raw, dict) or raw.get("package") != expected_package:
        raise DefensiveGadgetGenerationError("UAssetAPI returned a grant under the wrong package.")
    integers = ("exportIndex", "bulkIndex", "rewardIndex", "count")
    if any(type(raw.get(key)) is not int for key in integers) or type(raw.get("notification")) is not bool:
        raise DefensiveGadgetGenerationError("UAssetAPI returned invalid Defensive Gadget grant fields.")
    return DefensiveGadgetGrantSnapshot(
        expected_package, raw["exportIndex"], _string(raw.get("exportName"), "exportName"),
        raw["bulkIndex"], raw["rewardIndex"], _string(raw.get("rewardPackage"), "rewardPackage"),
        _string(raw.get("rewardAsset"), "rewardAsset"), raw["count"], raw["notification"],
    )


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DefensiveGadgetGenerationError(f"UAssetAPI returned an invalid {label}.")
    return value


# Build package files
def _package_files(header: Path) -> tuple[Path, ...]:
    return tuple(header.with_suffix(extension) for extension in (".uasset", ".uexp", ".ubulk", ".uptnl"))


# Calculate the SHA-256 hash
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
