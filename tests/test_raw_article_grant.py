from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.blood_code_generation import BloodCodeGenerationError, BloodCodeGrantSnapshot
from cv2randomizer.game.article_reward_catalog import CanonicalArticleRewardCatalogStore
from cv2randomizer.game.raw_article_grant_generation import (
    RawArticleGrantGenerationError,
    RawArticleGrantSnapshot,
    RawArticleGrantSerializer,
    filter_compatible_raw_article_grants,
    filter_compatible_world_loot_quest_grants,
)
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.game.jail_catalog import JailCatalogStore
from cv2randomizer.randomizer.defensive_gadgets import defensive_gadget_world_candidates
from cv2randomizer.randomizer.jails import jail_world_candidates
from cv2randomizer.randomizer.article_rewards import canonical_article_world_candidates
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan
from tests._content_class_fixtures import classified_world_fixture


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = (
    ROOT / "workspace" / "cache" / "defensive-gadget-research" / "Content" / "Cinematics"
    / "CinematicScene" / "Scenario" / "SmallScenario" / "Blueprint" / "Common" / "Test"
    / "JadwigaServant"
)
SOURCES = {
    "jadwiga_servant_lightning_barrage_reward": SOURCE_ROOT / "BP_SmallScenario_JadwigaServant1.uasset",
    "jadwiga_servant_booster_dex_reward": SOURCE_ROOT / "BP_SmallScenario_JadwigaServant2.uasset",
    "jadwiga_servant_initial_parry_reward": SOURCE_ROOT / "BP_SmallScenario_JadwigaServant3.uasset",
}
MAPPINGS = ROOT / "workspace" / "research_assets" / "CV2.usmap"
ARTICLE_CASES = (
    "/Game/Blueprint/Article/Gadget/StandAlone/DA_Bow",
    "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Warp_2",
    "/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight_1",
)


class DirectWorldLootRewardPoolTests(unittest.TestCase):
    # Check direct rewards hot spring bundles
    def test_direct_rewards_and_hot_spring_bundles_are_generic_world_loot_destinations(self) -> None:
        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        gadget_catalog = DefensiveGadgetCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        jail_catalog = JailCatalogStore(ROOT / "data" / "jails" / "current").load()
        article_catalog = CanonicalArticleRewardCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        plan = build_world_loot_randomization_plan(
            classified_world_fixture(catalogs.world_loot),
            "jadwiga-generic-world-seed",
            unsafe_packages=catalogs.unsafe_packages,
            catalog_candidates=(
                *defensive_gadget_world_candidates(gadget_catalog),
                *jail_world_candidates(jail_catalog),
                *canonical_article_world_candidates(article_catalog),
            ),
        )
        raw_direct = [
            assignment for assignment in plan.assignments
            if assignment.destination.destination_kind == "RAW_ARTICLE_GRANT"
        ]
        quest_direct = [
            assignment for assignment in plan.assignments
            if assignment.destination.destination_kind == "QUEST_ARTICLE_GRANT"
        ]
        self.assertEqual(
            len(plan.assignments),
            sum(not location.protected for location in catalogs.world_loot),
        )
        self.assertEqual({item.destination.destination_id for item in raw_direct}, set(SOURCES))
        self.assertEqual(len(raw_direct), 3)
        self.assertEqual(len(quest_direct), 77)
        self.assertEqual(
            {item.destination.destination_id for item in quest_direct},
            {
                item.destination_id for item in catalogs.world_loot
                if item.destination_id is not None
                and item.destination_kind == "QUEST_ARTICLE_GRANT"
            },
        )
        self.assertEqual(plan.booster_candidate_count, 3)
        self.assertEqual(plan.booster_injected_count, 0)
        self.assertEqual(plan.booster_duplicate_sources, 0)
        self.assertTrue(all(len(item.assigned_rewards) == 1 for item in raw_direct))
        self.assertEqual(sum(len(item.assigned_rewards) == 1 for item in quest_direct), 63)
        self.assertEqual(sum(len(item.assigned_rewards) == 2 for item in quest_direct), 14)
        hot_spring_assignments = [
            item for item in quest_direct if item.destination.destination_id.startswith("hot_spring_")
        ]
        self.assertEqual(len(hot_spring_assignments), 10)
        self.assertEqual(sum(len(item.assigned_rewards) for item in hot_spring_assignments), 19)
        self.assertTrue(all(
            not item.assigned_rewards[0].article_path.startswith("/Game/Blueprint/Article/Gadget/Defence/")
            for item in raw_direct
        ))
        self.assertEqual(
            {item.destination.display_name for item in raw_direct},
            {
                "Jadwiga Servant - Lightning Barrage Reward",
                "Jadwiga Servant - Booster Dex Reward",
                "Jadwiga Servant - Initial Parry Reward",
            },
        )
        singleton_world_rewards = {
            location.rewards[0].article_path
            for location in catalogs.world_loot
            if not location.protected and len(location.rewards) == 1
        }
        self.assertTrue(all(item.assigned_rewards[0].article_path in singleton_world_rewards for item in raw_direct))
        self.assertTrue(all(
            item.assigned_rewards[0].article_path.startswith("/Game/Blueprint/Article/")
            for item in quest_direct
        ))

    # Check malformed raw destination fail closed
    def test_malformed_raw_destination_fails_closed_in_planner(self) -> None:
        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        raw_slot = next(location for location in catalogs.world_loot if location.destination_id)
        from dataclasses import replace

        bad_slot = replace(raw_slot, raw_article_layout=None)
        plan = build_world_loot_randomization_plan((bad_slot,), "bad-jadwiga-layout")
        self.assertEqual(plan.assignments, ())
        self.assertEqual(plan.locked_locations[0].reason, "unvalidated direct Article grant destination")

    # Check malformed structured quest destination fail
    def test_malformed_structured_quest_destination_fails_closed_in_planner(self) -> None:
        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        quest_slot = next(location for location in catalogs.world_loot if location.destination_kind == "QUEST_ARTICLE_GRANT")
        from dataclasses import replace

        bad_slot = replace(quest_slot, quest_article_layout=None)
        plan = build_world_loot_randomization_plan((bad_slot,), "bad-quest-article-layout")
        self.assertEqual(plan.assignments, ())
        self.assertEqual(plan.locked_locations[0].reason, "unvalidated structured quest Article grant destination")

    # Leave incompatible raw sources vanilla
    def test_changed_raw_grant_fingerprint_is_filtered_before_planning(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        location = next(
            item for item in catalogs.world_loot
            if item.destination_id == "jadwiga_servant_lightning_barrage_reward"
        )
        unrelated = next(
            item for item in catalogs.world_loot
            if item.destination_kind != "RAW_ARTICLE_GRANT"
        )

        class Extractor:
            # Initialize this object
            def __init__(self, content_root: Path) -> None:
                self.content_root = content_root
                self.requested: tuple[str, ...] = ()

            # Extract requested game assets
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                self.requested = tuple(virtual_paths)
                files = []
                for extension, payload in ((".uasset", b"changed source"), (".uexp", b"changed exports")):
                    path = self.content_root / (location.package_path.rsplit("/", 1)[-1] + extension)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                    files.append(path)
                return (ExtractedAsset(location.package_path, tuple(files)),)

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            extractor = Extractor(Path(temporary))
            compatible, rejected = filter_compatible_raw_article_grants(
                (location, unrelated),
                game_root=Path(temporary),
                game_version="fixture-build",
                data_revision="current",
                extractor=extractor,
            )

        self.assertEqual(extractor.requested, (location.package_path,))
        self.assertEqual(rejected, (location,))
        self.assertEqual(compatible, (unrelated,))

    # Rebind exact profile raw grants
    def test_recognized_profile_rebinds_raw_grant_after_identity_check(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        location = next(
            item for item in catalogs.world_loot
            if item.destination_id == "jadwiga_servant_lightning_barrage_reward"
        )
        layout = location.raw_article_layout
        header_data = b"recognized profile header"
        export_data = b"recognized profile exports"

        class Extractor:
            # Extract requested direct packages
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                files = []
                for package in virtual_paths:
                    header = Path(temporary) / (Path(package).name + ".uasset")
                    header.write_bytes(header_data)
                    exports = header.with_suffix(".uexp")
                    exports.write_bytes(export_data)
                    files.append(ExtractedAsset(package, (header, exports)))
                return tuple(files)

        class Serializer:
            # Inspect exact raw reward
            def inspect_compatibility_profile(self, *, slot, input_file, compatibility_profile, work_root, **_kwargs):
                self.asserted_profile = compatibility_profile
                if not work_root.is_dir() or not input_file.is_relative_to(work_root):
                    raise ValueError("profile inspection paths are outside temporary workspace")
                return RawArticleGrantSnapshot(
                    slot.package_path,
                    input_file,
                    7,
                    layout.export_name,
                    layout.serial_size,
                    31,
                    layout.expected_vanilla_number,
                    32,
                    layout.expected_vanilla_number,
                    layout.expected_path_name_map_value,
                    layout.expected_object_name_map_value,
                    layout.expected_vanilla_article_path,
                )

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            real_temporary_directory = tempfile.TemporaryDirectory

            # Create temp folder under Delete
            def create_test_temporary_directory(*, prefix="", dir=None):
                return real_temporary_directory(prefix=prefix, dir=dir or ROOT / "Delete")

            with patch(
                "cv2randomizer.game.raw_article_grant_generation.tempfile.TemporaryDirectory",
                side_effect=create_test_temporary_directory,
            ):
                compatible, rejected = filter_compatible_raw_article_grants(
                    (location,),
                    game_root=Path(temporary),
                    game_version="local-unverified",
                    data_revision="current",
                    extractor=Extractor(),
                    compatibility_profile="1.0.9",
                    mappings_path=Path(temporary) / "CV2.usmap",
                    serializer=Serializer(),
                )

        self.assertEqual(rejected, ())
        self.assertEqual(len(compatible), 1)
        rebound = compatible[0]
        self.assertEqual(rebound.export_index, 7)
        self.assertEqual(rebound.raw_article_layout.expected_path_name_index, 31)
        self.assertEqual(rebound.raw_article_layout.expected_object_name_index, 32)
        self.assertEqual(rebound.raw_article_layout.source_uasset_sha256, hashlib.sha256(header_data).hexdigest())
        self.assertEqual(rebound.raw_article_layout.source_uexp_sha256, hashlib.sha256(export_data).hexdigest())

    # Keep matching raw sources eligible
    def test_matching_raw_grant_fingerprint_remains_eligible(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        location = next(
            item for item in catalogs.world_loot
            if item.destination_id == "jadwiga_servant_lightning_barrage_reward"
        )
        header_bytes = b"expected source header"
        exports_bytes = b"expected source exports"
        layout = replace(
            location.raw_article_layout,
            source_uasset_sha256=hashlib.sha256(header_bytes).hexdigest(),
            source_uexp_sha256=hashlib.sha256(exports_bytes).hexdigest(),
        )
        location = replace(location, raw_article_layout=layout)

        class Extractor:
            # Initialize this object
            def __init__(self, content_root: Path) -> None:
                self.content_root = content_root

            # Extract requested game assets
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                files = []
                for extension, payload in ((".uasset", header_bytes), (".uexp", exports_bytes)):
                    path = self.content_root / (location.package_path.rsplit("/", 1)[-1] + extension)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                    files.append(path)
                return (ExtractedAsset(location.package_path, tuple(files)),)

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            compatible, rejected = filter_compatible_raw_article_grants(
                (location,),
                game_root=Path(temporary),
                game_version="fixture-build",
                data_revision="current",
                extractor=Extractor(Path(temporary)),
            )

        self.assertEqual(compatible, (location,))
        self.assertEqual(rejected, ())

    # Leave changed quest task vanilla
    def test_changed_quest_task_layout_is_filtered_before_planning(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        location = next(
            item for item in catalogs.world_loot
            if item.destination_id == "quest_reward_qst_characterepisode_holly_c05020_48"
        )
        unrelated = next(
            item for item in catalogs.world_loot
            if item.destination_kind != "QUEST_ARTICLE_GRANT"
        )
        layout = location.quest_grant_layouts[0]

        class Extractor:
            # Initialize this object
            def __init__(self, content_root: Path) -> None:
                self.content_root = content_root

            # Extract requested game assets
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                results = []
                for package in virtual_paths:
                    header = self.content_root / Path(*package.split("/")[2:]).with_suffix(".uasset")
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes(b"quest source")
                    export = header.with_suffix(".uexp")
                    export.write_bytes(b"quest exports")
                    results.append(ExtractedAsset(package, (header, export)))
                return tuple(results)

        class Serializer:
            # Inspect source quest packages
            def inspect_world_loot_quest_packages(self, *, package_files, **_kwargs):
                return (BloodCodeGrantSnapshot(
                    virtual_package=location.package_path,
                    export_index=location.export_index,
                    export_name=layout.export_name,
                    bulk_index=layout.bulk_index,
                    reward_index=layout.reward_index,
                    reward_package="/Game/Blueprint/Article/Item/DA_ChangedSource",
                    reward_asset="DA_ChangedSource",
                    count=layout.count,
                    notification=layout.notification,
                    package_name_value=layout.expected_package_name_value,
                    package_name_number=layout.expected_package_name_number,
                    asset_name_value=layout.expected_asset_name_value,
                    asset_name_number=layout.expected_asset_name_number,
                    sub_path_string=layout.expected_sub_path_string,
                    articles_count=layout.expected_articles_count,
                ),)

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            compatible, rejected = filter_compatible_world_loot_quest_grants(
                (location, unrelated),
                game_root=Path(temporary),
                work_root=Path(temporary),
                mappings_path=Path(temporary) / "CV2.usmap",
                game_version="fixture-build",
                data_revision="current",
                extractor=Extractor(Path(temporary) / "extractor"),
                serializer=Serializer(),
            )

        self.assertEqual(compatible, (unrelated,))
        self.assertEqual(rejected, (location,))

    # Keep matching quest task eligible
    def test_matching_quest_task_layout_remains_eligible(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        location = next(
            item for item in catalogs.world_loot
            if item.destination_id == "quest_reward_qst_characterepisode_holly_c05020_48"
        )
        layout = location.quest_grant_layouts[0]

        class Extractor:
            # Extract requested game assets
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                files = []
                for package in virtual_paths:
                    header = Path(temporary) / Path(*package.split("/")[2:]).with_suffix(".uasset")
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes(b"quest source")
                    export = header.with_suffix(".uexp")
                    export.write_bytes(b"quest exports")
                    files.append(ExtractedAsset(package, (header, export)))
                return tuple(files)

        class Serializer:
            # Inspect source quest packages
            def inspect_world_loot_quest_packages(self, *, package_files, **_kwargs):
                return (BloodCodeGrantSnapshot(
                    virtual_package=location.package_path,
                    export_index=location.export_index,
                    export_name=layout.export_name,
                    bulk_index=layout.bulk_index,
                    reward_index=layout.reward_index,
                    reward_package=layout.expected_article_path,
                    reward_asset=layout.expected_article_path.rsplit("/", 1)[-1],
                    count=layout.count,
                    notification=layout.notification,
                    package_name_value=layout.expected_package_name_value,
                    package_name_number=layout.expected_package_name_number,
                    asset_name_value=layout.expected_asset_name_value,
                    asset_name_number=layout.expected_asset_name_number,
                    sub_path_string=layout.expected_sub_path_string,
                    articles_count=layout.expected_articles_count,
                ),)

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            compatible, rejected = filter_compatible_world_loot_quest_grants(
                (location,),
                game_root=Path(temporary),
                work_root=Path(temporary),
                mappings_path=Path(temporary) / "CV2.usmap",
                game_version="fixture-build",
                data_revision="current",
                extractor=Extractor(),
                serializer=Serializer(),
            )

        self.assertEqual(compatible, (location,))
        self.assertEqual(rejected, ())

    # Isolate incompatible quest packages
    def test_quest_layout_mismatch_skips_only_its_package(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        locations = tuple(
            next(item for item in catalogs.world_loot if item.destination_id == destination_id)
            for destination_id in (
                "quest_reward_qst_characterepisode_holly_c05020_48",
                "harmony_moon_holly_reward",
            )
        )
        by_package = {item.package_path: item for item in locations}

        class Extractor:
            # Extract requested quest packages
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                assets = []
                for package in virtual_paths:
                    header = Path(temporary) / Path(*package.split("/")[2:]).with_suffix(".uasset")
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes((package + " header").encode())
                    exports = header.with_suffix(".uexp")
                    exports.write_bytes((package + " exports").encode())
                    assets.append(ExtractedAsset(package, (header, exports)))
                return tuple(assets)

        class Serializer:
            # Isolate installed quest layouts
            def inspect_world_loot_quest_packages(self, *, package_files, **_kwargs):
                if len(package_files) > 1:
                    raise BloodCodeGenerationError(
                        "InvalidDataException: quest package layout differs from catalog."
                    )
                package = next(iter(package_files))
                if package == locations[1].package_path:
                    raise BloodCodeGenerationError(
                        "InvalidDataException: quest export 40 has an incompatible task layout."
                    )
                location = by_package[package]
                layout = location.quest_grant_layouts[0]
                return (BloodCodeGrantSnapshot(
                    virtual_package=location.package_path,
                    export_index=location.export_index,
                    export_name=layout.export_name,
                    bulk_index=layout.bulk_index,
                    reward_index=layout.reward_index,
                    reward_package=layout.expected_article_path,
                    reward_asset=layout.expected_article_path.rsplit("/", 1)[-1],
                    count=layout.count,
                    notification=layout.notification,
                    package_name_value=layout.expected_package_name_value,
                    package_name_number=layout.expected_package_name_number,
                    asset_name_value=layout.expected_asset_name_value,
                    asset_name_number=layout.expected_asset_name_number,
                    sub_path_string=layout.expected_sub_path_string,
                    articles_count=layout.expected_articles_count,
                ),)

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            compatible, rejected = filter_compatible_world_loot_quest_grants(
                locations,
                game_root=Path(temporary),
                work_root=Path(temporary),
                mappings_path=Path(temporary) / "CV2.usmap",
                game_version="local-unverified",
                data_revision="current",
                extractor=Extractor(),
                serializer=Serializer(),
                compatibility_profile="1.0.9",
            )

        self.assertEqual(
            tuple(item.destination_id for item in compatible),
            (locations[0].destination_id,),
        )
        self.assertEqual(rejected, (locations[1],))

    # Rebind moved profile quest exports
    def test_recognized_profile_rebinds_exact_moved_quest_task(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        original = next(
            item for item in catalogs.world_loot
            if item.destination_id == "quest_reward_qst_characterepisode_holly_c05020_48"
        )
        location = replace(original, export_index=48)
        layout = location.quest_grant_layouts[0]

        class Extractor:
            # Extract requested quest assets
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                files = []
                for package in virtual_paths:
                    header = Path(temporary) / Path(*package.split("/")[2:]).with_suffix(".uasset")
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes(b"different-version-header")
                    exports = header.with_suffix(".uexp")
                    exports.write_bytes(b"different-version-exports")
                    files.append(ExtractedAsset(package, (header, exports)))
                return tuple(files)

        class Serializer:
            # Inspect moved quest rewards
            def inspect_world_loot_quest_packages(self, *, package_files, **_kwargs):
                names = ["OtherTask"] * 44
                names[43] = layout.export_name
                return (BloodCodeGrantSnapshot(
                    virtual_package=location.package_path,
                    export_index=44,
                    export_name=layout.export_name,
                    bulk_index=layout.bulk_index,
                    reward_index=layout.reward_index,
                    reward_package=layout.expected_article_path,
                    reward_asset=layout.expected_article_path.rsplit("/", 1)[-1],
                    count=layout.count,
                    notification=layout.notification,
                    package_name_value=layout.expected_package_name_value,
                    package_name_number=layout.expected_package_name_number,
                    asset_name_value=layout.expected_asset_name_value,
                    asset_name_number=layout.expected_asset_name_number,
                    sub_path_string=layout.expected_sub_path_string,
                    articles_count=layout.expected_articles_count,
                    package_export_names=tuple(names),
                ),)

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            compatible, rejected = filter_compatible_world_loot_quest_grants(
                (location,),
                game_root=Path(temporary),
                work_root=Path(temporary),
                mappings_path=Path(temporary) / "CV2.usmap",
                game_version="local-unverified",
                data_revision="current",
                extractor=Extractor(),
                serializer=Serializer(),
                compatibility_profile="1.0.9",
            )

        self.assertEqual(rejected, ())
        self.assertEqual(len(compatible), 1)
        self.assertEqual(compatible[0].export_index, 44)
        self.assertIsNone(compatible[0].quest_grant_layouts[0].source_uasset_sha256)
        self.assertTrue(compatible[0].quest_grant_layouts[0].require_strict_export_snapshots)

    # Preserve Harmony task changes safely
    def test_profile_allows_removed_harmony_task_with_strict_snapshots(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        location = next(
            item for item in catalogs.world_loot
            if item.destination_id == "harmony_moon_holly_reward"
        )
        layout = location.quest_grant_layouts[0]

        class Extractor:
            # Extract requested quest assets
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                files = []
                for package in virtual_paths:
                    header = Path(temporary) / Path(*package.split("/")[2:]).with_suffix(".uasset")
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes(b"different-version-header")
                    exports = header.with_suffix(".uexp")
                    exports.write_bytes(b"different-version-exports")
                    files.append(ExtractedAsset(package, (header, exports)))
                return tuple(files)

        class Serializer:
            # Inspect Harmony reward source
            def inspect_world_loot_quest_packages(self, *, package_files, **_kwargs):
                names = ["OtherTask"] * 40
                names[38] = layout.export_name
                names[39] = "QSTask_GoalLocation_0"
                return (BloodCodeGrantSnapshot(
                    virtual_package=location.package_path,
                    export_index=39,
                    export_name=layout.export_name,
                    bulk_index=layout.bulk_index,
                    reward_index=layout.reward_index,
                    reward_package=layout.expected_article_path,
                    reward_asset=layout.expected_article_path.rsplit("/", 1)[-1],
                    count=layout.count,
                    notification=layout.notification,
                    package_name_value=layout.expected_package_name_value,
                    package_name_number=layout.expected_package_name_number,
                    asset_name_value=layout.expected_asset_name_value,
                    asset_name_number=layout.expected_asset_name_number,
                    sub_path_string=layout.expected_sub_path_string,
                    articles_count=layout.expected_articles_count,
                    package_export_names=tuple(names),
                ),)

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            compatible, rejected = filter_compatible_world_loot_quest_grants(
                (location,),
                game_root=Path(temporary),
                work_root=Path(temporary),
                mappings_path=Path(temporary) / "CV2.usmap",
                game_version="local-unverified",
                data_revision="current",
                extractor=Extractor(),
                serializer=Serializer(),
                compatibility_profile="1.0.9",
            )

        self.assertEqual(rejected, ())
        self.assertEqual(len(compatible), 1)
        self.assertEqual(compatible[0].export_index, 39)
        rebound = compatible[0].quest_grant_layouts[0]
        self.assertIsNone(rebound.preserved_export_index)
        self.assertIsNone(rebound.preserved_export_name)
        self.assertTrue(rebound.require_strict_export_snapshots)

    # Match Iris mirrored rewards
    def test_profile_rebinds_iris_rewards_by_exact_article(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        locations = tuple(
            item for item in catalogs.world_loot
            if item.destination_id in {"iris_reward_1", "iris_reward_2"}
        )
        all_layouts = tuple(layout for item in locations for layout in item.quest_grant_layouts)
        package_path = locations[0].package_path

        class Extractor:
            # Extract requested quest assets
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                files = []
                for package in virtual_paths:
                    header = Path(temporary) / Path(*package.split("/")[2:]).with_suffix(".uasset")
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes(b"different-version-header")
                    exports = header.with_suffix(".uexp")
                    exports.write_bytes(b"different-version-exports")
                    files.append(ExtractedAsset(package, (header, exports)))
                return tuple(files)

        class Serializer:
            # Inspect both quest mirrors
            def inspect_world_loot_quest_packages(self, *, package_files, **_kwargs):
                snapshots = []
                for export_index, export_name in ((39, "QSTask_GainItem_0"), (40, "QSTask_GainItem_1")):
                    names = ["OtherTask"] * 40
                    names[export_index - 1] = export_name
                    for layout in all_layouts:
                        snapshots.append(BloodCodeGrantSnapshot(
                            virtual_package=package_path,
                            export_index=export_index,
                            export_name=export_name,
                            bulk_index=layout.bulk_index,
                            reward_index=layout.reward_index,
                            reward_package=layout.expected_article_path,
                            reward_asset=layout.expected_article_path.rsplit("/", 1)[-1],
                            count=layout.count,
                            notification=layout.notification,
                            package_name_value=layout.expected_package_name_value,
                            package_name_number=layout.expected_package_name_number,
                            asset_name_value=layout.expected_asset_name_value,
                            asset_name_number=layout.expected_asset_name_number,
                            sub_path_string=layout.expected_sub_path_string,
                            articles_count=layout.expected_articles_count,
                            package_export_names=tuple(names),
                        ))
                return tuple(snapshots)

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            compatible, rejected = filter_compatible_world_loot_quest_grants(
                locations,
                game_root=Path(temporary),
                work_root=Path(temporary),
                mappings_path=Path(temporary) / "CV2.usmap",
                game_version="local-unverified",
                data_revision="current",
                extractor=Extractor(),
                serializer=Serializer(),
                compatibility_profile="1.0.9",
            )

        self.assertEqual(rejected, ())
        self.assertEqual(len(compatible), 2)
        by_id = {item.destination_id: item for item in compatible}
        first = by_id["iris_reward_1"]
        second = by_id["iris_reward_2"]
        self.assertEqual(first.export_index, 39)
        self.assertEqual(second.export_index, 40)
        rebound = first.quest_grant_layouts[0]
        self.assertEqual(rebound.export_name, "QSTask_GainItem_0")
        self.assertEqual(
            (rebound.mirrored_exports[0].export_index, rebound.mirrored_exports[0].export_name),
            (40, "QSTask_GainItem_1"),
        )
        self.assertTrue(rebound.require_strict_export_snapshots)
        rebound_second = second.quest_grant_layouts[0]
        self.assertEqual(rebound_second.export_name, "QSTask_GainItem_1")
        self.assertEqual(
            (rebound_second.mirrored_exports[0].export_index, rebound_second.mirrored_exports[0].export_name),
            (39, "QSTask_GainItem_0"),
        )

    # Skip changed preserved quest tasks
    def test_preserved_quest_task_mismatch_is_filtered_before_planning(self) -> None:
        from cv2randomizer.game.asset_extraction import ExtractedAsset

        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        location = next(
            item for item in catalogs.world_loot
            if item.destination_id == "harmony_moon_holly_reward"
        )
        layout = location.quest_grant_layouts[0]

        class Extractor:
            # Extract requested quest packages
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                results = []
                for package in virtual_paths:
                    header = Path(temporary) / Path(*package.split("/")[2:]).with_suffix(".uasset")
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes(b"quest source")
                    export = header.with_suffix(".uexp")
                    export.write_bytes(b"quest exports")
                    results.append(ExtractedAsset(package, (header, export)))
                return tuple(results)

        class Serializer:
            # Inspect source quest packages
            def __init__(self, preserved_export_name: str) -> None:
                self.preserved_export_name = preserved_export_name

            def inspect_world_loot_quest_packages(self, *, package_files, **_kwargs):
                export_names = ["OtherTask"] * 40
                export_names[location.export_index - 1] = layout.export_name
                export_names[layout.preserved_export_index - 1] = self.preserved_export_name
                return (BloodCodeGrantSnapshot(
                    virtual_package=location.package_path,
                    export_index=location.export_index,
                    export_name=layout.export_name,
                    bulk_index=layout.bulk_index,
                    reward_index=layout.reward_index,
                    reward_package=layout.expected_article_path,
                    reward_asset=layout.expected_article_path.rsplit("/", 1)[-1],
                    count=layout.count,
                    notification=layout.notification,
                    package_name_value=layout.expected_package_name_value,
                    package_name_number=layout.expected_package_name_number,
                    asset_name_value=layout.expected_asset_name_value,
                    asset_name_number=layout.expected_asset_name_number,
                    sub_path_string=layout.expected_sub_path_string,
                    articles_count=layout.expected_articles_count,
                    package_export_names=tuple(export_names),
                ),)

        with tempfile.TemporaryDirectory(dir=ROOT / "Delete") as temporary:
            compatible, rejected = filter_compatible_world_loot_quest_grants(
                (location,),
                game_root=Path(temporary),
                work_root=Path(temporary),
                mappings_path=Path(temporary) / "CV2.usmap",
                game_version="fixture-build",
                data_revision="current",
                extractor=Extractor(),
                serializer=Serializer("QSTask_GoalLocation_0"),
            )

            self.assertEqual(compatible, ())
            self.assertEqual(rejected, (location,))

            compatible, rejected = filter_compatible_world_loot_quest_grants(
                (location,),
                game_root=Path(temporary),
                work_root=Path(temporary),
                mappings_path=Path(temporary) / "CV2.usmap",
                game_version="fixture-build",
                data_revision="current",
                extractor=Extractor(),
                serializer=Serializer(layout.preserved_export_name),
            )

        self.assertEqual(compatible, (location,))
        self.assertEqual(rejected, ())


@unittest.skipUnless(shutil.which("dotnet"), "dotnet SDK is not available")
class DirectRawSerializerFixtureTests(unittest.TestCase):
    # Check all slot definitions share literal
    def test_all_slot_definitions_share_literal_name_and_zero_number_serializer(self) -> None:
        result = subprocess.run(
            [
                "dotnet", "run", "--project", str(ROOT / "tools" / "uassetapi-worldloot" / "UAssetAPIWorldLoot.csproj"),
                "--no-restore", "--", "test-raw-article-grant-fixtures",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 9)
        self.assertEqual({row["destinationId"] for row in rows}, set(SOURCES))
        vanilla_numbers = {
            "jadwiga_servant_lightning_barrage_reward": 0,
            "jadwiga_servant_booster_dex_reward": 2,
            "jadwiga_servant_initial_parry_reward": 2,
        }
        for destination_id in SOURCES:
            slot_rows = [row for row in rows if row["destinationId"] == destination_id]
            self.assertEqual([row["requestedPath"] for row in slot_rows], list(ARTICLE_CASES))
            for row in slot_rows:
                self.assertEqual(row["vanillaNumber"], vanilla_numbers[destination_id])
                requested = row["requestedPath"]
                object_name = requested.rsplit("/", 1)[-1]
                self.assertEqual(row["nameMapPath"], requested)
                self.assertEqual(row["nameMapObject"], object_name)
                self.assertEqual((row["pathNumber"], row["objectNumber"]), (0, 0))
                self.assertTrue(row["unrelatedRawBytesPreserved"])


@unittest.skipUnless(all(path.is_file() for path in SOURCES.values()) and MAPPINGS.is_file(), "local direct reward research assets and CV2.usmap are not available")
class DirectRawSerializerRealAssetTests(unittest.TestCase):
    # Check all slots accept generic article
    def test_all_slots_accept_generic_article_paths_and_preserve_their_sources(self) -> None:
        slots = {
            location.destination_id: location
            for location in CatalogStore(ROOT / "data" / "catalogs").load("current").world_loot
            if location.destination_id in SOURCES
        }
        serializer = RawArticleGrantSerializer()
        with tempfile.TemporaryDirectory(prefix="cv2-direct-raw-article-") as temporary:
            root = Path(temporary).resolve()
            for destination_id, source in SOURCES.items():
                slot = slots[destination_id]
                source_members = tuple(
                    path for path in (source, source.with_suffix(".uexp"), source.with_suffix(".ubulk"), source.with_suffix(".uptnl"))
                    if path.is_file()
                )
                original_hashes = {path.suffix: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_members}
                relative = Path(*slot.package_path.split("/")[2:]).with_suffix(".uasset")
                source_copy = root / "source" / destination_id / "Content" / relative
                source_copy.parent.mkdir(parents=True)
                for original in source_members:
                    shutil.copyfile(original, source_copy.with_suffix(original.suffix))
                before = serializer.inspect(
                    work_root=root,
                    mappings_path=MAPPINGS,
                    slot=slot,
                    input_file=source_copy,
                )
                self.assertEqual(before.export_index, 2)
                self.assertEqual(before.serial_size, 55)
                self.assertEqual(before.article_path, slot.raw_article_layout.expected_vanilla_article_path)
                self.assertEqual((before.path_name_index, before.object_name_index), (0, 27))
                expected_vanilla_number = slot.raw_article_layout.expected_vanilla_number
                self.assertEqual((before.path_number, before.object_number), (expected_vanilla_number, expected_vanilla_number))

                for index, article_path in enumerate(ARTICLE_CASES):
                    output = root / "staging" / destination_id / str(index) / "Content" / relative
                    output.parent.mkdir(parents=True)
                    patched, files = serializer.patch(
                        work_root=root,
                        mappings_path=MAPPINGS,
                        slot=slot,
                        input_file=source_copy,
                        output_file=output,
                        assigned_article_path=article_path,
                    )
                    self.assertTrue(output.is_file())
                    self.assertTrue(output.with_suffix(".uexp").is_file())
                    self.assertEqual(patched.article_path, article_path)
                    self.assertEqual(patched.path_name_map_value, article_path)
                    self.assertEqual(patched.object_name_map_value, article_path.rsplit("/", 1)[-1])
                    self.assertEqual((patched.path_number, patched.object_number), (0, 0))
                    self.assertTrue(all(path.is_file() for path in files))
                self.assertEqual(
                    {path.suffix: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_members},
                    original_hashes,
                )

    # Check each slot reject source pair
    def test_each_slot_rejects_a_source_pair_with_a_changed_fingerprint(self) -> None:
        slots = {
            location.destination_id: location
            for location in CatalogStore(ROOT / "data" / "catalogs").load("current").world_loot
            if location.destination_id in SOURCES
        }
        serializer = RawArticleGrantSerializer()
        with tempfile.TemporaryDirectory(prefix="cv2-direct-fingerprint-") as temporary:
            root = Path(temporary).resolve()
            for destination_id, source in SOURCES.items():
                slot = slots[destination_id]
                relative = Path(*slot.package_path.split("/")[2:]).with_suffix(".uasset")
                source_copy = root / destination_id / "Content" / relative
                source_copy.parent.mkdir(parents=True)
                shutil.copyfile(source, source_copy)
                shutil.copyfile(source.with_suffix(".uexp"), source_copy.with_suffix(".uexp"))
                with source_copy.open("ab") as handle:
                    handle.write(b"changed")
                with self.assertRaises(RawArticleGrantGenerationError):
                    serializer.inspect(
                        work_root=root,
                        mappings_path=MAPPINGS,
                        slot=slot,
                        input_file=source_copy,
                    )


if __name__ == "__main__":
    unittest.main()
