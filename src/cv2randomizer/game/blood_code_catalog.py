"""Version-bound metadata for Blood Code randomization locations."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import application_data_path


CONTENT_CLASS_COUNTS = {"BASE": 56, "AFTER_ENDING": 13, "DLC": 10}
TOTAL_BLOOD_CODE_LOCATIONS = sum(CONTENT_CLASS_COUNTS.values())
_LOCATION_KINDS = frozenset({
    "gain_item", "proficiency_reference", "scenario_reward", "buddy_settings_reference",
})
SCENARIO_SCHEMA_PACKAGES = (
    "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Base/BP_SmallScenarioBase",
    "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Base/BP_SmallScenarioGainBloodCodeBase",
    "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Base/BP_SmallScenarioGainBoosterBase",
)
_SCENARIO_SUPER_CLASSES = frozenset({
    "BP_SmallScenarioBase_C",
    "BP_SmallScenarioGainBloodCodeBase_C",
    "BP_SmallScenarioGainBoosterBase_C",
})
_DEFAULT_SCENARIO_SUPER_CLASS = "BP_SmallScenarioGainBloodCodeBase_C"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTENT_CLASSES = frozenset((*CONTENT_CLASS_COUNTS, "UNKNOWN"))


class BloodCodeCatalogError(ValueError):
    """Raised when the Blood Code grant catalog is absent, stale, or unsafe."""


@dataclass(frozen=True, slots=True)
class BloodCodeReferenceTarget:
    virtual_package: str
    export_index: int
    export_name: str
    reference_property: str
    reference_index: int
    expected_array_count: int
    expected_package_name_value: str
    expected_package_name_number: int
    expected_asset_name_value: str
    expected_asset_name_number: int
    expected_super_class: str | None = None


@dataclass(frozen=True, slots=True)
class BloodCodeObjectReferenceTarget:
    virtual_package: str
    export_index: int
    export_name: str
    reference_property: str
    property_schema_index: int
    expected_import_package: str
    expected_import_asset: str
    expected_package_name_number: int
    expected_asset_name_number: int
    expected_import_class_name: str
    expected_import_class_package: str


@dataclass(frozen=True, slots=True)
class BloodCodeGrant:
    slot_id: str
    content_class: str
    virtual_package: str
    export_index: int
    export_name: str
    bulk_index: int | None
    reward_index: int | None
    original_blood_code_package: str
    original_blood_code_asset: str
    count: int | None
    b_notification: bool | None
    location_kind: str = "gain_item"
    reference_property: str | None = None
    reference_index: int | None = None
    expected_array_count: int | None = None
    display_name: str = ""
    expected_package_name_value: str | None = None
    expected_package_name_number: int | None = None
    expected_asset_name_value: str | None = None
    expected_asset_name_number: int | None = None
    mirrored_references: tuple[BloodCodeReferenceTarget, ...] = ()
    linked_route_references: tuple[BloodCodeReferenceTarget, ...] = ()
    linked_proficiency_references: tuple[BloodCodeReferenceTarget, ...] = ()
    object_reference: BloodCodeObjectReferenceTarget | None = None

    # Find physical reference targets
    @property
    def physical_reference_targets(self) -> tuple[BloodCodeReferenceTarget, ...]:
        if self.location_kind == "buddy_settings_reference":
            return (*self.linked_proficiency_references, *self.mirrored_references)
        if self.location_kind == "gain_item":
            return self.linked_route_references
        if self.location_kind not in {"proficiency_reference", "scenario_reward"}:
            return ()
        assert self.reference_property is not None
        assert self.reference_index is not None
        assert self.expected_array_count is not None
        assert self.expected_package_name_value is not None
        assert self.expected_package_name_number is not None
        assert self.expected_asset_name_value is not None
        assert self.expected_asset_name_number is not None
        return (
            BloodCodeReferenceTarget(
                self.virtual_package, self.export_index, self.export_name,
                self.reference_property, self.reference_index, self.expected_array_count,
                self.expected_package_name_value, self.expected_package_name_number,
                self.expected_asset_name_value, self.expected_asset_name_number,
                _DEFAULT_SCENARIO_SUPER_CLASS,
            ),
            *self.mirrored_references,
        )

    # Find physical object reference targets
    @property
    def physical_object_reference_targets(self) -> tuple[BloodCodeObjectReferenceTarget, ...]:
        if self.location_kind != "buddy_settings_reference":
            return ()
        if self.object_reference is None:
            raise AssertionError("BuddySettings reward location is missing its object reference target.")
        return (self.object_reference,)

    # List physical source packages
    @property
    def physical_source_packages(self) -> tuple[str, ...]:
        packages = [self.virtual_package]
        packages.extend(target.virtual_package for target in self.physical_reference_targets)
        packages.extend(target.virtual_package for target in self.physical_object_reference_targets)
        return tuple(dict.fromkeys(packages))

    # Sort key
    @property
    def sort_key(self) -> tuple[str, int, str, int, int]:
        return (
            self.virtual_package.casefold(), self.export_index, self.location_kind,
            -1 if self.bulk_index is None else self.bulk_index,
            self.reward_index if self.location_kind == "gain_item" and self.reward_index is not None
            else self.reference_index if self.reference_index is not None else -1,
        )


@dataclass(frozen=True, slots=True)
class BloodCodeCatalog:
    revision: str
    slots: tuple[BloodCodeGrant, ...]
    source_package_sha256: dict[str, dict[str, str]]

    # Index records by package
    def by_package(self) -> dict[str, tuple[BloodCodeGrant, ...]]:
        packages: dict[str, list[BloodCodeGrant]] = {}
        for slot in self.slots:
            physical_packages = slot.physical_source_packages
            for package in physical_packages:
                packages.setdefault(package, []).append(slot)
        return {package: tuple(slots) for package, slots in packages.items()}

    # List source packages
    @property
    def source_packages(self) -> frozenset[str]:
        has_scenario_targets = any(
            target.reference_property == "ArticleBulkInfo.Articles"
            for slot in self.slots for target in slot.physical_reference_targets
        )
        schemas = SCENARIO_SCHEMA_PACKAGES if has_scenario_targets else ()
        return frozenset((*self.by_package(), *schemas))


class BloodCodeCatalogStore:
    """Load and fingerprint-check the supported Blood Code location inventory."""

    # Initialize this object
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_path("blood_codes", "current")

    # Load and validate catalog data
    def load(self, revision: str) -> BloodCodeCatalog:
        if not isinstance(revision, str) or not revision.strip():
            raise BloodCodeCatalogError("Blood Code catalog revision must be a non-empty identifier.")
        manifest_path = self.data_dir / "manifest.json"
        catalog_path = self.data_dir / "blood_code_grants.json"
        try:
            manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = catalog_path.read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BloodCodeCatalogError(f"Could not read Blood Code catalog files: {error}") from error
        if not isinstance(manifest, dict) or manifest.get("schema") != "cv2-blood-code-catalog-v1":
            raise BloodCodeCatalogError("Blood Code catalog manifest has an unknown schema.")
        if manifest.get("catalog_revision") != revision:
            raise BloodCodeCatalogError(
                f"Blood Code catalog is bound to revision {manifest.get('catalog_revision')!r}, not {revision!r}."
            )
        files = manifest.get("files")
        metadata = files.get(catalog_path.name) if isinstance(files, dict) else None
        if not isinstance(metadata, dict) or type(metadata.get("count")) is not int or metadata["count"] < 1:
            raise BloodCodeCatalogError(
                f"Blood Code manifest does not declare the expected {TOTAL_BLOOD_CODE_LOCATIONS}-location catalog."
            )
        if not isinstance(metadata.get("sha256"), str) or hashlib.sha256(payload).hexdigest() != metadata["sha256"].casefold():
            raise BloodCodeCatalogError("Blood Code grant JSON hash does not match its manifest.")
        try:
            document: Any = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BloodCodeCatalogError(f"Could not parse Blood Code grant JSON: {error}") from error
        if not isinstance(document, dict) or document.get("schema") != "cv2-blood-code-grants-v1":
            raise BloodCodeCatalogError("Blood Code grant catalog has an unknown schema.")
        if document.get("catalog_revision") != revision:
            raise BloodCodeCatalogError("Blood Code grant catalog and manifest revisions differ.")
        source_hashes = manifest.get("source_package_sha256")
        if not isinstance(source_hashes, dict):
            raise BloodCodeCatalogError("Blood Code manifest has no source package fingerprints.")
        raw_slots = document.get("slots")
        if not isinstance(raw_slots, list):
            raise BloodCodeCatalogError("Blood Code catalog does not contain a slot list.")
        slots: list[BloodCodeGrant] = []
        seen: set[tuple[str, int, str, int, int]] = set()
        seen_blood_code_sources: set[str] = set()
        for raw in raw_slots:
            if not isinstance(raw, dict):
                raise BloodCodeCatalogError("Blood Code catalog contains a non-object slot.")
            try:
                location_kind = _string(raw.get("location_kind", "gain_item"), "location_kind")
                if location_kind not in _LOCATION_KINDS:
                    raise ValueError(f"unsupported location_kind {location_kind!r}")
                is_direct = location_kind == "gain_item"
                slot = BloodCodeGrant(
                    slot_id=_string(raw.get("slot_id"), "slot_id"),
                    content_class=_string(raw.get("content_class"), "content_class").upper(),
                    virtual_package=_package(raw.get("virtual_package")),
                    export_index=_integer(raw.get("export_index"), "export_index", minimum=1),
                    export_name=_string(raw.get("export_name"), "export_name"),
                    bulk_index=(
                        _integer(raw.get("bulk_index"), "bulk_index", minimum=0)
                        if is_direct else None
                    ),
                    reward_index=(
                        _integer(raw.get("reward_index"), "reward_index", minimum=0)
                        if is_direct else None
                    ),
                    original_blood_code_package=_package(raw.get("original_blood_code_package")),
                    original_blood_code_asset=_string(raw.get("original_blood_code_asset"), "original_blood_code_asset"),
                    count=(
                        _integer(raw.get("count"), "count", minimum=1)
                        if is_direct else None
                    ),
                    b_notification=(
                        _boolean(raw.get("b_notification"), "b_notification")
                        if is_direct else None
                    ),
                    location_kind=location_kind,
                    reference_property=(
                        _string(raw.get("reference_property"), "reference_property")
                        if not is_direct else None
                    ),
                    reference_index=(
                        _integer(raw.get("reference_index"), "reference_index", minimum=0)
                        if not is_direct else None
                    ),
                    expected_array_count=(
                        _integer(raw.get("expected_array_count"), "expected_array_count", minimum=1)
                        if location_kind in {"proficiency_reference", "scenario_reward"} else None
                    ),
                    display_name=str(raw.get("display_name", "")),
                    expected_package_name_value=(
                        _string(raw.get("expected_package_name_value"), "expected_package_name_value")
                        if raw.get("expected_package_name_value") is not None else None
                    ),
                    expected_package_name_number=(
                        _integer(raw.get("expected_package_name_number"), "expected_package_name_number", minimum=0)
                        if raw.get("expected_package_name_number") is not None else None
                    ),
                    expected_asset_name_value=(
                        _string(raw.get("expected_asset_name_value"), "expected_asset_name_value")
                        if raw.get("expected_asset_name_value") is not None else None
                    ),
                    expected_asset_name_number=(
                        _integer(raw.get("expected_asset_name_number"), "expected_asset_name_number", minimum=0)
                        if raw.get("expected_asset_name_number") is not None else None
                    ),
                    mirrored_references=tuple(
                        _reference_target(item, raw)
                        for item in _reference_list(raw.get("mirrored_references"))
                    ),
                    linked_route_references=tuple(
                        _reference_target(item, raw)
                        for item in _reference_list(raw.get("linked_route_references"))
                    ),
                    linked_proficiency_references=tuple(
                        _reference_target(item, raw)
                        for item in _reference_list(raw.get("linked_proficiency_references"))
                    ),
                    object_reference=(
                        _object_reference_target(raw.get("object_reference"), raw)
                        if raw.get("object_reference") is not None else None
                    ),
                )
            except ValueError as error:
                raise BloodCodeCatalogError(f"Invalid Blood Code slot: {error}") from error
            if slot.content_class not in _CONTENT_CLASSES:
                raise BloodCodeCatalogError(f"Invalid content_class {slot.content_class!r} for {slot.slot_id}.")
            if slot.location_kind == "gain_item" and "gainitem" not in slot.export_name.casefold().replace("_", ""):
                raise BloodCodeCatalogError(f"Location {slot.slot_id} is not a GainItem export.")
            if slot.location_kind == "scenario_reward" and (
                not slot.export_name.startswith("Default__BP_SmallScenario_")
                or slot.reference_property != "ArticleBulkInfo.Articles"
                or slot.expected_array_count != 1 or slot.reference_index != 0
                or slot.expected_package_name_value != slot.original_blood_code_package
                or slot.expected_asset_name_value != slot.original_blood_code_asset
                or slot.expected_package_name_number != 0 or slot.expected_asset_name_number != 0
                or not slot.display_name
            ):
                raise BloodCodeCatalogError(f"Invalid companion scenario reward {slot.slot_id}.")
            if slot.location_kind == "proficiency_reference" and (
                slot.export_name != "QSTask_BloodCodeAddProficiency_0"
                or slot.reference_property != "BloodCodeArticleDatas"
                or slot.reference_index is None
                or slot.expected_array_count is None
                or slot.count is not None
                or slot.b_notification is not None
                or not slot.display_name
                or slot.expected_package_name_value != slot.original_blood_code_package
                or slot.expected_package_name_number != 0
                or slot.expected_asset_name_value != slot.original_blood_code_asset
                or slot.expected_asset_name_number != 0
                or not slot.mirrored_references
                or slot.linked_proficiency_references
            ):
                raise BloodCodeCatalogError(f"Invalid BloodCodeArticleDatas location {slot.slot_id}.")
            if slot.location_kind == "buddy_settings_reference" and (
                slot.export_name != slot.virtual_package.rsplit("/", 1)[-1]
                or not slot.export_name.startswith("DA_BuddySettings_")
                or slot.reference_property != "BloodCode"
                or slot.reference_index != 0
                or slot.expected_array_count is not None
                or slot.count is not None
                or slot.b_notification is not None
                or not slot.display_name
                or slot.object_reference is None
                or slot.object_reference.virtual_package != slot.virtual_package
                or slot.object_reference.export_index != slot.export_index
                or slot.object_reference.export_name != slot.export_name
                or slot.object_reference.reference_property != "BloodCode"
                or slot.object_reference.expected_import_package != slot.original_blood_code_package
                or slot.object_reference.expected_import_asset != slot.original_blood_code_asset
                or slot.object_reference.expected_package_name_number != 0
                or slot.object_reference.expected_asset_name_number != 0
                or slot.object_reference.expected_import_class_name != "BloodCodeArticleData"
                or slot.object_reference.expected_import_class_package != "/Script/Extensions"
                or slot.object_reference.property_schema_index != 6
                or slot.expected_package_name_value != slot.original_blood_code_package
                or slot.expected_package_name_number != 0
                or slot.expected_asset_name_value != slot.original_blood_code_asset
                or slot.expected_asset_name_number != 0
            ):
                raise BloodCodeCatalogError(f"Invalid BuddySettings Blood Code unlock location {slot.slot_id}.")
            if slot.location_kind == "buddy_settings_reference" and any(
                target.reference_property != "ArticleBulkInfo.Articles"
                or target.reference_index != 0
                or target.expected_array_count != 1
                or target.expected_super_class not in _SCENARIO_SUPER_CLASSES
                or not target.export_name.startswith("Default__BP_SmallScenario_")
                for target in slot.mirrored_references
            ):
                raise BloodCodeCatalogError(f"Invalid mirrored companion reward for {slot.slot_id}.")
            if slot.location_kind == "gain_item" and slot.linked_route_references:
                expected_linked_routes = {
                    (
                        "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/BloodCode/Past_a20180/BP_SmallScenario_Lou_Disp_Past_a20180_01",
                        2,
                        "Default__BP_SmallScenario_Lou_Disp_Past_a20180_01_C",
                    ),
                    (
                        "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/BloodCode/Past_a20180/BP_SmallScenario_Lou_Disp_Past_a20180_01_Yadorigi",
                        2,
                        "Default__BP_SmallScenario_Lou_Disp_Past_a20180_01_Yadorigi_C",
                    ),
                }
                actual_linked_routes = {
                    (target.virtual_package, target.export_index, target.export_name)
                    for target in slot.linked_route_references
                }
                if (
                    slot.slot_id != "/Game/Quest/DLC/QST_MainEpisode_DLC_GainBC_LouDisp::export=15::bulk=0::article=0"
                    or slot.original_blood_code_asset != "DA_BloodCode_LouDisp_02"
                    or slot.original_blood_code_package != "/Game/Blueprint/Article/BloodCode/DLC/DA_BloodCode_LouDisp_02"
                    or actual_linked_routes != expected_linked_routes
                    or len(slot.linked_route_references) != len(expected_linked_routes)
                    or any(
                        target.reference_property != "ArticleBulkInfo.Articles"
                        or target.reference_index != 0
                        or target.expected_array_count != 1
                        or target.expected_super_class not in _SCENARIO_SUPER_CLASSES
                        for target in slot.linked_route_references
                    )
                ):
                    raise BloodCodeCatalogError(f"Invalid linked Lou Disp Blood Code routes for {slot.slot_id}.")
            if slot.location_kind != "gain_item" and slot.linked_route_references:
                raise BloodCodeCatalogError(
                    f"Only the linked Lou Disp GainItem reward can own alternate reward routes: {slot.slot_id}."
                )
            if slot.location_kind not in {"buddy_settings_reference"} and slot.linked_proficiency_references:
                raise BloodCodeCatalogError(
                    f"Only a BuddySettings reward can own linked proficiency references: {slot.slot_id}."
                )
            if slot.location_kind == "gain_item" and any((
                slot.reference_property is not None,
                slot.reference_index is not None,
                slot.expected_array_count is not None,
                bool(slot.mirrored_references),
                slot.object_reference is not None,
            )):
                raise BloodCodeCatalogError(f"GainItem location {slot.slot_id} has reference-only fields.")
            if slot.original_blood_code_package.rsplit("/", 1)[-1] != slot.original_blood_code_asset:
                raise BloodCodeCatalogError(f"Blood Code asset identity is malformed for {slot.slot_id}.")
            if slot.original_blood_code_package.casefold() in seen_blood_code_sources:
                raise BloodCodeCatalogError(
                    f"Blood Code article {slot.original_blood_code_package} has multiple logical reward slots."
                )
            seen_blood_code_sources.add(slot.original_blood_code_package.casefold())
            key = (
                slot.virtual_package.casefold(), slot.export_index, slot.location_kind,
                -1 if slot.bulk_index is None else slot.bulk_index,
                slot.reward_index if slot.location_kind == "gain_item" and slot.reward_index is not None
                else slot.reference_index if slot.reference_index is not None else -1,
            )
            if key in seen:
                raise BloodCodeCatalogError(f"Duplicate Blood Code destination slot {key}.")
            seen.add(key)
            physical_seen: set[tuple[str, int, str, int]] = set()
            for target in slot.physical_reference_targets:
                target_key = (target.virtual_package.casefold(), target.export_index, target.reference_property, target.reference_index)
                if target_key in physical_seen:
                    raise BloodCodeCatalogError(f"Duplicate physical reference target for {slot.slot_id}.")
                physical_seen.add(target_key)
                if slot.location_kind == "buddy_settings_reference":
                    if target in slot.linked_proficiency_references:
                        valid_reference = (
                            target.export_name == "QSTask_BloodCodeAddProficiency_0"
                            and target.reference_property == "BloodCodeArticleDatas"
                            and target.reference_index < target.expected_array_count
                        )
                    else:
                        valid_reference = (
                            target in slot.mirrored_references
                            and target.reference_property == "ArticleBulkInfo.Articles"
                            and target.reference_index == 0
                            and target.expected_array_count == 1
                            and target.expected_super_class in _SCENARIO_SUPER_CLASSES
                        )
                elif slot.location_kind == "gain_item":
                    valid_reference = (
                        target in slot.linked_route_references
                        and target.reference_property == "ArticleBulkInfo.Articles"
                        and target.reference_index == 0
                        and target.expected_array_count == 1
                        and target.expected_super_class in _SCENARIO_SUPER_CLASSES
                    )
                else:
                    valid_reference = (
                        (slot.location_kind != "proficiency_reference" or target.export_name == slot.export_name)
                        and target.reference_property == slot.reference_property
                        and target.reference_index == slot.reference_index
                        and target.expected_array_count == slot.expected_array_count
                    )
                if (
                    not valid_reference
                    or target.expected_package_name_value != slot.original_blood_code_package
                    or target.expected_package_name_number != 0
                    or target.expected_asset_name_value != slot.original_blood_code_asset
                    or target.expected_asset_name_number != 0
                ):
                    raise BloodCodeCatalogError(f"Mirrored reference differs from its logical slot {slot.slot_id}.")
            for target in slot.physical_object_reference_targets:
                if (
                    target.virtual_package != slot.virtual_package
                    or target.export_index != slot.export_index
                    or target.export_name != slot.export_name
                    or target.reference_property != "BloodCode"
                    or target.expected_import_package != slot.original_blood_code_package
                    or target.expected_import_asset != slot.original_blood_code_asset
                ):
                    raise BloodCodeCatalogError(f"BuddySettings import differs from its logical slot {slot.slot_id}.")
            slots.append(slot)
        counts = Counter(slot.content_class for slot in slots)
        if counts != Counter(manifest.get("content_class_counts", {})):
            raise BloodCodeCatalogError(f"Blood Code location distribution differs from V1: {dict(counts)}.")
        if metadata.get("count") != len(slots):
            raise BloodCodeCatalogError("Blood Code manifest count does not match the parsed slot count.")
        self._validate_source_hashes(slots, source_hashes)
        slots.sort(key=lambda slot: (slot.content_class, *slot.sort_key))
        return BloodCodeCatalog(revision, tuple(slots), source_hashes)
    # Validate source hashes
    @staticmethod
    def _validate_source_hashes(slots: list[BloodCodeGrant], source_hashes: dict[str, Any]) -> None:
        packages = {
            package.casefold()
            for slot in slots
            for package in slot.physical_source_packages
        }
        if any(
            target.reference_property == "ArticleBulkInfo.Articles"
            for slot in slots for target in slot.physical_reference_targets
        ):
            packages.update(package.casefold() for package in SCENARIO_SCHEMA_PACKAGES)
        if {package.casefold() for package in source_hashes} != packages:
            raise BloodCodeCatalogError("Blood Code source fingerprints do not describe exactly the catalogued packages.")
        for package, hashes in source_hashes.items():
            if not isinstance(hashes, dict) or set(hashes) != {".uasset", ".uexp"}:
                raise BloodCodeCatalogError(f"Source fingerprints are incomplete for {package}.")
            if any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in hashes.values()):
                raise BloodCodeCatalogError(f"Invalid source fingerprint for {package}.")


# Resolve catalogued base blood code package
def catalogued_base_blood_code_package_paths(catalog: BloodCodeCatalog) -> tuple[str, ...]:
    """Return owner and Article packages listed for BASE Blood Code locations.

    Runtime availability is decided per installation. Older builds may lack
    individual catalog entries, which are filtered out before planning.
    """
    required = {
        package
        for slot in catalog.slots if slot.content_class == "BASE"
        for package in (*slot.physical_source_packages, slot.original_blood_code_package)
    }
    return tuple(sorted(required, key=str.casefold))


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")
    return value


# Resolve a package asset path
def _package(value: Any) -> str:
    result = _string(value, "package")
    if not result.startswith("/Game/") or "\\" in result or result.endswith("/") or "." in result.rsplit("/", 1)[-1]:
        raise ValueError(f"invalid virtual package path {result!r}.")
    if any(part in {".", ".."} for part in result.split("/")):
        raise ValueError(f"unsafe virtual package path {result!r}.")
    return result


# Parse an integer value
def _integer(value: Any, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}.")
    return value


# Parse a boolean value
def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean.")
    return value


# Resolve reference list
def _reference_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("mirrored_references must be a list.")
    return value


# Resolve reference target
def _reference_target(raw: Any, slot: dict[str, Any]) -> BloodCodeReferenceTarget:
    if not isinstance(raw, dict):
        raise ValueError("mirrored reference target must be an object.")
    reference_property = _string(
        raw.get("reference_property", slot.get("reference_property")),
        "mirrored reference_property",
    )
    expected_super_class = raw.get("expected_super_class", slot.get("expected_super_class"))
    if expected_super_class is None and reference_property == "ArticleBulkInfo.Articles":
        expected_super_class = _DEFAULT_SCENARIO_SUPER_CLASS
    return BloodCodeReferenceTarget(
        virtual_package=_package(raw.get("virtual_package")),
        export_index=_integer(raw.get("export_index"), "mirrored export_index", minimum=1),
        export_name=_string(raw.get("export_name"), "mirrored export_name"),
        reference_property=reference_property,
        reference_index=_integer(raw.get("reference_index", slot.get("reference_index")), "mirrored reference_index", minimum=0),
        expected_array_count=_integer(raw.get("expected_array_count", slot.get("expected_array_count")), "mirrored expected_array_count", minimum=1),
        expected_package_name_value=_string(
            raw.get("expected_package_name_value", slot.get("expected_package_name_value", slot.get("original_blood_code_package"))),
            "mirrored expected_package_name_value",
        ),
        expected_package_name_number=_integer(
            raw.get("expected_package_name_number", slot.get("expected_package_name_number", 0)),
            "mirrored expected_package_name_number",
            minimum=0,
        ),
        expected_asset_name_value=_string(
            raw.get("expected_asset_name_value", slot.get("expected_asset_name_value", slot.get("original_blood_code_asset"))),
            "mirrored expected_asset_name_value",
        ),
        expected_asset_name_number=_integer(
            raw.get("expected_asset_name_number", slot.get("expected_asset_name_number", 0)),
            "mirrored expected_asset_name_number",
            minimum=0,
        ),
        expected_super_class=(
            _string(expected_super_class, "mirrored expected_super_class")
            if expected_super_class is not None else None
        ),
    )


# Parse object reference target
def _object_reference_target(raw: Any, slot: dict[str, Any]) -> BloodCodeObjectReferenceTarget:
    if not isinstance(raw, dict):
        raise ValueError("object_reference must be an object.")
    return BloodCodeObjectReferenceTarget(
        virtual_package=_package(raw.get("virtual_package", slot.get("virtual_package"))),
        export_index=_integer(raw.get("export_index", slot.get("export_index")), "object reference export_index", minimum=1),
        export_name=_string(raw.get("export_name", slot.get("export_name")), "object reference export_name"),
        reference_property=_string(raw.get("reference_property", slot.get("reference_property")), "object reference property"),
        property_schema_index=_integer(raw.get("property_schema_index"), "object reference property_schema_index", minimum=0),
        expected_import_package=_package(raw.get("expected_import_package", slot.get("original_blood_code_package"))),
        expected_import_asset=_string(raw.get("expected_import_asset", slot.get("original_blood_code_asset")), "object reference import asset"),
        expected_package_name_number=_integer(raw.get("expected_package_name_number"), "object reference package FName number", minimum=0),
        expected_asset_name_number=_integer(raw.get("expected_asset_name_number"), "object reference asset FName number", minimum=0),
        expected_import_class_name=_string(raw.get("expected_import_class_name"), "object reference import class"),
        expected_import_class_package=_string(raw.get("expected_import_class_package"), "object reference import class package"),
    )
