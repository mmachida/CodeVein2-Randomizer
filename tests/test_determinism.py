from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from cv2randomizer.core.determinism import (
    RANDOMIZER_ALGORITHM_VERSION,
    StableRandom,
    canonical_package_identity,
    canonical_sort_key,
)
from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.core.seed import normalize_seed
from cv2randomizer.game.catalogs import RewardPayload, WorldLootLocation
from cv2randomizer.game.content_availability import (
    AssetAvailabilityState,
    ContentAvailability,
    SourceAssetAvailability,
)
from cv2randomizer.generation.reproducibility import build_reproducibility_metadata
from cv2randomizer.generation.spoiler import SpoilerEntry, write_spoiler
from cv2randomizer.randomizer.world_loot import (
    WorldLootRewardCandidate,
    build_world_loot_randomization_plan,
)


class StableRandomTests(unittest.TestCase):
    # Check algorithm vector is fixed
    def test_algorithm_vector_is_fixed(self) -> None:
        self.assertEqual(RANDOMIZER_ALGORITHM_VERSION, 3)
        rng = StableRandom.from_material("vector-test")
        values = list(range(10))
        rng.shuffle(values)
        self.assertEqual(values, [5, 6, 8, 4, 3, 7, 0, 9, 2, 1])
        self.assertEqual(
            StableRandom.from_material("vector-test").sample(list(range(10)), 5),
            [1, 3, 0, 2, 5],
        )

    # Check fresh processes ignore python hash
    def test_fresh_processes_ignore_python_hash_seed(self) -> None:
        script = (
            "from cv2randomizer.core.determinism import StableRandom; "
            "r=StableRandom.from_material('fresh-process'); "
            "items=sorted({'delta','Alpha','bravo','CHARLIE'}, key=lambda x:(x.casefold(),x)); "
            "r.shuffle(items); print(','.join(items))"
        )
        outputs = []
        for hash_seed in ("1", "987654"):
            env = dict(os.environ, PYTHONHASHSEED=hash_seed)
            outputs.append(subprocess.check_output(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
            ).strip())
        self.assertEqual(outputs[0], outputs[1])

    # Check canonical sort has explicit case
    def test_canonical_sort_has_an_explicit_case_tie_break(self) -> None:
        self.assertEqual(sorted(("a", "A", "b"), key=canonical_sort_key), ["A", "a", "b"])
        self.assertEqual(
            canonical_package_identity("\\Game\\Café\\DA_Item"),
            canonical_package_identity("/game/cafe\u0301/da_item"),
        )


class ReproducibilityFingerprintTests(unittest.TestCase):
    # Check retired pipe shop test setting
    def test_retired_pipe_shop_test_setting_is_ignored(self) -> None:
        shared = dict(
            seed=normalize_seed(271828), game_version="test-build", catalog_revision="current",
            content_availability=None, source_cache_metadata=None,
            world_plan=SimpleNamespace(assignments=()),
            shop_plan=SimpleNamespace(assignments=(), linked_assignments=(), npc_traders=None),
        )
        vanilla = build_reproducibility_metadata(options=GenerationOptions(), **shared)
        legacy_settings = build_reproducibility_metadata(
            options=GenerationOptions.from_dict({"test_unidentified_shop_materials": True}), **shared,
        )
        self.assertEqual(vanilla.config_fingerprint, legacy_settings.config_fingerprint)
        self.assertEqual(vanilla.document["assignments"], legacy_settings.document["assignments"])

    # Check blood code mastery setting changes
    def test_blood_code_mastery_setting_changes_config_without_changing_assignments(self) -> None:
        shared = dict(
            seed=normalize_seed(13579), game_version="test-build", catalog_revision="current",
            content_availability=None, source_cache_metadata=None,
            world_plan=SimpleNamespace(assignments=()),
            shop_plan=SimpleNamespace(assignments=(), npc_traders=None),
        )
        vanilla = build_reproducibility_metadata(options=GenerationOptions(), **shared)
        test_mode = build_reproducibility_metadata(
            options=GenerationOptions(low_blood_code_mastery_exp=True), **shared,
        )
        self.assertNotEqual(vanilla.config_fingerprint, test_mode.config_fingerprint)
        self.assertEqual(vanilla.document["assignments"], test_mode.document["assignments"])

    # Check cheaper booster extractor changes config
    def test_cheaper_booster_extractor_changes_config_without_changing_assignments(self) -> None:
        shared = dict(
            seed=normalize_seed(12345), game_version="test-build", catalog_revision="current",
            content_availability=None, source_cache_metadata=None,
            world_plan=SimpleNamespace(assignments=()),
            shop_plan=SimpleNamespace(assignments=(), npc_traders=None),
        )
        vanilla = build_reproducibility_metadata(options=GenerationOptions(), **shared)
        test_mode = build_reproducibility_metadata(
            options=GenerationOptions(cheaper_booster_extractor=True), **shared,
        )
        self.assertNotEqual(vanilla.config_fingerprint, test_mode.config_fingerprint)
        self.assertEqual(vanilla.document["assignments"], test_mode.document["assignments"])

    # Check linked jail manifest points correct
    def test_linked_jail_manifest_points_to_the_correct_canonical_route(self) -> None:
        expected = {
            "LINKED_WORLD_REWARD": "Josee's Outpost Stinger World Loot reward",
            "LINKED_INITIAL_INVENTORY": "Randomized starting Jail in Initial Inventory",
        }
        for origin_kind, linked_to in expected.items():
            with self.subTest(origin_kind=origin_kind):
                source = SimpleNamespace(
                    origin_kind=origin_kind,
                    canonical_path="/Game/Blueprint/Article/Jail/DA_Jail_Hounds",
                )
                destination = SimpleNamespace(
                    family="NORMAL",
                    shop_row="Shop_Jadwiga_Weapon",
                    product_index=29,
                    article_path="/Game/Blueprint/Article/Jail/DA_Jail_Ogre",
                    article_path_number=0,
                )
                shop_plan = SimpleNamespace(
                    assignments=(),
                    npc_traders=None,
                    linked_assignments=(SimpleNamespace(destination=destination, source=source),),
                )
                metadata = build_reproducibility_metadata(
                    seed=normalize_seed(12345),
                    game_version="test-build",
                    catalog_revision="current",
                    options=GenerationOptions(),
                    content_availability=None,
                    source_cache_metadata=None,
                    world_plan=SimpleNamespace(assignments=()),
                    shop_plan=shop_plan,
                )
                record = metadata.document["assignments"][0]
                self.assertEqual(record["linked_to"], linked_to)

    # Check install cache paths do not
    def test_install_and_cache_paths_do_not_affect_fingerprints(self) -> None:
        location = _world_location(0)
        world_plan = build_world_loot_randomization_plan((location, _world_location(1)), 987)
        shop_plan = SimpleNamespace(assignments=(), npc_traders=None)
        base_cache = {
            "cache_format_version": 2,
            "game_version": "test-build",
            "game_executable_sha256": "e" * 64,
            "game_archive_sha256": "a" * 64,
            "catalog_revision": "current",
            "extracted_at_utc": "2026-01-01T00:00:00Z",
            "game_install_path": "C:/Games/CodeVein2",
            "extracted_package_inventory": ["/Game/Test/Package", "/Game/Test/Other"],
            "package_files": {
                "/Game/Test/Package": [
                    {"name": "Package.uasset", "size": 10, "sha256": "b" * 64},
                ],
                "/Game/Test/Other": [
                    {"name": "Other.uexp", "size": 20, "sha256": "c" * 64},
                    {"name": "Other.uasset", "size": 30, "sha256": "d" * 64},
                ],
            },
        }
        other_cache = {
            **base_cache,
            "game_install_path": "D:/Steam/CodeVein2",
            "extracted_at_utc": "2026-05-05T05:05:05Z",
            "extracted_package_inventory": list(reversed(base_cache["extracted_package_inventory"])),
            "package_files": {
                key: list(reversed(files))
                for key, files in reversed(tuple(base_cache["package_files"].items()))
            },
        }
        availability = ContentAvailability((
            SourceAssetAvailability("shops", "/Game/Test/Package", AssetAvailabilityState.AVAILABLE),
            SourceAssetAvailability("world_loot", "/Game/Test/Other", AssetAvailabilityState.MISSING),
        ))
        reordered_availability = ContentAvailability(tuple(reversed(availability.sources)))
        first = build_reproducibility_metadata(
            seed=normalize_seed("000987"), game_version="test-build", catalog_revision="current",
            options=GenerationOptions(install_after_generation=True), content_availability=availability,
            source_cache_metadata=base_cache, world_plan=world_plan, shop_plan=shop_plan,
        )
        second = build_reproducibility_metadata(
            seed=normalize_seed(987), game_version="test-build", catalog_revision="current",
            options=GenerationOptions(install_after_generation=False), content_availability=reordered_availability,
            source_cache_metadata=other_cache, world_plan=world_plan, shop_plan=shop_plan,
        )
        self.assertEqual(first.config_fingerprint, second.config_fingerprint)
        self.assertEqual(first.content_fingerprint, second.content_fingerprint)
        self.assertEqual(first.assignment_manifest_sha256, second.assignment_manifest_sha256)
        self.assertEqual(first.document["seed"], "987")

    # Check ten repeated runs have identical
    def test_ten_repeated_runs_have_identical_assignment_and_spoiler_hashes(self) -> None:
        location = _world_location(0)
        world_plan = build_world_loot_randomization_plan((location, _world_location(1)), 9088670713407755301)
        shop_plan = SimpleNamespace(assignments=(), npc_traders=None)
        observed: set[tuple[str, str]] = set()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for run in range(10):
                metadata = build_reproducibility_metadata(
                    seed=normalize_seed("9088670713407755301"),
                    game_version="same-game-build",
                    catalog_revision="current",
                    options=GenerationOptions(randomize_healing=True),
                    content_availability=None,
                    source_cache_metadata={
                        "game_install_path": f"{output / f'install-{run}'}",
                        "cache_root": f"{output / f'cache-{run}' }",
                        "extracted_at_utc": f"2026-10-06T00:00:{run:02d}Z",
                        "game_executable_sha256": "e" * 64,
                        "extracted_package_inventory": ["/Game/Test/Map0", "/Game/Test/Map1"],
                    },
                    world_plan=world_plan,
                    shop_plan=shop_plan,
                )
                spoiler = output / f"spoiler-{run}.txt"
                write_spoiler(
                    spoiler,
                    (
                        SpoilerEntry("World Loot", "/Game/Item/B", "/Game/Item/C", destination="Map B"),
                        SpoilerEntry("World Loot", "/Game/Item/A", "/Game/Item/B", destination="Map A"),
                    ),
                    seed="9088670713407755301",
                    metadata=dict(metadata.spoiler_header()),
                )
                observed.add((metadata.assignment_manifest_sha256, hashlib.sha256(spoiler.read_bytes()).hexdigest()))
        self.assertEqual(len(observed), 1)

    # Check fresh processes distinct cache roots
    def test_fresh_processes_and_distinct_cache_roots_match_full_plan_hashes(self) -> None:
        script = r'''
import hashlib, json, sys
from pathlib import Path
from types import SimpleNamespace
from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.core.seed import normalize_seed
from cv2randomizer.game.catalogs import RewardPayload, WorldLootLocation
from cv2randomizer.generation.reproducibility import build_reproducibility_metadata, write_assignment_manifest
from cv2randomizer.generation.spoiler import SpoilerEntry, write_spoiler
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan

root = Path(sys.argv[1])
install, cache, output = root / 'game-root', root / 'cache-root', root / 'output-root'
install.mkdir(parents=True)
cache.mkdir(parents=True)
asset = cache / 'same-logical-package.uasset'
asset.write_bytes(b'identical logical asset bytes')
digest = hashlib.sha256(asset.read_bytes()).hexdigest()
locations = tuple(WorldLootLocation(
    package_path=f'/Game/Map/Test/Map{i}', source_file=f'Map{i}.umap', export_index=1,
    export_name=f'Pickup{i}', actor_class='BP_PickupItem_C', actor_category='GROUND_PICKUP',
    save_id=f'save-{i}', protected=False, protection_reasons=(),
    rewards=(RewardPayload(f'/Game/Item/Vanilla{i}', 1, True),),
) for i in range(4))
if root.name.endswith('1'):
    locations = tuple(reversed(locations))
plan = build_world_loot_randomization_plan(locations, 9088670713407755301)
metadata = build_reproducibility_metadata(
    seed=normalize_seed(9088670713407755301), game_version='same-game-build',
    catalog_revision='current', options=GenerationOptions(randomize_healing=True),
    content_availability=None,
    source_cache_metadata={
        'game_install_path': str(install), 'cache_root': str(cache),
        'extracted_at_utc': '2026-10-06T00:00:00Z',
        'game_executable_sha256': 'e' * 64,
        'extracted_package_inventory': (
            ['/Game/Test/LogicalPackage', '/Game/Test/Other']
            if root.name.endswith('0') else ['/Game/Test/Other', '/Game/Test/LogicalPackage']
        ),
        'package_files': (
            {
                '/Game/Test/LogicalPackage': [
                    {'name': 'LogicalPackage.uasset', 'size': asset.stat().st_size, 'sha256': digest}
                ],
                '/Game/Test/Other': [
                    {'name': 'Other.uexp', 'size': 12, 'sha256': 'c' * 64}
                ],
            }
            if root.name.endswith('0') else {
                '/Game/Test/Other': [
                    {'name': 'Other.uexp', 'size': 12, 'sha256': 'c' * 64}
                ],
                '/Game/Test/LogicalPackage': [
                    {'name': 'LogicalPackage.uasset', 'size': asset.stat().st_size, 'sha256': digest}
                ],
            }
        ),
    }, world_plan=plan, shop_plan=SimpleNamespace(assignments=(), npc_traders=None),
)
output.mkdir()
manifest_path = write_assignment_manifest(output / 'assignment_manifest.json', metadata)
spoiler_path = write_spoiler(
    output / 'Spoiler.txt',
    (
        SpoilerEntry('World Loot', '/Game/Item/A', '/Game/Item/B', destination='Map A'),
        SpoilerEntry('Shops', '/Game/Item/C', '/Game/Item/D', destination='Shop C'),
    ) if root.name.endswith('0') else (
        SpoilerEntry('Shops', '/Game/Item/C', '/Game/Item/D', destination='Shop C'),
        SpoilerEntry('World Loot', '/Game/Item/A', '/Game/Item/B', destination='Map A'),
    ),
    seed='9088670713407755301', metadata=dict(metadata.spoiler_header()),
)
print(json.dumps({
    'assignment': metadata.assignment_manifest_sha256,
    'manifest_bytes': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    'spoiler': hashlib.sha256(spoiler_path.read_bytes()).hexdigest(),
}))
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs = []
            for index, hash_seed in enumerate(("1", "987654")):
                env = dict(os.environ, PYTHONHASHSEED=hash_seed)
                outputs.append(subprocess.check_output(
                    [sys.executable, "-c", script, str(root / f"environment-{index}")],
                    cwd=Path(__file__).resolve().parents[1],
                    env=env,
                    text=True,
                ).strip())
        self.assertEqual(json.loads(outputs[0]), json.loads(outputs[1]))


# Find world location
def _world_location(index: int) -> WorldLootLocation:
    return WorldLootLocation(
        package_path=f"/Game/Map/Test/Map{index}",
        source_file=f"Map{index}.umap",
        export_index=1,
        export_name=f"Pickup{index}",
        actor_class="BP_PickupItem_C",
        actor_category="GROUND_PICKUP",
        save_id=f"save-{index}",
        protected=False,
        protection_reasons=(),
        rewards=(RewardPayload(f"/Game/Item/Vanilla{index}", 1, True),),
    )


if __name__ == "__main__":
    unittest.main()
