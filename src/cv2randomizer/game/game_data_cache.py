"""Persistent vanilla source cache and cache-only asset access."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from cv2randomizer.core.workspace import CacheManager
from cv2randomizer.core.resources import (
    ApplicationResourceError,
    cv2_usmap_sha256,
    resolve_uassetapi_helper,
)
from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    ExtractedAsset,
    IoStoreAssetExtractor,
    _normalize_asset_path,
    archive_source_fingerprint,
)
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.catalogs import is_world_map_package
from cv2randomizer.game.locator import check_installation
from cv2randomizer.game.versioning import VersionDetection

CACHE_FORMAT_VERSION = 5
CACHE_INFO_FILENAME = "cache_info.json"
SOURCE_REGISTRY_REVISION = "catalogued-sources-v27-quest-world-loot-grants"
WORLD_LOOT_DESCRIPTOR_REVISION = 13
WORLD_LOOT_PREPARATION_REVISION = 19
RAW_EXTRACTION_REVISION = 1
PREPARED_CACHE_REVISION = 1
_LEGACY_CACHE_ENTRY_NAME = re.compile(r"[0-9a-f]{64}\Z")


# Check link or junction
def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    """Return whether a canonical path stays inside its cache-owned root."""
    try:
        path.expanduser().resolve(strict=False).relative_to(root.expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


# Remove path without following links
def _remove_path_without_following_links(path: Path) -> None:
    """Remove a cache child recursively, unlinking reparse points without traversing them."""
    if path.is_symlink():
        path.unlink()
        return
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        path.rmdir()
        return
    if path.is_dir():
        for child in tuple(path.iterdir()):
            _remove_path_without_following_links(child)
        path.rmdir()
    else:
        path.unlink()


# Link files in the raw cache
def _link_raw_cache_assets(source_entry: Path, destination_entry: Path) -> tuple[int, int]:
    """Seed a staging entry with immutable raw assets using same-volume hardlinks."""
    source_root = source_entry / "assets"
    destination_root = destination_entry / "assets"
    if _is_link_or_junction(source_entry) or _is_link_or_junction(source_root):
        raise AssetExtractionError("Refusing to reuse a raw cache through a link or junction.")
    try:
        source_root.resolve(strict=True).relative_to(source_entry.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise AssetExtractionError("The raw cache assets directory escaped its cache entry.") from error
    if not source_root.is_dir() or destination_root.exists():
        raise AssetExtractionError("Raw cache reuse requires one existing source and an empty destination.")

    linked_files = 0
    linked_bytes = 0
    destination_root.mkdir(parents=True)
    for current, directories, filenames in os.walk(source_root, followlinks=False):
        source_directory = Path(current)
        relative_directory = source_directory.relative_to(source_root)
        target_directory = destination_root / relative_directory
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in tuple(directories):
            source_child = source_directory / name
            if _is_link_or_junction(source_child):
                raise AssetExtractionError(f"Refusing to reuse a raw cache containing a linked directory: {source_child}.")
        for name in filenames:
            source_file = source_directory / name
            if _is_link_or_junction(source_file) or not source_file.is_file():
                raise AssetExtractionError(f"Refusing to reuse a raw cache containing a linked or irregular file: {source_file}.")
            target_file = target_directory / name
            if target_file.exists() or _is_link_or_junction(target_file):
                raise AssetExtractionError(f"Raw cache staging destination already exists: {target_file}.")
            try:
                os.link(source_file, target_file)
            except OSError as error:
                raise AssetExtractionError(f"Could not safely hardlink immutable raw cache data: {error}.") from error
            linked_files += 1
            linked_bytes += source_file.stat().st_size
    if linked_files == 0:
        raise AssetExtractionError("The reusable raw cache contains no files.")
    return linked_files, linked_bytes


# Hash the raw cache identity
def _raw_cache_identity_fingerprint(identity: dict[str, str]) -> str:
    material = "\0".join((
        identity["game_version"], identity["game_archive_sha256"],
        identity.get("raw_extraction_revision", str(RAW_EXTRACTION_REVISION)),
    ))
    return hashlib.sha256(material.encode("ascii")).hexdigest()


# Hash the prepared cache identity
def _prepared_cache_identity_fingerprint(identity: dict[str, str]) -> str:
    material = "\0".join((
        _raw_cache_identity_fingerprint(identity), identity["usmap_sha256"],
        identity["catalog_revision"], identity["source_registry_revision"],
        identity["world_loot_descriptor_revision"], identity["world_loot_preparation_revision"],
        identity["uassetapi_helper_sha256"],
        identity.get("prepared_cache_revision", str(PREPARED_CACHE_REVISION)),
    ))
    return hashlib.sha256(material.encode("ascii")).hexdigest()


# Check a legacy cache entry
def _legacy_entry_matches_key(directory_name: str, metadata: Any) -> bool:
    """Recognize only cache folders whose old content-addressed key verifies."""
    if not isinstance(metadata, dict):
        return False
    game_version = metadata.get("game_version")
    data_revision = metadata.get("catalog_revision")
    identity_fields = (
        "game_archive_sha256", "usmap_sha256", "catalog_revision",
        "source_registry_revision", "world_loot_descriptor_revision",
        "world_loot_preparation_revision", "uassetapi_helper_sha256",
    )
    identity = {key: metadata.get(key) for key in identity_fields}
    if (
        not isinstance(game_version, str) or not game_version
        or not isinstance(data_revision, str) or not data_revision
        or any(not isinstance(value, str) or not value for value in identity.values())
    ):
        return False
    source_hash = _cache_source_fingerprint(identity)
    encoded = json.dumps(
        (game_version, source_hash.casefold(), data_revision),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest() == directory_name


class GameDataCacheState(StrEnum):
    NOT_EXTRACTED = "NOT_EXTRACTED"
    READY = "READY"
    OUTDATED = "OUTDATED"
    PREPARED_OUTDATED = "PREPARED_OUTDATED"
    INVALID = "INVALID"
    EXTRACTING = "EXTRACTING"


@dataclass(frozen=True, slots=True)
class GameDataCacheSnapshot:
    state: GameDataCacheState
    entry_path: Path | None
    reason: str
    game_version: str | None = None
    catalog_revision: str | None = None
    available_sources: tuple[str, ...] = ()
    unavailable_optional_sources: tuple[str, ...] = ()
    invalid_sources: tuple[tuple[str, str], ...] = ()
    required_sources: tuple[str, ...] = ()
    inventory: PackageInventory | None = None
    metadata: dict[str, Any] | None = None

    # Read ready
    @property
    def ready(self) -> bool:
        return self.state is GameDataCacheState.READY


class GameDataCacheStore:
    """Manage one atomic vanilla extraction cache for the selected game."""

    # Initialize this object
    def __init__(self, cache: CacheManager | None = None) -> None:
        self.cache = cache or CacheManager(single_entry=True)
        # The game-data cache is deliberately a single replaceable READY folder.
        # Generic asset extraction caches remain content-addressed by default.
        self.cache.single_entry = True
        self.root = self.cache.root.expanduser().resolve()

    # Inspect prepared game data
    def inspect(self, game_root: Path, detection: VersionDetection) -> GameDataCacheSnapshot:
        """Validate the reusable cache against installed archive contents and manifests."""
        if not detection.usable_for_local_validation or not detection.version_id or not detection.catalog_revision:
            return GameDataCacheSnapshot(
                GameDataCacheState.INVALID, None,
                detection.reason or "The selected game build is not supported.",
            )
        try:
            identity = self._identity(game_root, detection)
            entry = self.cache.entry_path(
                game_version=detection.version_id,
                source_hash=_cache_source_fingerprint(identity),
                data_revision=detection.catalog_revision,
                create=False,
            )
        except (AssetExtractionError, ApplicationResourceError, OSError, ValueError) as error:
            return GameDataCacheSnapshot(GameDataCacheState.INVALID, None, str(error))
        if not entry.is_dir():
            try:
                self._adopt_legacy_entry(entry, identity)
            except (OSError, ValueError, AssetExtractionError):
                # A legacy cache that cannot be validated remains untouched; the
                # caller can use the normal Extract action to build READY anew.
                pass
        if not entry.is_dir():
            if entry.exists() or _is_link_or_junction(entry):
                return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Extracted cache entry is not a regular directory.")
            return GameDataCacheSnapshot(
                GameDataCacheState.NOT_EXTRACTED, entry,
                "Game data must be extracted before generating a randomizer.",
                detection.version_id, detection.catalog_revision,
            )
        if _is_link_or_junction(entry):
            return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Extracted cache entry must not be a link or junction.")
        try:
            metadata: Any = json.loads((entry / CACHE_INFO_FILENAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, f"Cache metadata is unreadable: {error}")
        cache_format = metadata.get("cache_format_version") if isinstance(metadata, dict) else None
        if (
            not isinstance(metadata, dict)
            or not isinstance(cache_format, int) or isinstance(cache_format, bool)
            or cache_format < 1 or cache_format > CACHE_FORMAT_VERSION
        ):
            return GameDataCacheSnapshot(GameDataCacheState.OUTDATED, entry, "Extracted game data uses an obsolete cache format.")
        metadata = dict(metadata)
        if metadata.get("game_archive_sha256") != identity.get("game_archive_sha256"):
            return GameDataCacheSnapshot(
                GameDataCacheState.OUTDATED, entry,
                "Installed game archives changed since extraction; re-extract game data to refresh the cache.",
                detection.version_id, detection.catalog_revision,
            )
        cached_raw_identity = metadata.get("raw_cache_identity_sha256")
        if cached_raw_identity is None:
            cached_raw_identity = _raw_cache_identity_fingerprint({
                "game_version": str(metadata.get("game_version", "")),
                "game_archive_sha256": str(metadata.get("game_archive_sha256", "")),
                "raw_extraction_revision": str(metadata.get("raw_extraction_revision", RAW_EXTRACTION_REVISION)),
            })
        if cached_raw_identity != identity.get("raw_cache_identity_sha256"):
            return GameDataCacheSnapshot(
                GameDataCacheState.OUTDATED, entry,
                "Raw cache identity changed; re-extract game data to refresh the cache.",
                detection.version_id, detection.catalog_revision,
            )
        derived_identity_fields = (
            "game_version", "catalog_revision", "usmap_sha256",
            "source_registry_revision", "world_loot_descriptor_revision",
            "world_loot_preparation_revision", "uassetapi_helper_sha256",
        )
        raw_extraction_current = (
            metadata.get("raw_extraction_revision", str(RAW_EXTRACTION_REVISION))
            == str(RAW_EXTRACTION_REVISION)
        )
        prepared_metadata_current = all(
            metadata.get(key) == identity.get(key) for key in derived_identity_fields
        ) and (
            metadata.get("prepared_cache_revision", str(PREPARED_CACHE_REVISION))
            == str(PREPARED_CACHE_REVISION)
            and metadata.get("prepared_cache_identity_sha256")
            in (None, identity.get("prepared_cache_identity_sha256"))
        )
        prepared_cache_outdated = not prepared_metadata_current
        if not raw_extraction_current:
            return GameDataCacheSnapshot(
                GameDataCacheState.OUTDATED, entry,
                "Raw extracted assets use an obsolete extraction format; re-extract game data.",
                detection.version_id, detection.catalog_revision,
            )
        world_loot_index = metadata.get("world_loot_dependency_index")
        if (
            metadata.get("world_loot_preparation_revision") != str(WORLD_LOOT_PREPARATION_REVISION)
            and isinstance(world_loot_index, dict)
            and (world_loot_index.get("maps") or world_loot_index.get("world_loot_binary_patch_packages"))
        ):
            prepared_cache_outdated = True
        if world_loot_index is not None and not prepared_metadata_current:
            # The extracted source assets still belong to the same game archives,
            # but app-derived descriptors may encode old catalog/helper logic.
            # Ignore them and let Generate take its structural preparation path.
            metadata.pop("world_loot_dependency_index", None)
            world_loot_index = None
        if world_loot_index is not None and (
            not isinstance(world_loot_index, dict)
            or world_loot_index.get("schema_version") != 1
            or world_loot_index.get("game_version") != detection.version_id
            or world_loot_index.get("catalog_revision") != detection.catalog_revision
            or not isinstance(world_loot_index.get("maps"), dict)
            or not isinstance(world_loot_index.get("blueprint_cdo_defaults"), dict)
            or not isinstance(world_loot_index.get("world_loot_location_descriptors"), dict)
            or not isinstance(world_loot_index.get("renewable_class_profiles"), dict)
        ):
            return GameDataCacheSnapshot(
                GameDataCacheState.INVALID, entry,
                "Cached World Loot dependency index is invalid or belongs to another cache identity.",
            )
        if isinstance(world_loot_index, dict):
            expected_profile = detection.world_loot_compatibility_profile
            if world_loot_index.get("compatibility_profile") != expected_profile:
                return GameDataCacheSnapshot(
                    GameDataCacheState.OUTDATED, entry,
                    "Cached World Loot data uses a different version profile; extract game data again.",
                    detection.version_id, detection.catalog_revision,
                )
            rebindings = world_loot_index.get("world_loot_export_rebindings", [])
            if not isinstance(rebindings, list):
                return GameDataCacheSnapshot(
                    GameDataCacheState.INVALID, entry,
                    "Cached World Loot export rebindings are malformed.",
                )
            rebound_sources: set[tuple[str, int]] = set()
            rebound_targets: set[tuple[str, int]] = set()
            for row in rebindings:
                if not isinstance(row, dict):
                    return GameDataCacheSnapshot(
                        GameDataCacheState.INVALID, entry,
                        "Cached World Loot export rebinding is malformed.",
                    )
                package = row.get("package_path")
                source_index = row.get("catalog_export_index")
                target_index = row.get("resolved_export_index")
                export_name = row.get("export_name")
                class_package = row.get("class_package")
                if (
                    not isinstance(package, str) or not package.startswith("/Game/Map/")
                    or not isinstance(source_index, int) or isinstance(source_index, bool) or source_index < 1
                    or not isinstance(target_index, int) or isinstance(target_index, bool) or target_index < 1
                    or source_index == target_index
                    or not isinstance(export_name, str) or not export_name
                    or not isinstance(class_package, str) or not class_package.startswith("/Game/")
                ):
                    return GameDataCacheSnapshot(
                        GameDataCacheState.INVALID, entry,
                        "Cached World Loot export rebinding has an invalid identity.",
                    )
                source_key = (package.casefold(), source_index)
                target_key = (package.casefold(), target_index)
                if source_key in rebound_sources or target_key in rebound_targets:
                    return GameDataCacheSnapshot(
                        GameDataCacheState.INVALID, entry,
                        "Cached World Loot export rebindings contain duplicate slots.",
                    )
                rebound_sources.add(source_key)
                rebound_targets.add(target_key)
        if metadata.get("game_archive_sha256") != identity.get("game_archive_sha256"):
            return GameDataCacheSnapshot(
                GameDataCacheState.OUTDATED, entry,
                "Installed game archives changed since extraction; re-extract game data to refresh the cache.",
            )
        required = metadata.get("required_sources")
        available = metadata.get("extracted_package_inventory")
        optional_missing = metadata.get("unavailable_optional_sources")
        source_registry = metadata.get("source_registry")
        invalid_sources = metadata.get("invalid_sources", [])
        if (
            not isinstance(required, list) or any(not isinstance(value, str) for value in required)
            or not isinstance(available, list) or any(not isinstance(value, str) for value in available)
            or not isinstance(optional_missing, list) or any(not isinstance(value, str) for value in optional_missing)
            or not isinstance(invalid_sources, list)
            or not available
        ):
            return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cache metadata is incomplete.")
        # Catalogued item sources are optional across game builds and DLC
        # inventories. Only the version definition's core packages invalidate
        # the cache when absent; missing rewards are skipped during Generate.
        expected_required_values = set(detection.required_asset_paths)
        cache_inventory = PackageInventory.create(available)
        missing_current_required = sorted(
            (value for value in expected_required_values if not cache_inventory.contains(value)),
            key=str.casefold,
        )
        if missing_current_required:
            return GameDataCacheSnapshot(
                GameDataCacheState.OUTDATED, entry,
                "The current randomizer requires source packages not present in this cache: "
                + ", ".join(missing_current_required) + ". Re-extract game data to add them.",
                detection.version_id, detection.catalog_revision,
            )
        effective_required = tuple(sorted(expected_required_values, key=str.casefold))
        required = list(effective_required)
        metadata["required_sources"] = required
        if (
            metadata.get("world_loot_preparation_revision") == str(WORLD_LOOT_PREPARATION_REVISION)
            and isinstance(world_loot_index, dict)
            and bool(world_loot_index.get("maps"))
        ):
            binary_packages = (
                world_loot_index.get("world_loot_binary_patch_packages")
                if isinstance(world_loot_index, dict) else None
            )
            if (
                not isinstance(binary_packages, dict)
                or world_loot_index.get("binary_patch_version") != 3
            ):
                return GameDataCacheSnapshot(
                    GameDataCacheState.OUTDATED, entry,
                    "Extracted game data has no prepared binary World Loot patch descriptors; extract game data again.",
                    detection.version_id, detection.catalog_revision,
                )
            for package_key, descriptor in binary_packages.items():
                if not _valid_world_loot_binary_descriptor(package_key, descriptor):
                    return GameDataCacheSnapshot(
                        GameDataCacheState.INVALID, entry,
                        "Cached World Loot binary patch descriptor is malformed.",
                    )
                if not _validate_prepared_world_asset(entry, descriptor, verify_hash=False):
                    return GameDataCacheSnapshot(
                        GameDataCacheState.INVALID, entry,
                        "Cached prepared World Loot package is missing or failed integrity validation.",
                    )
            healing_maps = world_loot_index.get("healing_prepared_maps")
            healing_clones = world_loot_index.get("healing_prepared_clones")
            if not isinstance(healing_maps, dict) or not isinstance(healing_clones, dict):
                return GameDataCacheSnapshot(
                    GameDataCacheState.OUTDATED, entry,
                    "Extracted game data has no prepared Healing templates; extract game data again.",
                    detection.version_id, detection.catalog_revision,
                )
            for package_key, descriptor in healing_maps.items():
                if (
                    not _valid_world_loot_binary_descriptor(package_key, descriptor)
                    or not _validate_prepared_world_asset(entry, descriptor, verify_hash=False)
                ):
                    return GameDataCacheSnapshot(
                        GameDataCacheState.INVALID, entry,
                        "Cached prepared Healing map is missing or failed integrity validation.",
                    )
            for package_key, descriptor in healing_clones.items():
                if (
                    not _valid_healing_binary_descriptor(package_key, descriptor)
                    or not _validate_prepared_world_asset(entry, descriptor, verify_hash=False)
                ):
                    return GameDataCacheSnapshot(
                        GameDataCacheState.INVALID, entry,
                        "Cached prepared Healing Blueprint clone is missing or failed integrity validation.",
                    )
        location_descriptors = world_loot_index.get("world_loot_location_descriptors", {}) if isinstance(world_loot_index, dict) else {}
        map_descriptors = world_loot_index.get("maps", {}) if isinstance(world_loot_index, dict) else {}
        for key, descriptor in location_descriptors.items():
            if not isinstance(key, str) or not isinstance(descriptor, dict):
                return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot location descriptor is invalid.")
            package_path = descriptor.get("package_path")
            export_index = descriptor.get("export_index")
            mirror_package = descriptor.get("mirror_of_package_path")
            mirror_index = descriptor.get("mirror_of_export_index")
            export_name = descriptor.get("export_name")
            destination_kind = descriptor.get("destination_kind")
            location_type = descriptor.get("location_type")
            structural_class_package = descriptor.get("structural_class_package")
            protected = descriptor.get("protected")
            protection_reasons = descriptor.get("protection_reasons")
            package_available = descriptor.get("package_available")
            prepared = descriptor.get("prepared")
            preparation_reason = descriptor.get("preparation_reason")
            rewards = descriptor.get("rewards")
            patch_descriptor = descriptor.get("patch_descriptor")
            if (
                not isinstance(package_path, str) or not package_path.startswith("/Game/")
                or key != f"{package_path.casefold()}#{export_index}"
                or not isinstance(export_index, int) or isinstance(export_index, bool) or export_index < 1
                or not _valid_world_loot_mirror_reference(
                    destination_kind, mirror_package, mirror_index,
                )
                or not isinstance(export_name, str) or not export_name
                or not isinstance(destination_kind, str) or not destination_kind
                or location_type not in {
                    "NORMAL_WORLD_LOOT", "RENEWABLE_INGREDIENT", "HEALING_UPGRADE_GOLDEN_SEED",
                    "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD", "PROTECTED", "UNSUPPORTED",
                }
                or not isinstance(protected, bool)
                or not isinstance(protection_reasons, list)
                or any(not isinstance(reason, str) or not reason.strip() for reason in protection_reasons)
                or protected != bool(protection_reasons)
                or not isinstance(package_available, bool)
                or not isinstance(prepared, bool)
                or not isinstance(preparation_reason, str) or not preparation_reason
                or not isinstance(rewards, list) or not rewards
                or not isinstance(patch_descriptor, dict)
                or patch_descriptor.get("serializer") != destination_kind
                or patch_descriptor.get("expected_rewards") != rewards
            ):
                return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot location descriptor is incomplete or inconsistent.")
            if package_available and not cache_inventory.contains(package_path):
                return GameDataCacheSnapshot(
                    GameDataCacheState.INVALID, entry,
                    f"Prepared World Loot destination package is missing from cache: {package_path}.",
                )
            if prepared and not package_available:
                return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "A missing World Loot destination is marked prepared.")
            if prepared and destination_kind == "ARTICLE_BULK":
                exports = map_descriptors.get(package_path)
                export_descriptor = exports.get(str(export_index)) if isinstance(exports, dict) else None
                if (
                    not isinstance(export_descriptor, dict)
                    or export_descriptor.get("export_name") != export_name
                    or descriptor.get("blueprint_class_name") != export_descriptor.get("class_name")
                    or descriptor.get("blueprint_class_package") != export_descriptor.get("class_package")
                ):
                    return GameDataCacheSnapshot(
                        GameDataCacheState.INVALID, entry,
                        f"Prepared World Loot actor/class identity is inconsistent: {package_path}#{export_index}.",
                    )
            if location_type in {"RENEWABLE_INGREDIENT", "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD"}:
                if not isinstance(structural_class_package, str) or not structural_class_package.startswith("/Game/Environment/Item/"):
                    return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Prepared physical World Loot class identity is invalid.")
                # Only renewable ingredients need a Material hierarchy profile.
                # Healing pickups have their own per-location prepared templates
                # and are deliberately excluded from the renewable class pool.
                if location_type == "RENEWABLE_INGREDIENT" and cache_inventory.contains(structural_class_package):
                    profiles = world_loot_index.get("renewable_class_profiles", {})
                    profile = profiles.get(structural_class_package.casefold()) if isinstance(profiles, dict) else None
                    if profile is None:
                        catalogued_material_parent = any(
                            isinstance(parent, str)
                            and parent.startswith("/Game/Blueprint/FieldGimmick/Material/")
                            and parent.rsplit("/", 1)[-1].startswith("BP_PickupMaterial_")
                            for parent in descriptor.get("parent_blueprint_packages", [])
                        )
                        if (
                            descriptor.get("catalogued_effective_reset_on_yadorigi_rest") is True
                            and descriptor.get("reset_validation_source") == "catalogued_physical_audit"
                            and descriptor.get("protected") is True
                            and descriptor.get("protection_reasons") == ["RENEWABLE_INGREDIENT_DISABLED"]
                            and catalogued_material_parent
                        ):
                            # The full installed-map audit contains reset-enabled material
                            # classes that may not be in the prepared ingredient pool.
                            # Their catalogued parent/reset evidence is authoritative for
                            # this locked category when Extract has no runtime profile.
                            profile = {
                                "package_path": structural_class_package,
                                "material_hierarchy": True,
                                "effective_reset_on_yadorigi_rest": None,
                                "catalogued_effective_reset_on_yadorigi_rest": True,
                                "reset_validation_source": "catalogued_physical_audit",
                            }
                    if (
                        not isinstance(profile, dict)
                        or not isinstance(profile.get("package_path"), str)
                        or profile["package_path"].casefold() != structural_class_package.casefold()
                        or profile.get("material_hierarchy") is not True
                    ):
                        return GameDataCacheSnapshot(
                            GameDataCacheState.INVALID, entry,
                            "Physical renewable ingredient class has no validated hierarchy profile: "
                            f"{structural_class_package} at {package_path}#{export_index}.",
                        )
                    if (
                        profile.get("catalogued_effective_reset_on_yadorigi_rest") is not True
                        or profile.get("effective_reset_on_yadorigi_rest") is False
                        or profile.get("reset_validation_source") not in {
                            "runtime_serialized_profile", "catalogued_physical_audit",
                        }
                    ):
                        return GameDataCacheSnapshot(
                            GameDataCacheState.INVALID, entry,
                            "Renewable ingredient cache classification is not backed by reset behavior: "
                            f"{structural_class_package} at {package_path}#{export_index}.",
                        )
            for reward_index, reward in enumerate(rewards):
                if (
                    not isinstance(reward, dict)
                    or reward.get("reward_index") != reward_index
                    or not isinstance(reward.get("article_path"), str)
                    or not reward["article_path"].startswith("/Game/")
                    or not isinstance(reward.get("count"), int) or isinstance(reward.get("count"), bool) or reward["count"] < 1
                    or not isinstance(reward.get("notification"), bool)
                    or not isinstance(reward.get("article_package_available"), bool)
                ):
                    return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot reward descriptor is invalid.")
                identity = reward.get("soft_object_path_identity")
                if identity is None:
                    if not protected or not protection_reasons:
                        return GameDataCacheSnapshot(
                            GameDataCacheState.INVALID, entry,
                            f"Unprotected World Loot reward has no exact FName identity: {package_path}#{export_index}.",
                        )
                else:
                    if not isinstance(identity, dict):
                        return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot reward FName identity is invalid.")
                    package_fname = identity.get("package_name")
                    asset_fname = identity.get("asset_name")
                    article_path = reward["article_path"]
                    if not isinstance(package_fname, dict) or not isinstance(asset_fname, dict):
                        return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot reward FName identity is incomplete.")
                    package_value, package_number = package_fname.get("value"), package_fname.get("number")
                    asset_value, asset_number = asset_fname.get("value"), asset_fname.get("number")
                    if (
                        not isinstance(package_value, str) or not isinstance(package_number, int) or isinstance(package_number, bool) or package_number < 0
                        or not isinstance(asset_value, str) or not isinstance(asset_number, int) or isinstance(asset_number, bool) or asset_number < 0
                        or _resolve_fname(package_value, package_number) != article_path
                        or _resolve_fname(asset_value, asset_number) != article_path.rsplit("/", 1)[-1]
                    ):
                        return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot reward FName does not resolve exactly to its Article.")
                # A DataAsset object path is also its package path; its parent
                # directory is not a loadable package in the IoStore index.
                article_package = reward["article_path"]
                if reward["article_package_available"] and not cache_inventory.contains(article_package):
                    return GameDataCacheSnapshot(
                        GameDataCacheState.INVALID, entry,
                        f"Installed World Loot Article package is missing from cache: {article_package}.",
                    )
        cdo_defaults = world_loot_index.get("blueprint_cdo_defaults", {}) if isinstance(world_loot_index, dict) else {}
        for key, descriptor in cdo_defaults.items():
            if not isinstance(key, str) or not isinstance(descriptor, dict):
                return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot CDO descriptor is invalid.")
            package_path = descriptor.get("package_path")
            export_index = descriptor.get("export_index")
            export_name = descriptor.get("export_name")
            parents = descriptor.get("parent_blueprint_packages")
            rewards = descriptor.get("rewards")
            physical_count = descriptor.get("physical_instance_count")
            if (
                not isinstance(package_path, str)
                or key != package_path.casefold()
                or not isinstance(export_index, int) or isinstance(export_index, bool) or export_index < 1
                or not isinstance(export_name, str) or not export_name.startswith("Default__")
                or not isinstance(parents, list) or not parents or any(not isinstance(value, str) for value in parents)
                or not isinstance(rewards, list) or not rewards
                or not isinstance(physical_count, int) or isinstance(physical_count, bool) or physical_count < 1
            ):
                return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot CDO descriptor is incomplete.")
            if any(not cache_inventory.contains(value) for value in (package_path, *parents)):
                return GameDataCacheSnapshot(
                    GameDataCacheState.INVALID, entry,
                    f"Cached World Loot CDO owner/parent package is missing: {package_path}.",
                )
            for reward in rewards:
                if not isinstance(reward, dict) or not isinstance(reward.get("article_path"), str):
                    return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot CDO reward descriptor is invalid.")
                identity = reward.get("soft_object_path_identity")
                if identity is not None:
                    if not isinstance(identity, dict):
                        return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot CDO FName descriptor is invalid.")
                    package_fname = identity.get("package_name")
                    asset_fname = identity.get("asset_name")
                    article_path = reward["article_path"]
                    if not isinstance(package_fname, dict) or not isinstance(asset_fname, dict):
                        return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot CDO FName descriptor is incomplete.")
                    package_value, package_number = package_fname.get("value"), package_fname.get("number")
                    asset_value, asset_number = asset_fname.get("value"), asset_fname.get("number")
                    if (
                        not isinstance(package_value, str) or not isinstance(package_number, int) or isinstance(package_number, bool)
                        or not isinstance(asset_value, str) or not isinstance(asset_number, int) or isinstance(asset_number, bool)
                        or _resolve_fname(package_value, package_number) != article_path
                        or _resolve_fname(asset_value, asset_number) != article_path.rsplit("/", 1)[-1]
                    ):
                        return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, "Cached World Loot CDO FName does not resolve to its Article.")
        missing_required = [value for value in required if not cache_inventory.contains(value)]
        if missing_required:
            return GameDataCacheSnapshot(
                GameDataCacheState.INVALID, entry,
                "Required source packages are missing from the extracted cache: " + ", ".join(missing_required),
            )
        try:
            validation_required = tuple(dict.fromkeys((*required, *expected_required_values)))
            self._validate_package_entries(entry, available, validation_required, deep=False)
            inventory = cache_inventory
            normalized_invalid = tuple(
                (str(row.get("package", "")), str(row.get("reason", "")))
                for row in invalid_sources if isinstance(row, dict)
                and isinstance(row.get("package"), str) and isinstance(row.get("reason"), str)
            )
            if not isinstance(source_registry, dict) or any(
                not isinstance(group, str)
                or not isinstance(packages, list)
                or any(not isinstance(package, str) for package in packages)
                for group, packages in source_registry.items()
            ):
                source_registry = {"cached_inventory": list(available)}
                metadata["source_registry"] = source_registry
            registry_values = tuple(sorted(
                {package for packages in source_registry.values() for package in packages},
                key=str.casefold,
            ))
            if not registry_values:
                registry_values = tuple(available)
            self._prune_legacy_entries(entry)
            ready_state = (
                GameDataCacheState.PREPARED_OUTDATED
                if prepared_cache_outdated else GameDataCacheState.READY
            )
            return GameDataCacheSnapshot(
                ready_state,
                entry,
                (
                    "Raw extracted assets are valid; prepared cache data can be rebuilt without re-extracting game assets."
                    if prepared_cache_outdated else "Extracted game data is ready."
                ),
                detection.version_id,
                detection.catalog_revision,
                tuple(value for value in registry_values if inventory.contains(value)),
                tuple(str(value) for value in optional_missing),
                normalized_invalid,
                effective_required,
                inventory,
                metadata,
            )
        except (OSError, ValueError, AssetExtractionError, ApplicationResourceError) as error:
            return GameDataCacheSnapshot(GameDataCacheState.INVALID, entry, f"Extracted cache failed integrity checks: {error}")

    # Read adopt legacy entry
    def _adopt_legacy_entry(self, destination: Path, identity: dict[str, str]) -> None:
        """Move one validated legacy content-addressed cache into the READY slot."""
        if not self.root.is_dir() or _is_link_or_junction(self.root) or destination.exists():
            return
        candidates: list[tuple[str, int, Path]] = []
        for child in self.root.iterdir():
            if (
                not _LEGACY_CACHE_ENTRY_NAME.fullmatch(child.name)
                or not child.is_dir()
                or _is_link_or_junction(child)
            ):
                continue
            try:
                metadata = json.loads((child / CACHE_INFO_FILENAME).read_text(encoding="utf-8"))
                cache_format = metadata.get("cache_format_version") if isinstance(metadata, dict) else None
                packages = metadata.get("extracted_package_inventory") if isinstance(metadata, dict) else None
                required = metadata.get("required_sources") if isinstance(metadata, dict) else None
                if (
                    not isinstance(metadata, dict)
                    or not _legacy_entry_matches_key(child.name, metadata)
                    or metadata.get("game_archive_sha256") != identity.get("game_archive_sha256")
                    or not isinstance(cache_format, int) or isinstance(cache_format, bool)
                    or cache_format < 1 or cache_format > CACHE_FORMAT_VERSION
                    or not isinstance(packages, list) or not packages
                    or any(not isinstance(value, str) for value in packages)
                    or not isinstance(required, list)
                    or any(not isinstance(value, str) for value in required)
                ):
                    continue
                self._validate_package_entries(child, packages, required, deep=False)
                extracted_at = metadata.get("extracted_at_utc")
                sort_key = extracted_at if isinstance(extracted_at, str) else ""
                candidates.append((sort_key, child.stat().st_mtime_ns, child))
            except (OSError, ValueError, AssetExtractionError, json.JSONDecodeError):
                continue
        if candidates:
            selected = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]
            selected.replace(destination)

    # Read prune legacy entries
    def _prune_legacy_entries(self, active_entry: Path) -> None:
        """Remove only recognized, older app cache entries after READY validates."""
        if not self.root.is_dir() or _is_link_or_junction(self.root):
            return
        for child in tuple(self.root.iterdir()):
            if (
                child == active_entry
                or not _LEGACY_CACHE_ENTRY_NAME.fullmatch(child.name)
                or not child.is_dir()
                or _is_link_or_junction(child)
            ):
                continue
            try:
                metadata = json.loads((child / CACHE_INFO_FILENAME).read_text(encoding="utf-8"))
                cache_format = metadata.get("cache_format_version") if isinstance(metadata, dict) else None
                if (
                    isinstance(metadata, dict)
                    and _legacy_entry_matches_key(child.name, metadata)
                    and isinstance(cache_format, int)
                    and not isinstance(cache_format, bool)
                    and 1 <= cache_format <= CACHE_FORMAT_VERSION
                    and isinstance(metadata.get("extracted_package_inventory"), list)
                ):
                    _remove_path_without_following_links(child)
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    # Cache entry path
    def cache_entry_path(self, game_root: Path, detection: VersionDetection, *, create: bool = False) -> Path:
        if not detection.usable_for_local_validation or not detection.version_id or not detection.catalog_revision:
            raise ValueError("A supported or local-validation game identity is required to resolve its cache entry.")
        identity = self._identity(game_root, detection)
        return self.cache.entry_path(
            game_version=detection.version_id,
            source_hash=_cache_source_fingerprint(identity),
            data_revision=detection.catalog_revision,
            create=create,
        )

    # Build identity
    def identity(self, game_root: Path, detection: VersionDetection) -> dict[str, str]:
        """Return the exact source identity stored in cache metadata."""
        return self._identity(game_root, detection)

    # Cache source hash
    def cache_source_hash(
        self,
        game_root: Path,
        detection: VersionDetection,
        *,
        identity: dict[str, str] | None = None,
    ) -> str:
        """Return the namespace hash shared by extraction and cache validation."""
        return _cache_source_fingerprint(identity or self._identity(game_root, detection))

    # Write metadata
    def write_metadata(self, entry: Path, document: dict[str, Any]) -> None:
        entry.mkdir(parents=True, exist_ok=True)
        target = entry / CACHE_INFO_FILENAME
        temporary = entry / f".{CACHE_INFO_FILENAME}.tmp"
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(target)

    # Build metadata
    def build_metadata(
        self,
        *,
        game_root: Path,
        detection: VersionDetection,
        source_registry: dict[str, tuple[str, ...]],
        required_sources: Iterable[str],
        invalid_sources: Iterable[tuple[str, str]] = (),
        world_loot_dependency_index: dict[str, Any] | None = None,
        extract_memory_summary: dict[str, Any] | None = None,
        extracted_assets: Iterable[ExtractedAsset] | None = None,
        identity: dict[str, str] | None = None,
        extracted_at: datetime | None = None,
    ) -> dict[str, Any]:
        identity = identity or self._identity(game_root, detection)
        entry = self.cache_entry_path(game_root, detection, create=False)
        if extracted_assets is None:
            packages = _enumerate_cached_packages(entry)
        else:
            packages: dict[str, ExtractedAsset] = {}
            assets_root = (entry / "assets").resolve(strict=True)
            for asset in extracted_assets:
                normalized, _relative = _normalize_asset_path(asset.virtual_path)
                key = normalized.casefold()
                if key in packages:
                    raise AssetExtractionError(f"Duplicate cached package was registered: {asset.virtual_path}.")
                if not asset.files or len(asset.files) != len(asset.file_hashes):
                    raise AssetExtractionError(
                        f"The cache write inventory has no complete write-time hash record for {asset.virtual_path}."
                    )
                suffixes: set[str] = set()
                for path, digest in zip(asset.files, asset.file_hashes, strict=True):
                    try:
                        path.resolve(strict=True).relative_to(assets_root)
                    except (OSError, ValueError) as error:
                        raise AssetExtractionError(
                            f"The cache write inventory escaped its owned assets directory: {path}."
                        ) from error
                    suffix = path.suffix.casefold()
                    if (
                        suffix not in {".uasset", ".umap", ".uexp", ".ubulk", ".uptnl"}
                        or suffix in suffixes
                        or not _is_sha256(digest)
                    ):
                        raise AssetExtractionError(
                            f"The cache write inventory contains an invalid file record for {asset.virtual_path}."
                        )
                    suffixes.add(suffix)
                if not suffixes.intersection({".uasset", ".umap"}):
                    raise AssetExtractionError(f"The cache write inventory has no package header for {asset.virtual_path}.")
                packages[normalized] = asset
        package_inventory = tuple(sorted(packages, key=str.casefold))
        package_files: dict[str, list[dict[str, Any]]] = {}
        for package, asset in packages.items():
            if not asset.files:
                raise AssetExtractionError(f"Cached package has no files: {package}.")
            package_root = asset.files[0].parent
            manifest_path = package_root / ".cv2randomizer-asset.json"
            metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
            package_files[package] = [
                {
                    "name": row["name"],
                "size": (package_root / row["name"]).stat().st_size,
                "sha256": row["sha256"],
                }
                for row in metadata["files"]
            ]
        required = tuple(sorted(set(required_sources), key=str.casefold))
        package_lookup = PackageInventory.create(package_inventory)
        required_keys = {value.casefold() for value in required}
        missing_optional = tuple(sorted(
            {
                package
                for packages_for_group in source_registry.values()
                for package in packages_for_group
                if not package_lookup.contains(package) and package.casefold() not in required_keys
            }, key=str.casefold,
        ))
        registered_sources = tuple(sorted(
            {package for packages_for_group in source_registry.values() for package in packages_for_group},
            key=str.casefold,
        ))
        document = {
            **identity,
            "cache_format_version": CACHE_FORMAT_VERSION,
            "cache_revision": f"{CACHE_FORMAT_VERSION}:{detection.catalog_revision}",
            "version_validation": "local-unverified" if detection.local_unverified else "signature-verified",
            "extracted_at_utc": (extracted_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
            "required_sources": list(required),
            "available_sources": [value for value in registered_sources if package_lookup.contains(value)],
            "unavailable_optional_sources": list(missing_optional),
            "invalid_sources": [
                {"package": package, "reason": reason}
                for package, reason in sorted(invalid_sources, key=lambda item: item[0].casefold())
            ],
            "source_registry": {
                group: list(sorted(set(packages_for_group), key=str.casefold))
                for group, packages_for_group in sorted(source_registry.items(), key=lambda item: item[0].casefold())
            },
            "extracted_package_inventory": list(package_inventory),
            "package_files": package_files,
        }
        if world_loot_dependency_index is not None:
            document["world_loot_dependency_index"] = world_loot_dependency_index
        if extract_memory_summary is not None:
            document["extract_memory_summary"] = extract_memory_summary
        return document

    # Validate staged entry
    def validate_staged_entry(
        self,
        entry: Path,
        required_sources: Iterable[str],
        *,
        trusted_write_hashes: bool = False,
    ) -> None:
        metadata = json.loads((entry / CACHE_INFO_FILENAME).read_text(encoding="utf-8"))
        packages = metadata.get("extracted_package_inventory")
        if not isinstance(packages, list):
            raise ValueError("Staged cache has no extracted package inventory.")
        # Extract has just produced each immutable asset and recorded its SHA-256
        # in both the package manifest and cache metadata. When that write-time
        # inventory is supplied by the active extractor, validation checks file
        # identity, component list, sizes, and manifest/hash agreement without
        # rereading every large map solely to reproduce the hashes.
        self._validate_package_entries(
            entry,
            packages,
            tuple(required_sources),
            deep=not trusted_write_hashes,
            verify_hashes=False if trusted_write_hashes else None,
            expected_package_files=metadata.get("package_files") if trusted_write_hashes else None,
        )
        inventory = PackageInventory.create(packages)
        if any(not inventory.contains(value) for value in required_sources):
            raise ValueError("Staged cache is missing one or more required source packages.")
        dependency_index = metadata.get("world_loot_dependency_index")
        binary_descriptors = (
            dependency_index.get("world_loot_binary_patch_packages")
            if isinstance(dependency_index, dict) else None
        )
        if binary_descriptors is not None:
            if not isinstance(binary_descriptors, dict) or dependency_index.get("binary_patch_version") != 3:
                raise ValueError("Staged cache has an invalid prepared World Loot descriptor index.")
            for package_key, descriptor in binary_descriptors.items():
                if not _valid_world_loot_binary_descriptor(package_key, descriptor):
                    raise ValueError("Staged cache has a malformed prepared World Loot descriptor.")
                if not _validate_prepared_world_asset(entry, descriptor, verify_hash=not trusted_write_hashes):
                    raise ValueError("Staged cache has a missing or invalid prepared World Loot package.")
            healing_maps = dependency_index.get("healing_prepared_maps")
            healing_clones = dependency_index.get("healing_prepared_clones")
            if not isinstance(healing_maps, dict) or not isinstance(healing_clones, dict):
                raise ValueError("Staged cache has no prepared Healing templates.")
            for package_key, descriptor in healing_maps.items():
                if not _valid_world_loot_binary_descriptor(package_key, descriptor):
                    raise ValueError("Staged cache has a malformed prepared Healing map descriptor.")
                if not _validate_prepared_world_asset(entry, descriptor, verify_hash=not trusted_write_hashes):
                    raise ValueError("Staged cache has a missing or invalid prepared Healing map.")
            for package_key, descriptor in healing_clones.items():
                if not _valid_healing_binary_descriptor(package_key, descriptor):
                    raise ValueError("Staged cache has a malformed prepared Healing Blueprint descriptor.")
                if not _validate_prepared_world_asset(entry, descriptor, verify_hash=not trusted_write_hashes):
                    raise ValueError("Staged cache has a missing or invalid prepared Healing Blueprint clone.")

    # Clear the extracted game cache
    def clear(self) -> None:
        """Clear every cache revision without deleting the Cache parent."""
        root = self._validated_extracted_root()
        if root.exists():
            if not root.is_dir() or _is_link_or_junction(root):
                raise OSError(f"Extracted cache root is not a regular directory: {root}")
            for child in tuple(root.iterdir()):
                _remove_path_without_following_links(child)
        else:
            root.mkdir(parents=True, exist_ok=True)

    # Check contents
    def has_contents(self) -> bool:
        """Return whether Cache\\extracted contains anything, after path validation."""
        root = self._validated_extracted_root()
        if not root.exists():
            return False
        if not root.is_dir() or _is_link_or_junction(root):
            raise OSError(f"Extracted cache root is not a regular directory: {root}")
        return next(root.iterdir(), None) is not None

    # Read validated extracted root
    def _validated_extracted_root(self) -> Path:
        configured = self.cache.root.expanduser()
        if not configured.is_absolute():
            configured = configured.absolute()
        expected = CacheManager().root.expanduser().absolute()
        if (
            os.path.normcase(os.path.abspath(configured))
            != os.path.normcase(os.path.abspath(expected))
        ):
            raise ValueError(
                "Refusing to clear a path other than the app's extracted game-data cache."
            )

        # Reject links in the configured path itself or any parent component so
        # validation cannot be redirected outside the app cache before deletion.
        current = Path(configured.anchor)
        for component in configured.parts[1:]:
            current = current / component
            if _is_link_or_junction(current):
                raise ValueError(f"Refusing to clear an extracted cache path containing a link: {current}")

        root = configured.resolve(strict=False)
        if (
            root == Path(root.anchor)
            or root.name.casefold() != "extracted"
            or root.parent.name.casefold() != "cache"
            or root.parent == root
        ):
            raise ValueError(f"Refusing to clear an unsafe extracted cache path: {root}")
        return root

    # Build identity
    def _identity(self, game_root: Path, detection: VersionDetection) -> dict[str, str]:
        installation = check_installation(game_root)
        if not installation.valid or installation.pak_directory is None:
            raise AssetExtractionError(installation.reason)
        executable = detection.executable_path
        if executable is None or not executable.is_file():
            raise AssetExtractionError("Supported game executable is missing.")
        identity = {
            "game_install_path": str(installation.game_root.resolve()).casefold(),
            "game_version": str(detection.version_id),
            "game_executable_sha256": _sha256_file(executable),
            "game_archive_sha256": archive_source_fingerprint(installation.pak_directory),
            "usmap_sha256": cv2_usmap_sha256(),
            "catalog_revision": str(detection.catalog_revision),
            "source_registry_revision": SOURCE_REGISTRY_REVISION,
            "world_loot_descriptor_revision": str(WORLD_LOOT_DESCRIPTOR_REVISION),
            "world_loot_preparation_revision": str(WORLD_LOOT_PREPARATION_REVISION),
            "raw_extraction_revision": str(RAW_EXTRACTION_REVISION),
            "prepared_cache_revision": str(PREPARED_CACHE_REVISION),
            "uassetapi_helper_sha256": _uassetapi_helper_fingerprint(),
        }
        identity["raw_cache_identity_sha256"] = _raw_cache_identity_fingerprint(identity)
        identity["prepared_cache_identity_sha256"] = _prepared_cache_identity_fingerprint(identity)
        return identity

    # Validate package entries
    def _validate_package_entries(
        self,
        entry: Path,
        packages: Iterable[str],
        required: Iterable[str],
        *,
        deep: bool,
        verify_hashes: bool | None = None,
        expected_package_files: Any = None,
    ) -> None:
        reader = IoStoreAssetExtractor(cache=CacheManager(root=entry.parent))
        package_values = tuple(packages)
        package_inventory = PackageInventory.create(package_values)
        required_keys = {
            resolved.casefold()
            for value in required
            if (resolved := package_inventory.resolve_package_path(value)[0]) is not None
        }
        for package in package_values:
            normalized, relative = _normalize_asset_path(package)
            cached = reader._read_cached_asset(
                entry,
                normalized,
                relative,
                verify_hash=(
                    deep or package.casefold() in required_keys
                    if verify_hashes is None else verify_hashes
                ),
            )
            if cached is None:
                raise AssetExtractionError(f"Cached source package is missing or has an invalid file manifest: {package}.")
            if expected_package_files is not None:
                if not isinstance(expected_package_files, dict):
                    raise AssetExtractionError("The staged cache has no package-component hash inventory.")
                expected = next((
                    value for key, value in expected_package_files.items()
                    if isinstance(key, str) and key.casefold() == package.casefold()
                ), None)
                if not isinstance(expected, list):
                    raise AssetExtractionError(f"The staged cache has no component hashes for {package}.")
                manifest_path = cached.files[0].parent / ".cv2randomizer-asset.json"
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise AssetExtractionError(f"Could not validate the staged manifest for {package}: {error}.") from error
                if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                    raise AssetExtractionError(f"The staged component manifest is invalid for {package}.")
                expected_by_name = {
                    row.get("name"): row for row in expected if isinstance(row, dict)
                }
                observed = manifest["files"]
                if len(expected_by_name) != len(expected) or len(observed) != len(expected):
                    raise AssetExtractionError(f"The staged component count differs for {package}.")
                for row in observed:
                    if not isinstance(row, dict):
                        raise AssetExtractionError(f"The staged component record is invalid for {package}.")
                    expected_row = expected_by_name.get(row.get("name"))
                    if (
                        not isinstance(expected_row, dict)
                        or row.get("size") != expected_row.get("size")
                        or row.get("sha256") != expected_row.get("sha256")
                    ):
                        raise AssetExtractionError(f"The staged component hash manifest differs for {package}.")
            if not deep and package.casefold() not in required_keys:
                manifest = json.loads((cached.files[0].parent / ".cv2randomizer-asset.json").read_text(encoding="utf-8"))
                for file in cached.files:
                    expected = next((row for row in manifest["files"] if row.get("name") == file.name), None)
                    if expected is None or ("size" in expected and file.stat().st_size != expected["size"]):
                        raise AssetExtractionError(f"Cached optional source file is missing or truncated: {file.name}.")


class CachedAssetExtractor:
    """Drop-in asset source that can only read validated persistent cache files."""

    # Initialize this object
    def __init__(self, snapshot: GameDataCacheSnapshot) -> None:
        if not snapshot.ready or snapshot.entry_path is None or snapshot.metadata is None:
            raise ValueError("A ready vanilla game-data cache is required.")
        self.snapshot = snapshot
        self.entry_path = snapshot.entry_path
        self.game_version = snapshot.game_version
        self.data_revision = snapshot.catalog_revision
        self.inventory = snapshot.inventory or PackageInventory.create(())
        self._reader = IoStoreAssetExtractor(cache=CacheManager(root=self.entry_path.parent))

    # Inspect package inventory
    def inspect_package_inventory(self, _game_root: Path) -> PackageInventory:
        return self.inventory

    # Extract requested game assets
    def extract_assets(
        self,
        _game_root: Path,
        virtual_paths: Iterable[str],
        *,
        optional_virtual_paths: Iterable[str] = (),
        game_version: str,
        data_revision: str,
        progress=None,
    ) -> tuple[ExtractedAsset, ...]:
        if game_version != self.game_version or data_revision != self.data_revision:
            raise AssetExtractionError("Requested assets do not match the active vanilla cache identity.")
        optional = {value.casefold() for value in optional_virtual_paths}
        requested: list[str] = []
        seen: set[str] = set()
        for value in virtual_paths:
            normalized, _relative = _normalize_asset_path(value)
            if normalized.casefold() not in seen:
                requested.append(normalized)
                seen.add(normalized.casefold())
        if not optional.issubset(seen):
            raise AssetExtractionError("Optional cached packages must also appear in the requested package list.")
        results: list[ExtractedAsset] = []
        for index, package in enumerate(requested, start=1):
            if progress:
                progress("preparing-staging-files", index, len(requested))
            # Preserve the physical package identity used by downstream staging
            # and map-plan joins; normalized inventory keys are lookup-only.
            resolved, variants = self.inventory.resolve_package_path_spelling(package)
            if resolved is None:
                if package.casefold() in optional and not variants:
                    continue
                if variants:
                    raise AssetExtractionError(
                        f"Cached package path {package} is ambiguous: {', '.join(variants)}."
                    )
                if package.casefold() in optional:
                    continue
                raise AssetExtractionError(f"Required cached source package is unavailable: {package}.")
            normalized, relative = _normalize_asset_path(resolved)
            # Cache readiness and package sizes are checked once before Generate.
            # Re-hashing each immutable package on every request duplicated work
            # across subsystems and was especially costly for map packages.
            cached = self._reader._read_cached_asset(self.entry_path, normalized, relative, verify_hash=False)
            if cached is None:
                raise AssetExtractionError(f"Cached source package failed its hash checks: {package}.")
            results.append(cached)
        return tuple(results)

    # Extract prepared assets
    def extract_prepared_assets(
        self,
        _game_root: Path,
        virtual_paths: Iterable[str],
        *,
        game_version: str,
        data_revision: str,
        progress=None,
    ) -> tuple[ExtractedAsset, ...]:
        """Read validated Extract-time NameMap templates for binary World Loot edits."""
        if game_version != self.game_version or data_revision != self.data_revision:
            raise AssetExtractionError("Requested prepared assets do not match the active vanilla cache identity.")
        requested: list[tuple[str, PurePosixPath]] = []
        seen: set[str] = set()
        for value in virtual_paths:
            normalized, _ = _normalize_asset_path(value)
            if normalized.casefold() in seen:
                continue
            resolved, variants = self.inventory.resolve_package_path_spelling(normalized)
            if resolved is None:
                if variants:
                    raise AssetExtractionError(
                        f"Prepared package path {normalized} is ambiguous: {', '.join(variants)}."
                    )
                raise AssetExtractionError(f"Prepared source package is unavailable: {normalized}.")
            normalized, relative = _normalize_asset_path(resolved)
            requested.append((normalized, relative))
            seen.add(normalized.casefold())
        results: list[ExtractedAsset] = []
        for index, (package, relative) in enumerate(requested, start=1):
            if progress:
                progress("preparing-prepared-staging-files", index, len(requested))
            cached = _read_prepared_cached_asset(self.entry_path, package, relative, verify_hash=False)
            if cached is None:
                raise AssetExtractionError(f"Prepared cached package is missing or invalid: {package}.")
            results.append(cached)
        return tuple(results)

    # Extract prepared assets with descriptors
    def extract_prepared_assets_with_descriptors(
        self,
        _game_root: Path,
        descriptors: Mapping[str, Any],
        *,
        game_version: str,
        data_revision: str,
        progress=None,
    ) -> tuple[ExtractedAsset, ...]:
        """Read prepared assets using cache-bound descriptors, including app-owned clone packages."""
        if game_version != self.game_version or data_revision != self.data_revision:
            raise AssetExtractionError("Requested prepared descriptors do not match the active vanilla cache identity.")
        requested: list[tuple[str, PurePosixPath, dict[str, Any]]] = []
        seen: set[str] = set()
        for key, descriptor in descriptors.items():
            package = descriptor.get("package_path") if isinstance(descriptor, dict) else None
            prepared = descriptor.get("prepared_asset") if isinstance(descriptor, dict) else None
            if not isinstance(package, str) or not isinstance(prepared, dict):
                raise AssetExtractionError(f"Prepared descriptor is incomplete for {key}.")
            normalized, relative = _normalize_asset_path(package)
            if key.casefold() != normalized.casefold() or normalized.casefold() in seen:
                raise AssetExtractionError(f"Prepared descriptor identity is duplicated or malformed for {key}.")
            if prepared.get("relative_path") != relative.as_posix():
                raise AssetExtractionError(f"Prepared descriptor path differs from its package identity: {package}.")
            requested.append((normalized, relative, prepared))
            seen.add(normalized.casefold())
        results: list[ExtractedAsset] = []
        for index, (package, relative, prepared) in enumerate(requested, start=1):
            if progress:
                progress("preparing-prepared-staging-files", index, len(requested))
            cached = _read_prepared_cached_asset(
                self.entry_path,
                package,
                relative,
                verify_hash=False,
                prepared_root_name=prepared.get("root_name", "prepared-assets"),
            )
            if cached is None:
                raise AssetExtractionError(f"Prepared cached descriptor package is missing or invalid: {package}.")
            results.append(cached)
        return tuple(results)


# List cached asset packages
def _enumerate_cached_packages(entry: Path) -> dict[str, ExtractedAsset]:
    result: dict[str, ExtractedAsset] = {}
    assets_root = entry / "assets"
    if not assets_root.is_dir():
        return result
    for manifest_path in sorted(assets_root.rglob(".cv2randomizer-asset.json"), key=lambda item: item.as_posix().casefold()):
        try:
            metadata: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        package = metadata.get("virtual_path") if isinstance(metadata, dict) else None
        if not isinstance(package, str):
            continue
        try:
            normalized, relative = _normalize_asset_path(package)
        except AssetExtractionError:
            continue
        asset = IoStoreAssetExtractor(cache=CacheManager(root=entry.parent))._read_cached_asset(entry, normalized, relative)
        if asset is not None:
            result[normalized] = asset
    return result


# Cache source fingerprint
def _cache_source_fingerprint(identity: dict[str, str]) -> str:
    """Bind an extracted cache entry to both game archives and the parser map."""
    material = "\0".join((
        identity["game_archive_sha256"], identity["usmap_sha256"], identity["catalog_revision"],
        identity["source_registry_revision"], identity["world_loot_descriptor_revision"],
        identity["world_loot_preparation_revision"], identity["uassetapi_helper_sha256"],
    ))
    return hashlib.sha256(material.encode("ascii")).hexdigest()


# Hash the UAssetAPI helper
def _uassetapi_helper_fingerprint() -> str:
    digest = hashlib.sha256()
    helper = resolve_uassetapi_helper()
    if helper.suffix.casefold() == ".exe":
        try:
            digest.update(helper.name.encode("utf-8"))
            digest.update(b"\0")
            with helper.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise AssetExtractionError(f"UAssetAPI helper binary is unavailable at {helper}: {error}") from error
        return digest.hexdigest()

    helper_root = helper.parent
    sources = sorted((*helper_root.glob("*.cs"), *helper_root.glob("*.csproj")), key=lambda path: path.name.casefold())
    if not sources:
        raise AssetExtractionError(f"UAssetAPI helper sources are unavailable at {helper_root}.")
    for source in sources:
        digest.update(source.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# Parse SHA-256 file
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Resolve the NameMap FName
def _resolve_fname(value: str, number: int) -> str:
    return value if number == 0 else f"{value}_{number - 1}"


# Check the World Loot mirror reference
def _valid_world_loot_mirror_reference(
    destination_kind: Any,
    mirror_package: Any,
    mirror_index: Any,
) -> bool:
    if mirror_package is None:
        return mirror_index is None
    if (
        not isinstance(mirror_package, str)
        or not isinstance(mirror_index, int)
        or isinstance(mirror_index, bool)
        or mirror_index < 1
    ):
        return False
    if destination_kind == "QUEST_ARTICLE_GRANT":
        return mirror_package.casefold().startswith("/game/quest/")
    return is_world_map_package(mirror_package)


# Check the World Loot binary descriptor
def _valid_world_loot_binary_descriptor(package_key: Any, descriptor: Any) -> bool:
    if not isinstance(package_key, str) or not isinstance(descriptor, dict):
        return False
    package_path = descriptor.get("package_path")
    header_hash = descriptor.get("header_sha256")
    sidecar_hash = descriptor.get("uexp_sha256")
    header_size = descriptor.get("header_size")
    sidecar_size = descriptor.get("uexp_size")
    name_indices = descriptor.get("name_map_indices")
    name_map_before = descriptor.get("name_map_count_before")
    name_map_after = descriptor.get("name_map_count_after")
    names_added = descriptor.get("names_added_count")
    fields = descriptor.get("fields")
    package_hashes = descriptor.get("package_file_hashes")
    if (
                descriptor.get("version") != 3
        or not isinstance(package_path, str) or not package_path.startswith("/Game/Map/")
        or package_path.casefold() != package_key
        or not _is_sha256(header_hash)
        or not isinstance(header_size, int) or isinstance(header_size, bool) or header_size <= 0
        or (sidecar_hash is not None and not _is_sha256(sidecar_hash))
        or (sidecar_size is not None and (
            not isinstance(sidecar_size, int) or isinstance(sidecar_size, bool) or sidecar_size <= 0
        ))
        or not isinstance(name_indices, dict)
        or any(not isinstance(name, str) or type(index) is not int or index < 0 for name, index in name_indices.items())
        or not isinstance(package_hashes, dict)
        or not package_hashes
        or any(not isinstance(suffix, str) or not suffix.startswith(".") or not _is_sha256(value)
               for suffix, value in package_hashes.items())
        or not any(suffix in {".umap", ".uasset"} for suffix in package_hashes)
        or not isinstance(fields, list)
        or not _valid_prepared_world_asset_descriptor(package_path, descriptor.get("prepared_asset"))
    ):
        return False
    if (name_map_before, name_map_after, names_added) != (None, None, None):
        if (
            type(name_map_before) is not int or name_map_before < 0
            or type(name_map_after) is not int or name_map_after < name_map_before
            or type(names_added) is not int or names_added < 0
            or name_map_after - name_map_before != names_added
        ):
            return False
    elif descriptor.get("prepared_asset") is not None:
        return False
    seen_slots: set[tuple[int, int]] = set()
    for field in fields:
        if not isinstance(field, dict):
            return False
        export_index, reward_index = field.get("export_index"), field.get("reward_index")
        export_name, available = field.get("export_name"), field.get("available")
        slot = (export_index, reward_index)
        if (
            type(export_index) is not int or export_index < 1
            or type(reward_index) is not int or reward_index < 0
            or not isinstance(export_name, str) or not export_name
            or not isinstance(available, bool)
            or slot in seen_slots
        ):
            return False
        seen_slots.add(slot)
        if available:
            expected_hex = field.get("expected_bytes_hex")
            offset = field.get("offset")
            identity = field.get("expected_identity")
            if (
                type(offset) is not int or offset < 0
                or not isinstance(expected_hex, str) or len(expected_hex) != 32
                or any(char not in "0123456789abcdefABCDEF" for char in expected_hex)
                or not _valid_soft_path_identity(identity)
            ):
                return False
        elif not isinstance(field.get("unavailable_reason"), str) or not field["unavailable_reason"]:
            return False
    return True


# Check the healing binary descriptor
def _valid_healing_binary_descriptor(package_key: Any, descriptor: Any) -> bool:
    """Validate a prepared, isolated Blueprint CDO ArticleDataPath descriptor."""
    if not isinstance(package_key, str) or not isinstance(descriptor, dict):
        return False
    package_path = descriptor.get("package_path")
    fields = descriptor.get("fields")
    hashes = descriptor.get("package_file_hashes")
    name_indices = descriptor.get("name_map_indices")
    before, after, added = (
        descriptor.get("name_map_count_before"),
        descriptor.get("name_map_count_after"),
        descriptor.get("names_added_count"),
    )
    if (
        descriptor.get("version") != 3
        or not isinstance(package_path, str)
        or not package_path.startswith("/Game/CV2Randomizer/Healing/")
        or package_path.casefold() != package_key.casefold()
        or not isinstance(fields, list) or len(fields) != 1
        or not isinstance(hashes, dict) or ".uasset" not in hashes or ".uexp" not in hashes
        or not _is_sha256(descriptor.get("header_sha256"))
        or not _is_sha256(descriptor.get("uexp_sha256"))
        or descriptor.get("header_sha256") != hashes.get(".uasset")
        or descriptor.get("uexp_sha256") != hashes.get(".uexp")
        or type(descriptor.get("header_size")) is not int or descriptor["header_size"] <= 0
        or type(descriptor.get("uexp_size")) is not int or descriptor["uexp_size"] <= 0
        or not isinstance(name_indices, dict)
        or any(not isinstance(name, str) or type(index) is not int or index < 0
               for name, index in name_indices.items())
        or type(before) is not int or before < 0
        or type(after) is not int or after < before
        or type(added) is not int or added != after - before
        or not _valid_prepared_world_asset_descriptor(package_path, descriptor.get("prepared_asset"))
    ):
        return False
    field = fields[0]
    expected_hex = field.get("expected_bytes_hex") if isinstance(field, dict) else None
    if (
        not isinstance(field, dict)
        or type(field.get("export_index")) is not int or field["export_index"] < 1
        or field.get("reward_index") != 0
        or not isinstance(field.get("export_name"), str) or not field["export_name"]
        or type(field.get("offset")) is not int or field["offset"] < 0
        or not isinstance(expected_hex, str) or len(expected_hex) != 32
        or any(char not in "0123456789abcdefABCDEF" for char in expected_hex)
        or field.get("available") is not True
        or not _valid_soft_path_identity(field.get("expected_identity"))
    ):
        return False
    return True


# Check the prepared world descriptor
def _valid_prepared_world_asset_descriptor(package_path: str, value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    relative = value.get("relative_path")
    files = value.get("files")
    root_name = value.get("root_name", "prepared-assets")
    expected = PurePosixPath(package_path.removeprefix("/Game/"))
    is_healing_clone = package_path.startswith("/Game/CV2Randomizer/Healing/")
    required_header = ".uasset" if is_healing_clone else ".umap"
    allowed_suffixes = {".uasset", ".uexp", ".ubulk", ".uptnl"} if is_healing_clone else {".umap", ".uexp", ".ubulk", ".uptnl"}
    if (
        not isinstance(relative, str)
        or root_name not in {"prepared-assets", "healing-prepared-assets"}
        or (is_healing_clone and root_name != "healing-prepared-assets")
        or "\\" in relative
        or PurePosixPath(relative) != expected
        or not isinstance(files, list)
        or not files
    ):
        return False
    seen: set[str] = set()
    suffixes: set[str] = set()
    for row in files:
        if not isinstance(row, dict):
            return False
        name, size, digest = row.get("name"), row.get("size"), row.get("sha256")
        if (
            not isinstance(name, str)
            or PurePosixPath(name).name != name
            or not isinstance(size, int) or isinstance(size, bool) or size <= 0
            or not _is_sha256(digest)
        ):
            return False
        suffix = Path(name).suffix.casefold()
        if suffix not in allowed_suffixes or name.casefold() in seen or suffix in suffixes:
            return False
        seen.add(name.casefold())
        suffixes.add(suffix)
    return required_header in suffixes and ".uexp" in suffixes


# Read prepared cached asset
def _read_prepared_cached_asset(
    entry: Path,
    virtual_path: str,
    relative: PurePosixPath,
    *,
    verify_hash: bool,
    prepared_root_name: str = "prepared-assets",
) -> ExtractedAsset | None:
    if prepared_root_name not in {"prepared-assets", "healing-prepared-assets"}:
        return None
    prepared_root = entry / prepared_root_name
    package_root = prepared_root.joinpath(*relative.parts)
    if not _is_within(package_root, prepared_root) or not package_root.is_dir():
        return None
    current = prepared_root
    for part in relative.parts:
        current = current / part
        if _is_link_or_junction(current):
            return None
    manifest_path = package_root / ".cv2randomizer-prepared-asset.json"
    try:
        metadata: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema_version") != 1
            or not isinstance(metadata.get("virtual_path"), str)
            or metadata["virtual_path"].casefold() != virtual_path.casefold()
            or not isinstance(metadata.get("files"), list)
        ):
            return None
        rows = metadata["files"]
        if not rows:
            return None
        files: list[Path] = []
        seen_names: set[str] = set()
        seen_suffixes: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                return None
            name, size, expected_hash = row.get("name"), row.get("size"), row.get("sha256")
            if (
                not isinstance(name, str)
                or PurePosixPath(name).name != name
                or not isinstance(size, int) or isinstance(size, bool) or size <= 0
                or not _is_sha256(expected_hash)
            ):
                return None
            suffix = Path(name).suffix.casefold()
            allowed_suffixes = (
                {".uasset", ".uexp", ".ubulk", ".uptnl"}
                if virtual_path.startswith("/Game/CV2Randomizer/Healing/")
                else {".umap", ".uexp", ".ubulk", ".uptnl"}
            )
            if suffix not in allowed_suffixes:
                return None
            if name.casefold() in seen_names or suffix in seen_suffixes:
                return None
            seen_names.add(name.casefold())
            seen_suffixes.add(suffix)
            path = package_root / name
            if not _is_within(path, package_root) or path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                return None
            if verify_hash and _sha256_file(path).casefold() != expected_hash.casefold():
                return None
            files.append(path)
        required_header = ".uasset" if virtual_path.startswith("/Game/CV2Randomizer/Healing/") else ".umap"
        if not {required_header, ".uexp"}.issubset(seen_suffixes):
            return None
        return ExtractedAsset(virtual_path, tuple(sorted(files, key=lambda path: path.suffix.casefold())))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


# Validate prepared world asset
def _validate_prepared_world_asset(entry: Path, descriptor: dict[str, Any], *, verify_hash: bool) -> bool:
    prepared = descriptor.get("prepared_asset")
    if prepared is None:
        return True
    package_path = descriptor.get("package_path")
    if not isinstance(package_path, str):
        return False
    try:
        normalized, relative = _normalize_asset_path(package_path)
    except AssetExtractionError:
        return False
    if prepared.get("relative_path") != relative.as_posix():
        return False
    asset = _read_prepared_cached_asset(
        entry, normalized, relative, verify_hash=verify_hash,
        prepared_root_name=prepared.get("root_name", "prepared-assets"),
    )
    if asset is None:
        return False
    package_root = asset.files[0].parent
    try:
        manifest: Any = json.loads(
            (package_root / ".cv2randomizer-prepared-asset.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if manifest.get("files") != prepared.get("files"):
        return False
    file_hashes = descriptor.get("package_file_hashes")
    if not isinstance(file_hashes, dict):
        return False
    for row in prepared["files"]:
        suffix = Path(row["name"]).suffix.casefold()
        if file_hashes.get(suffix) != row["sha256"]:
            return False
        if suffix == ".umap" and (
            descriptor.get("header_sha256") != row["sha256"]
            or descriptor.get("header_size") != row["size"]
        ):
            return False
        if suffix == ".uexp" and (
            descriptor.get("uexp_sha256") != row["sha256"]
            or descriptor.get("uexp_size") != row["size"]
        ):
            return False
    return True


# Check the soft path identity
def _valid_soft_path_identity(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    package_name, asset_name = value.get("package_name"), value.get("asset_name")
    return all(
        isinstance(name, dict)
        and isinstance(name.get("value"), str) and bool(name["value"])
        and type(name.get("number")) is int and name["number"] >= 0
        for name in (package_name, asset_name)
    )


# Check SHA-256
def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdefABCDEF" for char in value
    )
