"""Extract, validate, deterministically plan, and patch confirmed Blood Code grants."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
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
from cv2randomizer.game.blood_code_catalog import (
    BloodCodeCatalog,
    BloodCodeGrant,
    BloodCodeObjectReferenceTarget,
    BloodCodeReferenceTarget,
)
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION
from cv2randomizer.randomizer.blood_codes import (
    BloodCodeAssignment,
    BloodCodePlan,
    BloodCodePlanError,
    build_blood_code_plan,
)


class BloodCodeGenerationError(RuntimeError):
    """Raised when a Blood Code source or output fails closed validation."""


@dataclass(frozen=True, slots=True)
class BloodCodeGrantSnapshot:
    virtual_package: str
    export_index: int
    export_name: str
    bulk_index: int
    reward_index: int
    reward_package: str
    reward_asset: str
    count: int
    notification: bool
    package_name_value: str = ""
    package_name_number: int = 0
    asset_name_value: str = ""
    asset_name_number: int = 0
    sub_path_string: str = ""
    articles_count: int | None = None


@dataclass(frozen=True, slots=True)
class BloodCodeReferenceSnapshot:
    virtual_package: str
    export_index: int
    export_name: str
    reference_property: str
    reference_index: int
    array_count: int
    reward_package: str
    reward_asset: str
    package_name_value: str
    package_name_number: int
    asset_name_value: str
    asset_name_number: int
    sub_path_string: str
    package_name_map_index: int
    asset_name_map_index: int


@dataclass(frozen=True, slots=True)
class BloodCodeObjectReferenceSnapshot:
    virtual_package: str
    export_index: int
    export_name: str
    reference_property: str
    property_schema_index: int
    import_index: int
    package_import_index: int
    import_class_name: str
    import_class_package: str
    reward_package: str
    package_name_number: int
    reward_asset: str
    asset_name_number: int
    owner_exports: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BloodCodePreparation:
    plan: BloodCodePlan
    catalog: BloodCodeCatalog
    source_assets: tuple[MaterializedAsset, ...]
    snapshots: tuple[BloodCodeGrantSnapshot, ...]
    reference_snapshots: tuple[BloodCodeReferenceSnapshot, ...] = ()
    object_reference_snapshots: tuple[BloodCodeObjectReferenceSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class BloodCodeGenerationResult:
    plan: BloodCodePlan
    output_files: tuple[Path, ...]
    patched_packages: tuple[str, ...]
    reopened: bool


# Resolve required blood code package paths
def required_blood_code_package_paths(
    catalog: BloodCodeCatalog,
) -> tuple[str, ...]:
    """Return all physical owner/reference packages in the filtered catalog."""
    return tuple(sorted(catalog.source_packages, key=str.casefold))


class BloodCodeSerializer:
    """Invoke the existing UAssetAPI helper for Blood Code package inspection and patching."""

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
            raise ValueError(f"Blood Code serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Inspect extracted packages
    def inspect_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        package_files: Mapping[str, Path],
    ) -> tuple[BloodCodeGrantSnapshot, ...]:
        return self._inspect_packages(
            "inspect-blood-codes", "Blood Code", work_root=work_root,
            mappings_path=mappings_path, package_files=package_files,
        )

    # Inspect referenced packages
    def inspect_reference_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        package_files: Mapping[str, Path],
        targets: tuple[BloodCodeReferenceTarget, ...],
    ) -> tuple[BloodCodeReferenceSnapshot, ...]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        grouped: dict[str, list[BloodCodeReferenceTarget]] = defaultdict(list)
        for target in targets:
            grouped[target.virtual_package].append(target)
        if not grouped or set(grouped) != set(package_files):
            raise BloodCodeGenerationError("Blood Code reference inspection packages do not match their targets.")
        packages = []
        for virtual, package_targets in sorted(grouped.items(), key=lambda item: item[0].casefold()):
            path = package_files[virtual].expanduser().resolve(strict=True)
            if not _is_within(path, root) or not path.is_file():
                raise BloodCodeGenerationError("Blood Code reference packages must be files inside the generation workspace.")
            packages.append({
                "virtualPackage": virtual,
                "inputFile": str(path),
                "references": [_reference_target_json(target) for target in package_targets],
            })
        result = self._run_json(root, mappings, "inspect-blood-code-references", {"packages": packages})
        if not isinstance(result, list) or len(result) != len(packages):
            raise BloodCodeGenerationError("UAssetAPI returned the wrong number of inspected Blood Code reference packages.")
        snapshots: list[BloodCodeReferenceSnapshot] = []
        for expected, output in zip(packages, result, strict=True):
            if (
                not isinstance(output, dict)
                or output.get("virtualPackage") != expected["virtualPackage"]
                or Path(str(output.get("inputFile", ""))).resolve() != Path(expected["inputFile"])
            ):
                raise BloodCodeGenerationError("UAssetAPI returned unexpected Blood Code reference package identity.")
            references = output.get("references")
            if not isinstance(references, list) or output.get("referenceCount") != len(references):
                raise BloodCodeGenerationError("UAssetAPI omitted or miscounted Blood Code references.")
            for raw in references:
                snapshots.append(_parse_reference(raw, expected["virtualPackage"]))
        if len(snapshots) != len(targets):
            raise BloodCodeGenerationError("UAssetAPI returned an incomplete Blood Code reference inventory.")
        return tuple(snapshots)

    # Inspect object reference packages
    def inspect_object_reference_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        package_files: Mapping[str, Path],
        targets: tuple[BloodCodeObjectReferenceTarget, ...],
    ) -> tuple[BloodCodeObjectReferenceSnapshot, ...]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        grouped: dict[str, list[BloodCodeObjectReferenceTarget]] = defaultdict(list)
        for target in targets:
            grouped[target.virtual_package].append(target)
        if not grouped or set(grouped) != set(package_files):
            raise BloodCodeGenerationError("BuddySettings inspection packages do not match their object-reference targets.")
        packages = []
        for virtual, package_targets in sorted(grouped.items(), key=lambda item: item[0].casefold()):
            path = package_files[virtual].expanduser().resolve(strict=True)
            if not _is_within(path, root) or not path.is_file():
                raise BloodCodeGenerationError("BuddySettings source packages must be files inside the generation workspace.")
            packages.append({
                "virtualPackage": virtual,
                "inputFile": str(path),
                "references": [_object_reference_target_json(target) for target in package_targets],
            })
        result = self._run_json(root, mappings, "inspect-blood-code-buddy-settings", {"packages": packages})
        if not isinstance(result, list) or len(result) != len(packages):
            raise BloodCodeGenerationError("UAssetAPI returned the wrong number of inspected BuddySettings packages.")
        snapshots: list[BloodCodeObjectReferenceSnapshot] = []
        for expected, output in zip(packages, result, strict=True):
            if (
                not isinstance(output, dict)
                or output.get("virtualPackage") != expected["virtualPackage"]
                or Path(str(output.get("inputFile", ""))).resolve() != Path(expected["inputFile"])
            ):
                raise BloodCodeGenerationError("UAssetAPI returned unexpected BuddySettings package identity.")
            references = output.get("references")
            if not isinstance(references, list) or output.get("referenceCount") != len(references):
                raise BloodCodeGenerationError("UAssetAPI omitted or miscounted BuddySettings references.")
            snapshots.extend(_parse_object_reference(raw, expected["virtualPackage"]) for raw in references)
        if len(snapshots) != len(targets):
            raise BloodCodeGenerationError("UAssetAPI returned an incomplete BuddySettings reference inventory.")
        return tuple(snapshots)

    # Inspect Forma reward packages
    def inspect_bequeathed_formae_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        package_files: Mapping[str, Path],
    ) -> tuple[BloodCodeGrantSnapshot, ...]:
        """Inspect Gadget GainItem payloads with the same UAssetAPI quest parser."""
        return self._inspect_packages(
            "inspect-bequeathed-formae", "Bequeathed Forma", work_root=work_root,
            mappings_path=mappings_path, package_files=package_files,
        )

    # Inspect quest reward packages
    def inspect_world_loot_quest_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        package_files: Mapping[str, Path],
    ) -> tuple[BloodCodeGrantSnapshot, ...]:
        """Inspect structured Article GainItem grants for the shared World Loot pool."""
        return self._inspect_packages(
            "inspect-world-loot-quest-articles", "World Loot quest Article",
            work_root=work_root, mappings_path=mappings_path, package_files=package_files,
        )

    # Inspect extracted asset packages
    def _inspect_packages(
        self,
        operation: str,
        feature_name: str,
        *,
        work_root: Path,
        mappings_path: Path,
        package_files: Mapping[str, Path],
    ) -> tuple[BloodCodeGrantSnapshot, ...]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        if not package_files:
            raise BloodCodeGenerationError(f"{feature_name} inspection requires at least one source package.")
        requests = []
        for virtual, raw_path in sorted(package_files.items(), key=lambda item: item[0].casefold()):
            path = raw_path.expanduser().resolve(strict=True)
            if not _is_within(path, root) or not path.is_file():
                raise BloodCodeGenerationError(f"{feature_name} source packages must be files inside the generation workspace.")
            requests.append({"virtualPackage": virtual, "inputFile": str(path)})
        result = self._run_json(root, mappings, operation, {"packages": requests})
        if not isinstance(result, list) or len(result) != len(requests):
            raise BloodCodeGenerationError(f"UAssetAPI returned the wrong number of inspected {feature_name} packages.")
        snapshots: list[BloodCodeGrantSnapshot] = []
        for expected, output in zip(requests, result, strict=True):
            if not isinstance(output, dict) or output.get("virtualPackage") != expected["virtualPackage"]:
                raise BloodCodeGenerationError(f"UAssetAPI returned unexpected {feature_name} package identities.")
            if Path(str(output.get("inputFile", ""))).resolve() != Path(expected["inputFile"]):
                raise BloodCodeGenerationError(f"UAssetAPI inspected an unexpected {feature_name} package file.")
            raw_grants = output.get("grants")
            if not isinstance(raw_grants, list) or output.get("grantCount") != len(raw_grants):
                raise BloodCodeGenerationError(f"UAssetAPI omitted or miscounted grants for {expected['virtualPackage']}.")
            for raw in raw_grants:
                if not isinstance(raw, dict):
                    raise BloodCodeGenerationError("UAssetAPI returned an invalid Blood Code grant record.")
                snapshots.append(_parse_grant(raw, expected["virtualPackage"]))
        return tuple(snapshots)

    # Patch package reward data
    def patch_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        patch_packages: Mapping[str, tuple[Path, Path, tuple[dict[str, Any], ...]]],
    ) -> list[dict[str, Any]]:
        return self._patch_packages(
            "patch-blood-codes", "Blood Code", work_root=work_root,
            mappings_path=mappings_path, patch_packages=patch_packages,
        )

    # Patch reference packages
    def patch_reference_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        patch_packages: Mapping[str, tuple[Path, Path, tuple[dict[str, Any], ...]]],
    ) -> list[dict[str, Any]]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        requests = []
        for virtual, (source_path, output_path, assignments) in sorted(patch_packages.items(), key=lambda item: item[0].casefold()):
            source = source_path.expanduser().resolve(strict=True)
            output = output_path.expanduser().resolve()
            if not _is_within(source, root) or not _is_within(output, root) or source == output:
                raise BloodCodeGenerationError("Blood Code reference input and output must be separate files inside the generation workspace.")
            if not assignments:
                raise BloodCodeGenerationError(f"{virtual} has no changed Blood Code references.")
            requests.append({"virtualPackage": virtual, "inputFile": str(source), "outputFile": str(output), "assignments": list(assignments)})
        if not requests:
            raise BloodCodeGenerationError("Blood Code reference patch request contains no changed packages.")
        result = self._run_json(root, mappings, "patch-blood-code-references", {"packages": requests})
        if not isinstance(result, list) or len(result) != len(requests):
            raise BloodCodeGenerationError("UAssetAPI returned the wrong number of patched Blood Code reference packages.")
        return result

    # Patch object reference packages
    def patch_object_reference_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        patch_packages: Mapping[str, tuple[Path, Path, tuple[dict[str, Any], ...]]],
    ) -> list[dict[str, Any]]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        requests = []
        for virtual, (source_path, output_path, assignments) in sorted(patch_packages.items(), key=lambda item: item[0].casefold()):
            source = source_path.expanduser().resolve(strict=True)
            output = output_path.expanduser().resolve()
            if not _is_within(source, root) or not _is_within(output, root) or source == output:
                raise BloodCodeGenerationError("BuddySettings patch input and output must be separate files inside the generation workspace.")
            if not assignments:
                raise BloodCodeGenerationError(f"{virtual} has no changed BuddySettings Blood Code references.")
            requests.append({"virtualPackage": virtual, "inputFile": str(source), "outputFile": str(output), "assignments": list(assignments)})
        if not requests:
            raise BloodCodeGenerationError("BuddySettings object-reference patch request contains no changed packages.")
        result = self._run_json(root, mappings, "patch-blood-code-buddy-settings", {"packages": requests})
        if not isinstance(result, list) or len(result) != len(requests):
            raise BloodCodeGenerationError("UAssetAPI returned the wrong number of patched BuddySettings packages.")
        return result

    # Patch bequeathed formae packages
    def patch_bequeathed_formae_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        patch_packages: Mapping[str, tuple[Path, Path, tuple[dict[str, Any], ...]]],
    ) -> list[dict[str, Any]]:
        """Patch Gadget GainItem paths through the existing snapshotting serializer."""
        return self._patch_packages(
            "patch-bequeathed-formae", "Bequeathed Forma", work_root=work_root,
            mappings_path=mappings_path, patch_packages=patch_packages,
        )

    # Patch world loot quest packages
    def patch_world_loot_quest_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        patch_packages: Mapping[str, tuple[Path, Path, tuple[dict[str, Any], ...]]],
    ) -> list[dict[str, Any]]:
        """Patch generic canonical Article paths in structured quest GainItem exports."""
        return self._patch_packages(
            "patch-world-loot-quest-articles", "World Loot quest Article",
            work_root=work_root, mappings_path=mappings_path, patch_packages=patch_packages,
        )

    # Patch package reward data
    def _patch_packages(
        self,
        operation: str,
        feature_name: str,
        *,
        work_root: Path,
        mappings_path: Path,
        patch_packages: Mapping[str, tuple[Path, Path, tuple[dict[str, Any], ...]]],
    ) -> list[dict[str, Any]]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        requests = []
        for virtual, (source_path, output_path, assignments) in sorted(patch_packages.items(), key=lambda item: item[0].casefold()):
            source = source_path.expanduser().resolve(strict=True)
            output = output_path.expanduser().resolve()
            if not _is_within(source, root) or not _is_within(output, root) or source == output:
                raise BloodCodeGenerationError(f"{feature_name} input and output packages must be separate files inside the generation workspace.")
            if not assignments:
                raise BloodCodeGenerationError(f"{virtual} has no changed {feature_name} assignments.")
            requests.append({"virtualPackage": virtual, "inputFile": str(source), "outputFile": str(output), "assignments": list(assignments)})
        if not requests:
            raise BloodCodeGenerationError(f"{feature_name} patch request contains no changed packages.")
        result = self._run_json(root, mappings, operation, {"packages": requests})
        if not isinstance(result, list) or len(result) != len(requests):
            raise BloodCodeGenerationError(f"UAssetAPI returned the wrong number of patched {feature_name} packages.")
        return result

    # Check package paths stay contained
    def _validate_roots(self, work_root: Path, mappings_path: Path) -> tuple[Path, Path]:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise BloodCodeGenerationError("Blood Code helper, generation workspace, or CV2.usmap mapping is missing.")
        return root, mappings

    # Run helper and parse JSON
    def _run_json(self, root: Path, mappings: Path, operation: str, document: dict[str, Any]) -> Any:
        manifest = root / f".cv2-blood-codes-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project, self.dotnet_executable, operation, str(root), str(mappings),
            self.engine_version, str(manifest),
        )
        try:
            try:
                completed = self.runner(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout_seconds, cwd=root)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise BloodCodeGenerationError(f"Could not run UAssetAPI Blood Code helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise BloodCodeGenerationError(f"UAssetAPI {operation} failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise BloodCodeGenerationError(f"UAssetAPI {operation} returned no JSON result.")
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError as error:
                raise BloodCodeGenerationError(f"UAssetAPI {operation} did not return valid JSON.") from error
        finally:
            manifest.unlink(missing_ok=True)


# Prepare blood code plan
def prepare_blood_code_plan(
    catalog: BloodCodeCatalog,
    seed: str | int,
    *,
    game_root: Path,
    work_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: BloodCodeSerializer,
    mix_postgame_dlc_with_base: bool,
    progress: Callable[[str, int, int], None] | None = None,
) -> BloodCodePreparation:
    root = work_root.expanduser().resolve()
    mappings = mappings_path.expanduser().resolve(strict=True)
    packages = required_blood_code_package_paths(catalog)
    try:
        if progress:
            progress("extracting-blood-code-quests", 0, len(packages))
        extracted = extractor.extract_assets(game_root, packages, game_version=game_version, data_revision=data_revision, progress=progress)
        by_virtual = {asset.virtual_path.casefold(): asset for asset in extracted}
        if len(by_virtual) != len(packages) or set(by_virtual) != {package.casefold() for package in packages}:
            raise BloodCodeGenerationError("Asset extraction returned missing, duplicate, or unexpected Blood Code quest packages.")
        source_assets = materialize_extracted_assets(extracted, root / "source" / "Content")
        assets_by_virtual = {asset.virtual_path.casefold(): asset for asset in source_assets}
        files = {package: assets_by_virtual[package.casefold()].primary_file for package in packages}
        _validate_fingerprints(catalog.source_package_sha256, assets_by_virtual)
        direct_packages = {
            slot.virtual_package for slot in catalog.slots if slot.location_kind == "gain_item"
        }
        direct_files = {package: files[package] for package in direct_packages}
        snapshots = (
            serializer.inspect_packages(work_root=root, mappings_path=mappings, package_files=direct_files)
            if direct_files else ()
        )
        _validate_snapshots(catalog, snapshots)
        reference_targets = tuple(
            target
            for slot in catalog.slots if slot.physical_reference_targets
            for target in slot.physical_reference_targets
        )
        reference_files = {target.virtual_package: files[target.virtual_package] for target in reference_targets}
        reference_snapshots = (
            serializer.inspect_reference_packages(
                work_root=root, mappings_path=mappings, package_files=reference_files,
                targets=reference_targets,
            )
            if reference_targets else ()
        )
        _validate_reference_snapshots(catalog, reference_snapshots)
        object_reference_targets = tuple(
            target
            for slot in catalog.slots if slot.physical_object_reference_targets
            for target in slot.physical_object_reference_targets
        )
        object_reference_files = {target.virtual_package: files[target.virtual_package] for target in object_reference_targets}
        object_reference_snapshots = (
            serializer.inspect_object_reference_packages(
                work_root=root, mappings_path=mappings, package_files=object_reference_files,
                targets=object_reference_targets,
            )
            if object_reference_targets else ()
        )
        _validate_object_reference_snapshots(catalog, object_reference_snapshots)
        plan = build_blood_code_plan(
            catalog, seed, mix_postgame_dlc_with_base=mix_postgame_dlc_with_base,
        )
        if progress:
            progress("inspecting-blood-code-quests", len(packages), len(packages))
        return BloodCodePreparation(
            plan, catalog, tuple(assets_by_virtual[p.casefold()] for p in packages),
            snapshots, reference_snapshots, object_reference_snapshots,
        )
    except (AssetExtractionError, BloodCodePlanError, OSError, ValueError, KeyError) as error:
        if isinstance(error, BloodCodeGenerationError):
            raise
        raise BloodCodeGenerationError(f"Could not prepare Blood Code quest grants: {error}") from error


# Apply blood code plan
def apply_blood_code_plan(
    preparation: BloodCodePreparation,
    *,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    serializer: BloodCodeSerializer,
    progress: Callable[[str, int, int], None] | None = None,
) -> BloodCodeGenerationResult:
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    if not _is_within(output_root, root) or output_root == root or output_root == (root / "source" / "Content").resolve():
        raise BloodCodeGenerationError("Blood Code output must be a separate Content tree inside the generation workspace.")
    output_root.mkdir(parents=True, exist_ok=True)
    source_by_package = {asset.virtual_path: asset for asset in preparation.source_assets}
    assignments_by_package: dict[str, list[dict[str, Any]]] = defaultdict(list)
    references_by_package: dict[str, list[dict[str, Any]]] = defaultdict(list)
    object_references_by_package: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assignment in preparation.plan.assignments:
        if assignment.destination.original_blood_code_package == assignment.source.original_blood_code_package:
            continue
        destination = assignment.destination
        source = assignment.source
        if destination.location_kind == "gain_item":
            assignments_by_package[destination.virtual_package].append({
                "exportIndex": destination.export_index,
                "exportName": destination.export_name,
                "bulkIndex": destination.bulk_index,
                "rewardIndex": destination.reward_index,
                "expectedPackage": destination.original_blood_code_package,
                "expectedAsset": destination.original_blood_code_asset,
                "expectedCount": destination.count,
                "expectedNotification": destination.b_notification,
                "assignedPackage": source.original_blood_code_package,
                "assignedAsset": source.original_blood_code_asset,
            })
        for target in destination.physical_object_reference_targets:
            object_references_by_package[target.virtual_package].append({
                **_object_reference_target_json(target),
                "expectedPackage": destination.original_blood_code_package,
                "expectedAsset": destination.original_blood_code_asset,
                "assignedPackage": source.original_blood_code_package,
                "assignedAsset": source.original_blood_code_asset,
            })
        for target in destination.physical_reference_targets:
            references_by_package[target.virtual_package].append({
                **_reference_target_json(target),
                "expectedPackage": destination.original_blood_code_package,
                "expectedAsset": destination.original_blood_code_asset,
                "assignedPackage": source.original_blood_code_package,
                "assignedAsset": source.original_blood_code_asset,
            })
    requests: dict[str, tuple[Path, Path, tuple[dict[str, Any], ...]]] = {}
    reference_requests: dict[str, tuple[Path, Path, tuple[dict[str, Any], ...]]] = {}
    object_reference_requests: dict[str, tuple[Path, Path, tuple[dict[str, Any], ...]]] = {}
    owned_headers: list[Path] = []
    committed: list[Path] = []
    try:
        package_modes = (set(assignments_by_package), set(references_by_package), set(object_references_by_package))
        if any(left & right for index, left in enumerate(package_modes) for right in package_modes[index + 1:]):
            raise BloodCodeGenerationError("A Blood Code source package cannot overlap direct, soft-reference, and object-reference patch modes.")
        for grouped, target in (
            (assignments_by_package, requests),
            (references_by_package, reference_requests),
            (object_references_by_package, object_reference_requests),
        ):
            for virtual, assignments in sorted(grouped.items(), key=lambda item: item[0].casefold()):
                source = source_by_package[virtual].primary_file.resolve(strict=True)
                output = output_root.joinpath(*virtual.split("/")[2:]).with_suffix(source.suffix)
                output.parent.mkdir(parents=True, exist_ok=True)
                if any(path.exists() for path in _package_files(output)):
                    raise BloodCodeGenerationError(f"Refusing to overwrite staged Blood Code package {output.name}.")
                owned_headers.append(output)
                target[virtual] = (source, output, tuple(assignments))
        all_requests = {**requests, **reference_requests, **object_reference_requests}
        if all_requests:
            if progress:
                progress("patching-blood-codes", 0, len(all_requests))
        raw_results: list[dict[str, Any]] = []
        if requests:
            raw_results = serializer.patch_packages(work_root=root, mappings_path=mappings_path, patch_packages=requests)
        if reference_requests:
            raw_results.extend(serializer.patch_reference_packages(
                work_root=root, mappings_path=mappings_path, patch_packages=reference_requests,
            ))
        if object_reference_requests:
            raw_results.extend(serializer.patch_object_reference_packages(
                work_root=root, mappings_path=mappings_path, patch_packages=object_reference_requests,
            ))
        by_package = {item.get("virtualPackage"): item for item in raw_results if isinstance(item, dict)}
        if set(by_package) != set(all_requests):
            raise BloodCodeGenerationError("UAssetAPI patched unexpected Blood Code package paths.")
        for index, (virtual, (source, output, assignments)) in enumerate(all_requests.items(), start=1):
            result = by_package[virtual]
            if Path(str(result.get("inputFile", ""))).resolve() != source or Path(str(result.get("outputFile", ""))).resolve() != output:
                raise BloodCodeGenerationError(f"UAssetAPI returned unexpected paths for {virtual}.")
            expected_count = len(assignments)
            actual_count = (
                result.get("patchedReferences")
                if virtual in reference_requests or virtual in object_reference_requests
                else result.get("patchedSlots")
            )
            if actual_count != expected_count or result.get("virtualPackage") != virtual:
                raise BloodCodeGenerationError(f"UAssetAPI reported an invalid patch count for {virtual}.")
            output_paths = result.get("outputFiles")
            if not isinstance(output_paths, list):
                raise BloodCodeGenerationError(f"UAssetAPI omitted staged sidecar inventory for {virtual}.")
            package_files = tuple(Path(str(path)).resolve(strict=True) for path in output_paths)
            if output.resolve() not in package_files or any(
                not _is_within(path, output_root) or path.stem.casefold() != output.stem.casefold()
                or path.suffix.casefold() not in {".uasset", ".uexp", ".ubulk", ".uptnl"}
                for path in package_files
            ):
                raise BloodCodeGenerationError(f"UAssetAPI returned an invalid output inventory for {virtual}.")
            if any(source_asset.suffix.casefold() == ".uexp" for source_asset in source_by_package[virtual].files) and not any(path.suffix.casefold() == ".uexp" for path in package_files):
                raise BloodCodeGenerationError(f"Patched Blood Code package {virtual} is missing its .uexp sidecar.")
            committed.extend(package_files)
            if progress:
                progress("patching-blood-codes", index, len(all_requests))
        if len(committed) != len(set(committed)):
            raise BloodCodeGenerationError("Blood Code patcher returned duplicate output files.")
        patched_packages = set(all_requests)
        return BloodCodeGenerationResult(
            preparation.plan, tuple(sorted(committed)), tuple(sorted(patched_packages, key=str.casefold)), True,
        )
    except Exception as error:
        cleanup = set(committed)
        for header in owned_headers:
            cleanup.update(path for path in _package_files(header) if path.exists())
        for path in cleanup:
            if _is_within(path, output_root):
                path.unlink(missing_ok=True)
        if isinstance(error, BloodCodeGenerationError):
            raise
        raise BloodCodeGenerationError(f"Could not apply Blood Code plan: {error}") from error


# Parse grant
def _parse_grant(raw: dict[str, Any], expected_virtual: str) -> BloodCodeGrantSnapshot:
    if raw.get("package") != expected_virtual:
        raise BloodCodeGenerationError("UAssetAPI returned a grant under the wrong virtual package.")
    integers = ("exportIndex", "bulkIndex", "rewardIndex", "count")
    if any(type(raw.get(key)) is not int for key in integers) or type(raw.get("notification")) is not bool:
        raise BloodCodeGenerationError("UAssetAPI returned invalid Blood Code grant fields.")
    optional_integers = ("packageNameNumber", "assetNameNumber")
    for key in optional_integers:
        if key in raw and type(raw[key]) is not int:
            raise BloodCodeGenerationError("UAssetAPI returned invalid raw FName Number metadata.")
    if "articlesCount" in raw and (
        type(raw["articlesCount"]) is not int or raw["articlesCount"] < 1
    ):
        raise BloodCodeGenerationError("UAssetAPI returned invalid quest Articles count metadata.")
    optional_strings = ("packageNameValue", "assetNameValue", "subPathString")
    for key in optional_strings:
        if key in raw and not isinstance(raw[key], str):
            raise BloodCodeGenerationError("UAssetAPI returned invalid raw FName/path metadata.")
    return BloodCodeGrantSnapshot(
        expected_virtual,
        raw["exportIndex"],
        _string(raw.get("exportName"), "exportName"),
        raw["bulkIndex"],
        raw["rewardIndex"],
        _string(raw.get("rewardPackage"), "rewardPackage"),
        _string(raw.get("rewardAsset"), "rewardAsset"),
        raw["count"],
        raw["notification"],
        str(raw.get("packageNameValue", raw.get("rewardPackage", ""))),
        raw.get("packageNameNumber", 0),
        str(raw.get("assetNameValue", raw.get("rewardAsset", ""))),
        raw.get("assetNameNumber", 0),
        str(raw.get("subPathString", "")),
        raw.get("articlesCount"),
    )


# Check staged package snapshots
def _validate_snapshots(catalog: BloodCodeCatalog, snapshots: tuple[BloodCodeGrantSnapshot, ...]) -> None:
    packages = {snapshot.virtual_package.casefold() for snapshot in snapshots}
    expected = {
        (slot.virtual_package, slot.export_index, slot.bulk_index, slot.reward_index): slot
        for slot in catalog.slots
        if slot.location_kind == "gain_item" and slot.virtual_package.casefold() in packages
    }
    observed = {
        (snapshot.virtual_package, snapshot.export_index, snapshot.bulk_index, snapshot.reward_index): snapshot
        for snapshot in snapshots
    }
    if len(observed) != len(snapshots) or set(observed) != set(expected):
        raise BloodCodeGenerationError("Extracted Blood Code grants do not match all catalogued direct locations.")
    for key, slot in expected.items():
        snapshot = observed[key]
        if (snapshot.export_name, snapshot.reward_package, snapshot.reward_asset, snapshot.count, snapshot.notification) != (
            slot.export_name, slot.original_blood_code_package, slot.original_blood_code_asset, slot.count, slot.b_notification,
        ):
            raise BloodCodeGenerationError(f"Extracted Blood Code grant differs from the catalog at {slot.slot_id}.")


# Resolve reference target JSON
def _reference_target_json(target: BloodCodeReferenceTarget) -> dict[str, Any]:
    return {
        "exportIndex": target.export_index,
        "exportName": target.export_name,
        "referenceProperty": target.reference_property,
        "referenceIndex": target.reference_index,
        "expectedArrayCount": target.expected_array_count,
        "expectedPackage": target.expected_package_name_value,
        "expectedAsset": target.expected_asset_name_value,
        "expectedPackageNameValue": target.expected_package_name_value,
        "expectedPackageNameNumber": target.expected_package_name_number,
        "expectedAssetNameValue": target.expected_asset_name_value,
        "expectedAssetNameNumber": target.expected_asset_name_number,
        "expectedSuperClass": target.expected_super_class,
    }


# Parse object reference target JSON
def _object_reference_target_json(target: BloodCodeObjectReferenceTarget) -> dict[str, Any]:
    return {
        "exportIndex": target.export_index,
        "exportName": target.export_name,
        "referenceProperty": target.reference_property,
        "propertySchemaIndex": target.property_schema_index,
        "expectedPackage": target.expected_import_package,
        "expectedAsset": target.expected_import_asset,
        "expectedPackageNameNumber": target.expected_package_name_number,
        "expectedAssetNameNumber": target.expected_asset_name_number,
        "expectedImportClassName": target.expected_import_class_name,
        "expectedImportClassPackage": target.expected_import_class_package,
    }


# Parse reference
def _parse_reference(raw: Any, expected_virtual: str) -> BloodCodeReferenceSnapshot:
    if not isinstance(raw, dict) or raw.get("package") != expected_virtual:
        raise BloodCodeGenerationError("UAssetAPI returned a Blood Code reference under the wrong package.")
    integer_fields = (
        "exportIndex", "referenceIndex", "arrayCount", "packageNameNumber", "assetNameNumber",
        "packageNameMapIndex", "assetNameMapIndex",
    )
    if any(type(raw.get(field)) is not int for field in integer_fields):
        raise BloodCodeGenerationError("UAssetAPI returned invalid Blood Code reference indices or FName numbers.")
    string_fields = (
        "exportName", "referenceProperty", "rewardPackage", "rewardAsset",
        "packageNameValue", "assetNameValue", "subPathString",
    )
    if any(not isinstance(raw.get(field), str) for field in string_fields):
        raise BloodCodeGenerationError("UAssetAPI returned invalid Blood Code reference path metadata.")
    return BloodCodeReferenceSnapshot(
        expected_virtual,
        raw["exportIndex"], raw["exportName"], raw["referenceProperty"],
        raw["referenceIndex"], raw["arrayCount"], raw["rewardPackage"], raw["rewardAsset"],
        raw["packageNameValue"], raw["packageNameNumber"], raw["assetNameValue"],
        raw["assetNameNumber"], raw["subPathString"], raw["packageNameMapIndex"],
        raw["assetNameMapIndex"],
    )


# Validate reference snapshots
def _validate_reference_snapshots(
    catalog: BloodCodeCatalog,
    snapshots: tuple[BloodCodeReferenceSnapshot, ...],
) -> None:
    expected: dict[tuple[str, int, str, int], tuple[BloodCodeGrant, BloodCodeReferenceTarget]] = {}
    for slot in catalog.slots:
        if not slot.physical_reference_targets:
            continue
        for target in slot.physical_reference_targets:
            key = (target.virtual_package.casefold(), target.export_index, target.reference_property, target.reference_index)
            if key in expected:
                raise BloodCodeGenerationError(f"Duplicate catalogued Blood Code reference target for {slot.slot_id}.")
            expected[key] = (slot, target)
    observed = {
        (item.virtual_package.casefold(), item.export_index, item.reference_property, item.reference_index): item
        for item in snapshots
    }
    if len(observed) != len(snapshots) or set(observed) != set(expected):
        raise BloodCodeGenerationError("Extracted proficiency references do not match the Blood Code catalog.")
    for key, (slot, target) in expected.items():
        snapshot = observed[key]
        actual = (
            snapshot.export_name, snapshot.array_count, snapshot.reward_package, snapshot.reward_asset,
            snapshot.package_name_value, snapshot.package_name_number,
            snapshot.asset_name_value, snapshot.asset_name_number, snapshot.sub_path_string,
        )
        wanted = (
            target.export_name, target.expected_array_count, slot.original_blood_code_package,
            slot.original_blood_code_asset, target.expected_package_name_value,
            target.expected_package_name_number, target.expected_asset_name_value,
            target.expected_asset_name_number, "",
        )
        if actual != wanted:
            raise BloodCodeGenerationError(
                f"Extracted BloodCodeArticleDatas differs from the catalog at {slot.display_name} "
                f"in {target.virtual_package}[{target.reference_index}]."
            )
        if snapshot.package_name_map_index < 0 or snapshot.asset_name_map_index < 0:
            raise BloodCodeGenerationError(f"Blood Code reference FName is absent from the NameMap at {target.virtual_package}.")


# Parse object reference
def _parse_object_reference(raw: Any, expected_virtual: str) -> BloodCodeObjectReferenceSnapshot:
    if not isinstance(raw, dict) or raw.get("package") != expected_virtual:
        raise BloodCodeGenerationError("UAssetAPI returned a BuddySettings reference under the wrong package.")
    integer_fields = (
        "exportIndex", "propertySchemaIndex", "importIndex", "packageImportIndex",
        "packageNameNumber", "assetNameNumber",
    )
    if any(type(raw.get(field)) is not int for field in integer_fields):
        raise BloodCodeGenerationError("UAssetAPI returned invalid BuddySettings import or FName metadata.")
    owner_exports = raw.get("ownerExports")
    if not isinstance(owner_exports, list) or any(type(value) is not int for value in owner_exports):
        raise BloodCodeGenerationError("UAssetAPI returned an invalid BuddySettings import-owner list.")
    string_fields = (
        "exportName", "referenceProperty", "importClassName", "importClassPackage",
        "rewardPackage", "rewardAsset",
    )
    if any(not isinstance(raw.get(field), str) for field in string_fields):
        raise BloodCodeGenerationError("UAssetAPI returned invalid BuddySettings object-reference strings.")
    return BloodCodeObjectReferenceSnapshot(
        expected_virtual, raw["exportIndex"], raw["exportName"], raw["referenceProperty"],
        raw["propertySchemaIndex"], raw["importIndex"], raw["packageImportIndex"],
        raw["importClassName"], raw["importClassPackage"], raw["rewardPackage"],
        raw["packageNameNumber"], raw["rewardAsset"], raw["assetNameNumber"], tuple(owner_exports),
    )


# Validate object reference snapshots
def _validate_object_reference_snapshots(
    catalog: BloodCodeCatalog,
    snapshots: tuple[BloodCodeObjectReferenceSnapshot, ...],
) -> None:
    expected: dict[tuple[str, int, str], tuple[BloodCodeGrant, BloodCodeObjectReferenceTarget]] = {}
    for slot in catalog.slots:
        for target in slot.physical_object_reference_targets:
            key = (target.virtual_package.casefold(), target.export_index, target.reference_property)
            if key in expected:
                raise BloodCodeGenerationError(f"Duplicate BuddySettings Blood Code reference for {slot.slot_id}.")
            expected[key] = (slot, target)
    observed = {
        (item.virtual_package.casefold(), item.export_index, item.reference_property): item
        for item in snapshots
    }
    if len(observed) != len(snapshots) or set(observed) != set(expected):
        raise BloodCodeGenerationError("Extracted BuddySettings Blood Code imports do not match the catalog.")
    for key, (slot, target) in expected.items():
        snapshot = observed[key]
        actual = (
            snapshot.export_name, snapshot.property_schema_index, snapshot.reward_package,
            snapshot.package_name_number, snapshot.reward_asset, snapshot.asset_name_number,
            snapshot.import_class_name, snapshot.import_class_package, snapshot.owner_exports,
        )
        wanted = (
            target.export_name, target.property_schema_index, target.expected_import_package,
            target.expected_package_name_number, target.expected_import_asset,
            target.expected_asset_name_number, target.expected_import_class_name,
            target.expected_import_class_package, (target.export_index,),
        )
        if actual != wanted or snapshot.reference_property != "BloodCode":
            raise BloodCodeGenerationError(
                f"BuddySettings.BloodCode import differs from its catalogued reward at {slot.slot_id}."
            )


# Validate fingerprints
def _validate_fingerprints(expected: Mapping[str, Mapping[str, str]], assets: Mapping[str, MaterializedAsset]) -> None:
    expected_by_package = {key.casefold(): value for key, value in expected.items()}
    for asset in assets.values():
        sidecars = expected_by_package.get(asset.virtual_path.casefold())
        if sidecars is None:
            raise BloodCodeGenerationError(f"Unexpected extracted Blood Code package {asset.virtual_path}.")
        actual = {path.suffix.casefold(): _sha256(path) for path in asset.files if path.suffix.casefold() in {".uasset", ".uexp"}}
        if actual != sidecars:
            raise BloodCodeGenerationError(f"Extracted Blood Code source fingerprint changed for {asset.virtual_path}.")


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


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BloodCodeGenerationError(f"UAssetAPI returned an invalid {label}.")
    return value


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
