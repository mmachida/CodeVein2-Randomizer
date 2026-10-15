"""Version-bound metadata for FieldEnemy direct Article reward locations."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.catalog_integrity import canonical_text_sha256
from .catalogs import QuestArticleGrantLayout, RewardPayload, WorldLootLocation
from .content_availability import PackageInventory
from .fname_identity import FNameIdentity, SoftObjectPathIdentity
from .important_items import ImportantItemExclusions

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DESTINATION_ID = re.compile(r"QST_FieldEnemy_[A-Za-z0-9_-]+")
_QUEST_GROUPS = frozenset({"UNI_C", "UNI_FO", "UNI_P", "UNI_VO"})
_CATEGORIES = frozenset({"DEFENSIVE_GADGET", "ENHANCEMENT_MATERIAL", "GADGET", "WEAPON"})


class FieldEnemyCatalogError(ValueError):
    """Raised when FieldEnemy metadata is stale, incomplete, or unsafe."""


@dataclass(frozen=True, slots=True)
class FieldEnemyProtectedExclusion:
    """Auditable protected key grant that never becomes a randomizable slot."""

    destination_id: str
    package_path: str
    article_path: str


@dataclass(frozen=True, slots=True)
class FieldEnemyCatalog:
    """Eligible FieldEnemy WHERE slots and separate protected key exclusions."""

    revision: str
    locations: tuple[WorldLootLocation, ...]
    protected_exclusions: tuple[FieldEnemyProtectedExclusion, ...]
    category_counts: tuple[tuple[str, int], ...]
    quest_group_counts: tuple[tuple[str, int], ...]

    # Read destination ids
    @property
    def destination_ids(self) -> frozenset[str]:
        return frozenset(str(location.destination_id) for location in self.locations)

    # Find eligible locations
    def eligible_locations(
        self,
        inventory: PackageInventory,
    ) -> tuple[WorldLootLocation, ...]:
        """Return only the optional WHERE slots present in this installation.

        The vanilla reward Article classification is deliberately not consulted:
        FieldEnemy adds destinations, while replacement WHAT comes from the
        canonical filtered World Loot pool. Older builds can be missing individual
        quest packages, so available locations remain usable independently.
        """
        return tuple(
            location for location in self.locations
            if inventory.contains(location.package_path)
        )

    # Resolve required package paths
    def required_package_paths(
        self,
        inventory: PackageInventory,
    ) -> tuple[str, ...]:
        """List the FieldEnemy Quest input packages available in this build."""
        return tuple(sorted(
            (location.package_path for location in self.eligible_locations(inventory)),
            key=str.casefold,
        ))


class FieldEnemyCatalogStore:
    """Read checksum-bound FieldEnemy research without treating it as reward inventory."""

    # Initialize this object
    def __init__(self, data_root: Path | None = None) -> None:
        self.data_root = data_root or application_data_path("field_enemy")

    # Load and validate catalog data
    def load(
        self,
        revision: str,
        *,
        important_item_exclusions: ImportantItemExclusions | None,
    ) -> FieldEnemyCatalog:
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise FieldEnemyCatalogError("FieldEnemy catalog revision must be a simple identifier.")
        directory = self.data_root / revision
        manifest = self._read_json(directory / "manifest.json", "FieldEnemy manifest")
        if (
            manifest.get("schema_version") != 1
            or manifest.get("catalog_revision") != revision
            or manifest.get("feature") != "field_enemy_direct_rewards"
            or manifest.get("engine_version") != "VER_UE5_4"
        ):
            raise FieldEnemyCatalogError("FieldEnemy manifest schema, feature, engine, or revision is invalid.")

        files = manifest.get("files")
        if not isinstance(files, dict):
            raise FieldEnemyCatalogError("FieldEnemy manifest files must be an object.")
        direct_doc, direct_rows = self._read_catalog_file(
            directory, files, "field_enemy_direct_rewards.json", "locations", revision, 24,
        )
        exclusion_doc, exclusion_rows = self._read_catalog_file(
            directory, files, "field_enemy_key_exclusions.json", "exclusions", revision, 10,
        )
        if (
            direct_doc.get("schema_version") != 1
            or direct_doc.get("feature") != "field_enemy_direct_rewards"
            or direct_doc.get("semantics") != {
                "role": "WHERE",
                "location_type": "FIELD_ENEMY_DIRECT_REWARD",
                "patch_field": "ArticleBulkInfo.Articles[].ArticleDataPath",
                "preserve_destination_fields": ["Count", "bNotification"],
            "dlc_rule": "Each FieldEnemy WHERE slot is eligible only when its Quest package is installed.",
            }
        ):
            raise FieldEnemyCatalogError("FieldEnemy direct reward document metadata is invalid.")
        if (
            exclusion_doc.get("schema_version") != 1
            or exclusion_doc.get("feature") != "field_enemy_key_exclusions"
            or exclusion_doc.get("match_policy") != "EXACT_IDENTITY_ONLY"
        ):
            raise FieldEnemyCatalogError("FieldEnemy key exclusion document metadata is invalid.")
        if important_item_exclusions is None or important_item_exclusions.revision != revision:
            raise FieldEnemyCatalogError("Version-matched Important Item identities are required for FieldEnemy keys.")

        locations = tuple(self._parse_location(row) for row in direct_rows)
        exclusions = tuple(self._parse_exclusion(row) for row in exclusion_rows)
        self._validate_unique_records(locations, exclusions)

        important_keys = important_item_exclusions.key_identities
        missing_keys = sorted(
            (item.article_path for item in exclusions if item.article_path.casefold() not in important_keys),
            key=str.casefold,
        )
        if missing_keys:
            raise FieldEnemyCatalogError(
                "FieldEnemy protected rewards are missing from the global exact Key exclusion list: "
                + ", ".join(missing_keys)
            )

        category_counts = Counter(str(row["rewards"][0]["category"]) for row in direct_rows)
        quest_group_counts = Counter(str(row["quest_group"]) for row in direct_rows)
        counts = manifest.get("counts")
        if not isinstance(counts, dict) or counts != {
            "eligible_locations": len(locations),
            "protected_exclusions": len(exclusions),
            "content_class": {"DLC": len(locations)},
            "categories": dict(sorted(category_counts.items())),
            "quest_groups": dict(sorted(quest_group_counts.items())),
        }:
            raise FieldEnemyCatalogError("FieldEnemy manifest counts do not match the catalog records.")

        return FieldEnemyCatalog(
            revision,
            locations,
            exclusions,
            tuple(sorted(category_counts.items())),
            tuple(sorted(quest_group_counts.items())),
        )

    # Read JSON
    @staticmethod
    def _read_json(path: Path, label: str) -> dict[str, Any]:
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise FieldEnemyCatalogError(f"Could not read {label}: {error}.") from error
        if not isinstance(value, dict):
            raise FieldEnemyCatalogError(f"{label} must be a JSON object.")
        return value

    # Read catalog file
    @classmethod
    def _read_catalog_file(
        cls,
        directory: Path,
        manifest_files: dict[str, Any],
        filename: str,
        collection: str,
        revision: str,
        expected_records: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        path = directory / filename
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise FieldEnemyCatalogError(f"Could not read {filename}: {error}.") from error
        metadata = manifest_files.get(filename)
        if (
            not isinstance(metadata, dict)
            or metadata.get("sha256") != canonical_text_sha256(payload)
            or metadata.get("records") != expected_records
        ):
            raise FieldEnemyCatalogError(f"FieldEnemy catalog signature or record count mismatch: {filename}.")
        try:
            document: Any = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise FieldEnemyCatalogError(f"{filename} is not valid UTF-8 JSON: {error}.") from error
        if not isinstance(document, dict) or document.get("catalog_revision") != revision:
            raise FieldEnemyCatalogError(f"{filename} revision does not match the game catalog revision.")
        rows = document.get(collection)
        if not isinstance(rows, list) or len(rows) != expected_records or any(not isinstance(row, dict) for row in rows):
            raise FieldEnemyCatalogError(f"{filename}.{collection} must contain exactly {expected_records} objects.")
        return document, rows

    # Parse location
    @classmethod
    def _parse_location(cls, row: dict[str, Any]) -> WorldLootLocation:
        destination_id = cls._string(row.get("id"), "FieldEnemy location id")
        package_path = cls._string(row.get("virtual_package"), "FieldEnemy virtual package")
        source_file = cls._string(row.get("source_file"), "FieldEnemy source file")
        expected_package = f"/Game/Quest/FieldEnemy/{destination_id}"
        expected_source = f"Quest\\FieldEnemy\\{destination_id}.uasset"
        if (
            row.get("location_type") != "FIELD_ENEMY_DIRECT_REWARD"
            or package_path != expected_package
            or source_file.replace("/", "\\") != expected_source
            or row.get("content_class") != "DLC"
            or row.get("protected") is not False
            or row.get("randomizable") is not True
            or row.get("where_only") is not True
            or row.get("protection_reasons") != []
        ):
            raise FieldEnemyCatalogError(f"FieldEnemy location is not an eligible DLC WHERE slot: {destination_id}.")
        quest_group = cls._string(row.get("quest_group"), "FieldEnemy quest group")
        if quest_group not in _QUEST_GROUPS:
            raise FieldEnemyCatalogError(f"Unknown FieldEnemy quest group: {quest_group}.")
        fingerprint = cls._fingerprint(row.get("source_fingerprint"), destination_id)
        identity = row.get("patch_identity")
        if not isinstance(identity, dict):
            raise FieldEnemyCatalogError(f"Missing FieldEnemy patch identity: {destination_id}.")
        export_index = cls._integer(identity.get("export_index"), "FieldEnemy export index", minimum=1)
        export_name = cls._string(identity.get("export_name"), "FieldEnemy export name")
        bulk_index = cls._integer(identity.get("bulk_index"), "FieldEnemy ArticleBulkInfo index")
        reward_index = cls._integer(identity.get("reward_index"), "FieldEnemy reward index")
        if export_index != 7 or export_name != "QSTask_GainItem_0" or bulk_index != 0 or reward_index != 0:
            raise FieldEnemyCatalogError(f"FieldEnemy quest grant layout changed: {destination_id}.")
        articles_count = cls._integer(identity.get("articles_count"), "FieldEnemy Articles count", minimum=1)
        if identity.get("sub_path_string") != "" or articles_count != 1:
            raise FieldEnemyCatalogError(f"FieldEnemy Article array identity changed: {destination_id}.")
        rewards = row.get("rewards")
        if not isinstance(rewards, list) or len(rewards) != 1 or not isinstance(rewards[0], dict):
            raise FieldEnemyCatalogError(f"FieldEnemy must remain a singleton Article grant: {destination_id}.")
        reward = rewards[0]
        article_path = cls._article_path(reward.get("article_path"), "FieldEnemy Article path")
        asset_name = cls._string(reward.get("asset_name"), "FieldEnemy Article asset name")
        category = cls._string(reward.get("category"), "FieldEnemy reward category")
        if (
            asset_name != article_path.rsplit("/", 1)[-1]
            or category not in _CATEGORIES
            or cls._integer(reward.get("count"), "FieldEnemy reward Count", minimum=1) != 1
            or reward.get("notification") is not True
        ):
            raise FieldEnemyCatalogError(f"FieldEnemy reward payload changed: {destination_id}.")
        package_name = cls._string(identity.get("package_name_value"), "FieldEnemy PackageName")
        asset_name_value = cls._string(identity.get("asset_name_value"), "FieldEnemy AssetName")
        package_number = cls._integer(identity.get("package_name_number"), "FieldEnemy PackageName number")
        asset_number = cls._integer(identity.get("asset_name_number"), "FieldEnemy AssetName number")
        expected_asset_name = article_path.rsplit("/", 1)[-1]
        # Resolve name
        def resolve_name(value: str, number: int) -> str:
            return value if number == 0 else f"{value}_{number - 1}"
        if (
            resolve_name(package_name, package_number) != article_path
            or resolve_name(asset_name_value, asset_number) != expected_asset_name
        ):
            raise FieldEnemyCatalogError(f"FieldEnemy vanilla FName identity does not match its Article path: {destination_id}.")
        layout = QuestArticleGrantLayout(
            export_name=export_name,
            bulk_index=bulk_index,
            reward_index=reward_index,
            expected_article_path=article_path,
            expected_package_name_value=package_name,
            expected_package_name_number=package_number,
            expected_asset_name_value=asset_name_value,
            expected_asset_name_number=asset_number,
            count=1,
            notification=True,
            expected_sub_path_string="",
            expected_articles_count=1,
            source_uasset_sha256=fingerprint[".uasset"],
            source_uexp_sha256=fingerprint[".uexp"],
        )
        return WorldLootLocation(
            package_path=package_path,
            source_file=expected_source,
            export_index=export_index,
            export_name=export_name,
            actor_class=export_name,
            actor_category="DIRECT_ARTICLE_GRANT",
            save_id=None,
            protected=False,
            protection_reasons=(),
            # The source location is confirmed DLC, but the Article identity has
            # no independent reviewed BASE/DLC classification yet.
            rewards=(RewardPayload(
                article_path,
                1,
                True,
                "UNKNOWN",
                soft_object_path_identity=SoftObjectPathIdentity(
                    FNameIdentity(package_name, package_number),
                    FNameIdentity(asset_name_value, asset_number),
                ),
            ),),
            destination_kind="QUEST_ARTICLE_GRANT",
            destination_id=destination_id,
            display_name=f"FieldEnemy {quest_group}: {destination_id}",
            quest_article_layout=layout,
            content_class="DLC",
        )

    # Parse exclusion
    @classmethod
    def _parse_exclusion(cls, row: dict[str, Any]) -> FieldEnemyProtectedExclusion:
        destination_id = cls._string(row.get("id"), "FieldEnemy protected id")
        if not _DESTINATION_ID.fullmatch(destination_id):
            raise FieldEnemyCatalogError(f"Invalid FieldEnemy protected id: {destination_id}.")
        package_path = cls._string(row.get("virtual_package"), "FieldEnemy protected package")
        expected_package = f"/Game/Quest/FieldEnemy/{destination_id}"
        expected_source = f"Quest\\FieldEnemy\\{destination_id}.uasset"
        if (
            row.get("location_type") != "FIELD_ENEMY_DIRECT_REWARD"
            or package_path != expected_package
            or cls._string(row.get("source_file"), "FieldEnemy protected source file").replace("/", "\\") != expected_source
            or row.get("protected") is not True
            or row.get("randomizable") is not False
            or row.get("protection_reasons") != ["KEY_OR_PROGRESSION_ITEM"]
        ):
            raise FieldEnemyCatalogError(f"FieldEnemy protected key record is invalid: {destination_id}.")
        cls._fingerprint(row.get("source_fingerprint"), destination_id)
        article_path = cls._article_path(row.get("article_path"), "FieldEnemy protected Article path")
        if (
            cls._string(row.get("asset_name"), "FieldEnemy protected asset name") != article_path.rsplit("/", 1)[-1]
            or cls._integer(row.get("count"), "FieldEnemy protected Count", minimum=1) != 1
            or row.get("notification") is not True
        ):
            raise FieldEnemyCatalogError(f"FieldEnemy protected payload changed: {destination_id}.")
        identity = row.get("patch_identity")
        if (
            not isinstance(identity, dict)
            or identity.get("export_index") != 7
            or identity.get("export_name") != "QSTask_GainItem_0"
            or identity.get("bulk_index") != 0
            or identity.get("reward_index") != 0
        ):
            raise FieldEnemyCatalogError(f"FieldEnemy protected grant identity changed: {destination_id}.")
        return FieldEnemyProtectedExclusion(destination_id, package_path, article_path)

    # Calculate the source fingerprint
    @classmethod
    def _fingerprint(cls, value: Any, destination_id: str) -> dict[str, str]:
        if not isinstance(value, dict):
            raise FieldEnemyCatalogError(f"FieldEnemy source fingerprint is missing: {destination_id}.")
        combined = cls._string(value.get("combined_sha256"), "FieldEnemy combined SHA-256")
        files = value.get("files")
        if not isinstance(files, dict):
            raise FieldEnemyCatalogError(f"FieldEnemy sidecar fingerprints are missing: {destination_id}.")
        hashes = {suffix: cls._string(files.get(suffix), f"FieldEnemy {suffix} SHA-256") for suffix in (".uasset", ".uexp")}
        if not _SHA256.fullmatch(combined) or any(not _SHA256.fullmatch(digest) for digest in hashes.values()):
            raise FieldEnemyCatalogError(f"FieldEnemy source fingerprint is malformed: {destination_id}.")
        return {"combined_sha256": combined, **hashes}

    # Validate unique records
    @staticmethod
    def _validate_unique_records(
        locations: tuple[WorldLootLocation, ...],
        exclusions: tuple[FieldEnemyProtectedExclusion, ...],
    ) -> None:
        all_records = [*locations, *exclusions]
        ids = [
            (str(item.destination_id) if isinstance(item, WorldLootLocation) else item.destination_id).casefold()
            for item in all_records
        ]
        packages = [item.package_path.casefold() for item in all_records]
        if len(set(ids)) != len(ids) or len(set(packages)) != len(packages):
            raise FieldEnemyCatalogError("FieldEnemy source slot identities must be unique and disjoint.")

    # Validate a text value
    @staticmethod
    def _string(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise FieldEnemyCatalogError(f"{label} must be a non-empty string.")
        return value

    # Parse an integer value
    @staticmethod
    def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise FieldEnemyCatalogError(f"{label} must be an integer of at least {minimum}.")
        return value

    # Resolve article path
    @classmethod
    def _article_path(cls, value: Any, label: str) -> str:
        path = cls._string(value, label)
        if not path.startswith("/Game/Blueprint/Article/") or path.endswith("/") or "." in path.rsplit("/", 1)[-1]:
            raise FieldEnemyCatalogError(f"{label} must identify a canonical Article package.")
        return path
