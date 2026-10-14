"""Fail-closed in-place ArticleDataPath edits for Extract-validated map layouts."""

from __future__ import annotations

import shutil
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .asset_extraction import ExtractedAsset
from .fname_identity import SoftObjectPathIdentity
from .world_loot_serialization import (
    WorldLootPackagePatchResult,
    WorldLootPatchResult,
    WorldLootTuplePatch,
)


@dataclass(frozen=True, slots=True)
class BinaryFieldEdit:
    export_index: int
    reward_index: int
    export_name: str
    offset: int
    expected: bytes
    replacement: bytes


@dataclass(frozen=True, slots=True)
class BinaryPackagePlan:
    package_path: str
    source: ExtractedAsset
    edits: tuple[BinaryFieldEdit, ...]
    patches: tuple[WorldLootTuplePatch, ...]


# Explain binary name map fallback reason
def binary_name_map_fallback_reason(
    package_path: str,
    descriptor: Any,
    patches: tuple[WorldLootTuplePatch, ...],
) -> str | None:
    """Reject packages needing new NameMap entries before reading cached bytes.

    This inexpensive descriptor-only pass avoids an additional full read/hash of
    every map when the replacement names are already known to be unavailable.
    It never makes a package eligible; packages that pass still require exact
    expected-byte and layout validation in :func:`preflight_binary_package`.
    """
    if not isinstance(descriptor, dict) or descriptor.get("version") != 3:
        return "no Extract-time binary descriptor"
    descriptor_package = descriptor.get("package_path")
    if not isinstance(descriptor_package, str) or descriptor_package.casefold() != package_path.casefold():
        return "descriptor package identity differs"
    fields = descriptor.get("fields")
    name_indices = descriptor.get("name_map_indices")
    if not isinstance(fields, list) or not isinstance(name_indices, dict):
        return "descriptor fields or NameMap index are invalid"
    by_slot: dict[tuple[int, int], dict[str, Any]] = {}
    for field in fields:
        if not isinstance(field, dict):
            return "descriptor contains an invalid field"
        slot = (field.get("export_index"), field.get("reward_index"))
        if slot in by_slot:
            return "descriptor repeats an export/reward slot"
        by_slot[slot] = field
    for patch in patches:
        field = by_slot.get((patch.export_index, patch.reward_index))
        if field is None or field.get("export_name") != patch.export_name:
            return f"descriptor has no matching ArticleDataPath at {patch.export_name}"
        if (
            patch.expected_original.count != patch.assigned.count
            or patch.expected_original.b_notification != patch.assigned.b_notification
        ):
            return "assignment would change non-path reward metadata"
        expected_identity = patch.expected_original.soft_object_path_identity
        assigned_identity = patch.assigned.soft_object_path_identity
        if expected_identity is None or assigned_identity is None:
            return "source or assigned ArticleDataPath has no exact FName identity"
        if field.get("expected_identity") != _identity_to_json(expected_identity):
            return "expected FName identity differs from Extract descriptor"
        if (
            _name_index(name_indices, assigned_identity.package_name.value) is None
            or _name_index(name_indices, assigned_identity.asset_name.value) is None
        ):
            return "assigned ArticleDataPath requires a NameMap entry"
    return None


# List preflight binary package
def preflight_binary_package(
    package_path: str,
    source: ExtractedAsset,
    descriptor: Any,
    patches: tuple[WorldLootTuplePatch, ...],
) -> tuple[BinaryPackagePlan | None, str | None]:
    """Prove every change in one package is an exact fixed-width FName edit.

    Returning a reason means the caller must route the *whole package* through
    the structural serializer. This function never writes to source or staging.
    """
    if not isinstance(descriptor, dict) or descriptor.get("version") != 3:
        return None, "no Extract-time binary descriptor"
    descriptor_package = descriptor.get("package_path")
    if not isinstance(descriptor_package, str) or descriptor_package.casefold() != package_path.casefold():
        return None, "descriptor package identity differs"
    try:
        primary = source.primary_file.resolve(strict=True)
        uexp = primary.with_suffix(".uexp").resolve(strict=True)
        header_size, sidecar_size = primary.stat().st_size, uexp.stat().st_size
    except OSError:
        return None, "required package header or .uexp is absent"
    if descriptor.get("header_size") != header_size or descriptor.get("uexp_size") != sidecar_size:
        return None, "cached package size differs from Extract descriptor"
    package_hashes = descriptor.get("package_file_hashes")
    actual_suffixes = {file.suffix.casefold() for file in source.files}
    if not isinstance(package_hashes, dict) or set(package_hashes) != actual_suffixes:
        return None, "cached package sidecar set differs from Extract descriptor"
    if any(not source_file.is_file() for source_file in source.files):
        return None, "cached package sidecar is absent"
    fields = descriptor.get("fields")
    name_indices = descriptor.get("name_map_indices")
    if not isinstance(fields, list) or not isinstance(name_indices, dict):
        return None, "descriptor fields or NameMap index are invalid"
    by_slot: dict[tuple[int, int], dict[str, Any]] = {}
    for field in fields:
        if not isinstance(field, dict):
            return None, "descriptor contains an invalid field"
        slot = (field.get("export_index"), field.get("reward_index"))
        if slot in by_slot:
            return None, "descriptor repeats an export/reward slot"
        by_slot[slot] = field

    edits: list[BinaryFieldEdit] = []
    for patch in patches:
        field = by_slot.get((patch.export_index, patch.reward_index))
        if field is None or field.get("export_name") != patch.export_name:
            return None, f"descriptor has no matching ArticleDataPath at {patch.export_name}"
        if (
            patch.expected_original.count != patch.assigned.count
            or patch.expected_original.b_notification != patch.assigned.b_notification
        ):
            return None, "assignment would change non-path reward metadata"
        expected_identity = patch.expected_original.soft_object_path_identity
        assigned_identity = patch.assigned.soft_object_path_identity
        if expected_identity is None or assigned_identity is None:
            return None, "source or assigned ArticleDataPath has no exact FName identity"
        if field.get("expected_identity") != _identity_to_json(expected_identity):
            return None, "expected FName identity differs from Extract descriptor"
        package_index = _name_index(name_indices, assigned_identity.package_name.value)
        asset_index = _name_index(name_indices, assigned_identity.asset_name.value)
        if package_index is None or asset_index is None:
            return None, "assigned ArticleDataPath requires a NameMap entry"
        try:
            replacement = struct.pack(
                "<iiii", package_index, assigned_identity.package_name.number,
                asset_index, assigned_identity.asset_name.number,
            )
            expected = bytes.fromhex(field["expected_bytes_hex"])
            offset = field["offset"]
        except (KeyError, TypeError, ValueError, struct.error):
            return None, "descriptor contains malformed FName bytes or offset"
        if len(expected) != 16 or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return None, "descriptor ArticleDataPath is not a valid 16-byte field"
        if offset + len(expected) > sidecar_size:
            return None, "descriptor ArticleDataPath falls outside the .uexp sidecar"
        edits.append(BinaryFieldEdit(
            patch.export_index, patch.reward_index, patch.export_name,
            offset, expected, replacement,
        ))

    ordered = sorted(edits, key=lambda edit: edit.offset)
    for previous, current in zip(ordered, ordered[1:]):
        if previous.offset + len(previous.expected) > current.offset:
            return None, "binary ArticleDataPath ranges overlap"
    try:
        with uexp.open("rb") as stream:
            for edit in edits:
                stream.seek(edit.offset)
                if stream.read(16) != edit.expected:
                    return None, f"expected vanilla bytes differ at .uexp offset {edit.offset}"
    except OSError:
        return None, "could not validate expected bytes in the cached .uexp"
    return BinaryPackagePlan(package_path, source, tuple(ordered), patches), None


# Write binary package
def write_binary_package(
    plan: BinaryPackagePlan,
    *,
    work_root: Path,
    output_content_root: Path,
) -> WorldLootPackagePatchResult:
    """Copy a vanilla package once, patch exact ranges, and atomically promote."""
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    if not _is_within(output_root, root) or output_root == root:
        raise ValueError("Binary World Loot output must be a separate tree inside work_root.")
    relative = PurePosixPath(*plan.package_path.split("/")[2:])
    destination_header = output_root.joinpath(*relative.parts).with_suffix(plan.source.primary_file.suffix)
    if not _is_within(destination_header, output_root):
        raise ValueError("Binary World Loot package path escaped output Content.")
    source_header = plan.source.primary_file.resolve(strict=True)
    source_uexp = source_header.with_suffix(".uexp")
    source_files = tuple(path.resolve(strict=True) for path in plan.source.files)
    final_files = tuple(destination_header.with_suffix(path.suffix) for path in source_files)
    if any(not _is_within(path, output_root) for path in final_files):
        raise ValueError("Binary World Loot sidecar path escaped output Content.")
    if any(path.exists() or path.is_symlink() for path in final_files):
        raise FileExistsError(f"Refusing to overwrite staged package file {destination_header.name}.")
    if source_uexp not in source_files:
        raise ValueError("Binary World Loot source package has no .uexp sidecar.")
    temporary_files: list[tuple[Path, Path, Path]] = []
    moved: list[Path] = []
    try:
        destination_header.parent.mkdir(parents=True, exist_ok=True)
        edit_by_offset = {edit.offset: edit for edit in plan.edits}
        source_by_suffix = {path.suffix.casefold(): path for path in source_files}
        if source_by_suffix.get(".umap", source_by_suffix.get(".uasset")) != source_header:
            raise ValueError("Binary World Loot source header is missing from its package inventory.")
        for source_file, final_file in zip(source_files, final_files, strict=True):
            final_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = final_file.with_name(f".{final_file.name}.{uuid.uuid4().hex}.cv2-tmp")
            temporary_files.append((source_file, temporary, final_file))
            shutil.copyfile(source_file, temporary)
            if temporary.stat().st_size != source_file.stat().st_size:
                raise ValueError(f"Copied package sidecar has an unexpected size: {source_file.name}.")
            if source_file.suffix.casefold() == ".uexp":
                with source_file.open("rb") as original, temporary.open("r+b") as staged:
                    for edit in plan.edits:
                        original.seek(edit.offset)
                        if original.read(16) != edit.expected:
                            raise ValueError(f"Expected bytes changed before patch at .uexp offset {edit.offset}.")
                        staged.seek(edit.offset)
                        if staged.read(16) != edit.expected:
                            raise ValueError(f"Copied expected bytes differ at .uexp offset {edit.offset}.")
                        staged.seek(edit.offset)
                        staged.write(edit.replacement)
                        staged.flush()
                        staged.seek(edit.offset)
                        if staged.read(16) != edit.replacement:
                            raise ValueError(f"Binary replacement failed read-back at .uexp offset {edit.offset}.")
                if temporary.stat().st_size != source_file.stat().st_size:
                    raise ValueError("Binary patch unexpectedly changed the .uexp size.")
            temporary.replace(final_file)
            moved.append(final_file)
        if destination_header not in final_files:
            raise ValueError("Binary World Loot output omitted its package header.")
        results = tuple(
            WorldLootPatchResult(
                patch.export_name, patch.export_index, patch.reward_index,
                "", patch.expected_original, patch.assigned, destination_header,
            )
            for patch in plan.patches
        )
        return WorldLootPackagePatchResult(source_header, destination_header, results)
    except Exception:
        for path in moved:
            path.unlink(missing_ok=True)
        raise
    finally:
        for _source, temporary, _final in temporary_files:
            temporary.unlink(missing_ok=True)


# Resolve identity to JSON
def _identity_to_json(identity: SoftObjectPathIdentity) -> dict[str, Any]:
    return {
        "package_name": {"value": identity.package_name.value, "number": identity.package_name.number},
        "asset_name": {"value": identity.asset_name.value, "number": identity.asset_name.number},
    }


# Resolve name index
def _name_index(indices: dict[str, Any], literal: str) -> int | None:
    value = indices.get(literal)
    if type(value) is int and value >= 0:
        return value
    return None


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
