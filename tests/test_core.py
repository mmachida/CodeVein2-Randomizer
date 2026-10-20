import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from cv2randomizer.core.config import SettingsError, SettingsStore
from cv2randomizer.core.models import AppSettings, GenerationOptions
from cv2randomizer.core.seed import normalize_seed
from cv2randomizer.core.workspace import (
    CacheManager,
    WorkspaceManager,
    _is_randomizer_spoiler_copy,
    clear_output_directory,
)


class SeedTests(unittest.TestCase):
    # Check integer seed spellings are reproducible
    def test_integer_seed_spellings_are_reproducible(self) -> None:
        self.assertEqual(normalize_seed("0042").internal_value, normalize_seed(42).internal_value)
        self.assertEqual(normalize_seed("  -7 ").internal_value, normalize_seed(-7).internal_value)

    # Check text integer seeds use distinct
    def test_text_and_integer_seeds_use_distinct_domains(self) -> None:
        self.assertNotEqual(normalize_seed("42").internal_value, normalize_seed("text:42").internal_value)
        self.assertEqual(normalize_seed("hello").internal_value, normalize_seed("hello").internal_value)

    # Check blank none generate reproducible display
    def test_blank_and_none_generate_reproducible_display_seeds(self) -> None:
        blank = normalize_seed(" ")
        missing = normalize_seed(None)

        self.assertTrue(blank.generated)
        self.assertTrue(missing.generated)
        self.assertEqual(normalize_seed(blank.display_value).internal_value, blank.internal_value)
        self.assertEqual(normalize_seed(missing.display_value).internal_value, missing.internal_value)

    # Check boolean is not integer seed
    def test_boolean_is_not_an_integer_seed(self) -> None:
        with self.assertRaises(TypeError):
            normalize_seed(True)


class SettingsAndWorkspaceTests(unittest.TestCase):
    # Check materials option round trips settings
    def test_materials_option_round_trips_from_settings(self) -> None:
        self.assertFalse(GenerationOptions().randomize_materials)
        self.assertTrue(GenerationOptions(randomize_materials=True).randomize_materials)
        self.assertTrue(GenerationOptions.from_dict({"randomize_materials": True}).randomize_materials)

    # Check mandatory options override retired saved
    def test_mandatory_options_override_retired_saved_optouts(self) -> None:
        self.assertTrue(GenerationOptions().randomize_bequeathed_formae_quest_rewards)
        older_options = GenerationOptions.from_dict({"randomize_world_loot": False})
        self.assertFalse(hasattr(older_options, "randomize_healing_upgrades"))
        self.assertTrue(older_options.randomize_world_loot)
        self.assertTrue(older_options.randomize_bequeathed_formae_quest_rewards)
        self.assertTrue(older_options.randomize_healing)
        self.assertFalse(older_options.randomize_extractor_recipes)
        self.assertFalse(older_options.cheaper_booster_extractor)
        self.assertFalse(older_options.low_blood_code_mastery_exp)
        self.assertFalse(hasattr(older_options, "disable_preorder_weapons"))
        self.assertTrue(GenerationOptions.from_dict({
            "free_booster_extraction": True,
        }).to_dict()["cheaper_booster_extractor"])
        self.assertTrue(GenerationOptions.from_dict({
            "booster_material_cost_test": True,
        }).cheaper_booster_extractor)
        self.assertFalse(GenerationOptions.from_dict({
            "free_booster_extraction": True,
            "cheaper_booster_extractor": False,
        }).cheaper_booster_extractor)
        self.assertTrue(older_options.randomize_npc_traders)
        self.assertTrue(older_options.randomize_defensive_gadget_npc_rewards)
        self.assertTrue(older_options.mix_postgame_dlc_with_base)
        self.assertTrue(older_options.shops_normal and older_options.shops_luxury and older_options.shops_trade)
        self.assertEqual(older_options.npc_traders_mode, "only")
        legacy = GenerationOptions.from_dict({"include_dlc_content": True})
        self.assertNotIn("include_dlc_content", legacy.to_dict())
        enabled = GenerationOptions(randomize_extractor_recipes=True, randomize_healing=False)
        restored = GenerationOptions.from_dict(enabled.to_dict())
        self.assertTrue(restored.randomize_world_loot)
        self.assertTrue(restored.randomize_bequeathed_formae_quest_rewards)
        self.assertFalse(restored.randomize_healing)
        self.assertTrue(restored.randomize_extractor_recipes)
        legacy_healing_off = GenerationOptions.from_dict({
            "randomize_world_loot": True,
            "randomize_healing_upgrades": False,
        })
        self.assertTrue(legacy_healing_off.randomize_world_loot)
        self.assertTrue(legacy_healing_off.randomize_healing)
        self.assertNotIn("randomize_healing_upgrades", legacy_healing_off.to_dict())
        trader_options = GenerationOptions(
            randomize_npc_traders=True,
            npc_traders_mode="mixed",
            randomize_bequeathed_formae_quest_rewards=True,
            unlock_npc_trader_shops=True,
            unlock_all_booster_slots=True,
            second_weapon_unlocked=True,
            disable_tutorials=True,
            cheaper_booster_extractor=True,
            low_blood_code_mastery_exp=True,
        )
        self.assertEqual(GenerationOptions.from_dict(trader_options.to_dict()), trader_options)
        self.assertFalse(GenerationOptions().unlock_npc_trader_shops)
        self.assertTrue(GenerationOptions.from_dict({"second_weapon_unlocked": True}).second_weapon_unlocked)
        self.assertTrue(GenerationOptions.from_dict({"low_blood_code_mastery_exp": True}).low_blood_code_mastery_exp)
        retired_shop_test = GenerationOptions.from_dict({"test_unidentified_shop_materials": True})
        self.assertFalse(hasattr(retired_shop_test, "test_unidentified_shop_materials"))
        self.assertNotIn("test_unidentified_shop_materials", retired_shop_test.to_dict())
        self.assertFalse(hasattr(
            GenerationOptions.from_dict({"disable_preorder_weapons": True}),
            "disable_preorder_weapons",
        ))
        self.assertEqual(GenerationOptions(npc_traders_mode="invalid").npc_traders_mode, "only")
        self.assertEqual(GenerationOptions.from_dict({"npc_trader_mode": "invalid"}).npc_traders_mode, "only")
        self.assertNotIn("include_dlc_content", restored.to_dict())

    # Check settings store round trips options
    def test_settings_store_round_trips_options_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config" / "settings.json"
            store = SettingsStore(path)
            settings = AppSettings(
                game_path="C:/Games/CodeVein2",
                last_seed="blue rose",
                output_dir="C:/Mods",
                motorcycle_unlocked=True,
                second_weapon_unlocked=True,
                last_options=GenerationOptions(
                    randomize_shops=False,
                    randomize_bequeathed_formae_quest_rewards=True,
                    tap_to_skip_dialogue=True,
                ),
            )

            store.save(settings)

            self.assertEqual(store.load(), settings)
            self.assertEqual(list(path.parent.iterdir()), [path])

    # Check legacy second weapon placeholder setting
    def test_legacy_second_weapon_placeholder_setting_migrates_to_generation_options(self) -> None:
        restored = AppSettings.from_dict({
            "second_weapon_unlocked": True,
            "last_options": {},
        })

        self.assertTrue(restored.second_weapon_unlocked)
        self.assertTrue(restored.last_options.second_weapon_unlocked)

    # Check invalid settings fail closed
    def test_invalid_settings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text('{"last_options":{"randomize_shops":"yes"}}', encoding="utf-8")

            with self.assertRaises(SettingsError):
                SettingsStore(path).load()

            path.write_text(
                '{"mappings_path":7,"last_options":{"mix_postgame_dlc_with_base":false,'
                '"randomize_defensive_gadget_npc_rewards":false,"shop_normal":false}}',
                encoding="utf-8",
            )
            migrated = SettingsStore(path).load()
            self.assertIsNone(migrated.mappings_path)
            self.assertTrue(migrated.last_options.mix_postgame_dlc_with_base)
            self.assertTrue(migrated.last_options.randomize_defensive_gadget_npc_rewards)
            self.assertNotIn("mix_postgame_dlc_with_base", migrated.last_options.to_dict())
            self.assertNotIn("mappings_path", migrated.to_dict())

    # Check generation workspace is unique complete
    def test_generation_workspace_is_unique_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = WorkspaceManager(Path(temporary))
            timestamp = datetime(2026, 9, 25, tzinfo=UTC)
            first = manager.create_generation(normalize_seed(17), timestamp=timestamp)
            second = manager.create_generation(normalize_seed(17), timestamp=timestamp)

            self.assertNotEqual(first.root, second.root)
            for directory in (first.extracted, first.staging, first.packaged, first.logs):
                self.assertTrue(directory.is_dir())
            self.assertEqual(first.manifest, first.root / "manifest.json")

    # Check clear output directory removes contents
    def test_clear_output_directory_removes_contents_and_preserves_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app_data = Path(temporary) / "app-data"
            output = app_data / "output"
            nested = output / "generated" / "old-run" / "logs"
            nested.mkdir(parents=True)
            (nested / "run.log").write_text("old log", encoding="utf-8")
            (output / "assignment_manifest.json").write_text("{}", encoding="utf-8")
            settings = app_data / "settings.json"
            settings.write_text("{}", encoding="utf-8")
            cache_file = app_data / "Cache" / "READY" / "index.json"
            cache_file.parent.mkdir(parents=True)
            cache_file.write_text("cached data", encoding="utf-8")
            outside = Path(temporary) / "user-data"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep", encoding="utf-8")
            try:
                (output / "external-link").symlink_to(outside, target_is_directory=True)
            except OSError:
                pass  # Creating symlinks may require elevated privileges on Windows.

            removed = clear_output_directory(
                output,
                app_data_root=app_data,
                protected_paths=(settings, cache_file),
            )

            self.assertGreater(removed, 0)
            self.assertTrue(output.is_dir())
            self.assertEqual(list(output.iterdir()), [])
            self.assertEqual(settings.read_text(encoding="utf-8"), "{}")
            self.assertEqual(cache_file.read_text(encoding="utf-8"), "cached data")
            self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep")

    # Check clear output directory reject unsafe
    def test_clear_output_directory_rejects_unsafe_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app_data = Path(temporary) / "app-data"
            app_data.mkdir()
            with self.assertRaises(ValueError):
                clear_output_directory(app_data, app_data_root=app_data)
            with self.assertRaises(ValueError):
                clear_output_directory(Path(temporary), app_data_root=app_data)

            protected_output = app_data / "custom-output"
            protected_output.mkdir()
            settings = protected_output / "settings.json"
            settings.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                clear_output_directory(
                    protected_output,
                    app_data_root=app_data,
                    protected_paths=(settings,),
                )

    # Check successful workspace cleanup keep spoiler
    def test_successful_workspace_cleanup_keeps_spoiler_logs_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = WorkspaceManager(root / "output")
            cache_asset = manager.root / "extracted" / "READY" / "cached.uasset"
            cache_asset.parent.mkdir(parents=True)
            cache_asset.write_bytes(b"vanilla cache stays")
            current = manager.create_generation(normalize_seed("current"))
            old = manager.create_generation(normalize_seed("old"))
            current_payload = current.staging / "Content" / "changed.umap"
            current_payload.parent.mkdir(parents=True)
            current_payload.write_bytes(b"staged asset" * 17)
            (old.packaged / "CV2Randomizer_P.ucas").write_bytes(b"temporary container" * 23)
            current_log = current.logs / "generation_failure.txt"
            current_log.write_text("current run log\n", encoding="utf-8")
            old_log = old.logs / "retoc_timing.json"
            old_log.write_text('{"old":"run log"}\n', encoding="utf-8")
            (current.packaged / "Spoiler.txt").write_text(
                "Created: 2026-10-10\nTotal Randomized: 1 / Unchanged: 0 "
                "(same item after randomized)\nApp Version: v0.9.1\nGame Version: fixture\n"
                "Randomizer Settings:\nSeed: current\nEnabled Initial Inventory\n\n"
                "No randomized replacements.\n",
                encoding="utf-8",
            )
            old_spoiler = manager.root / "Spoiler.txt"
            old_spoiler.write_text(
                "Created: 2026-10-09\nTotal Randomized: 1 / Unchanged: 0 "
                "(same item after randomized)\nApp Version: v0.9.1\nGame Version: fixture\n"
                "Randomizer Settings:\nDisabled Initial Inventory\nSeed: old\n\n"
                "Old chest: DA_A -> DA_B\n",
                encoding="utf-8",
            )
            (current.root / "assignment_manifest.json").write_text(
                '{"schema":"test","assignments":[]}\n', encoding="utf-8",
            )

            report = manager.cleanup_successful_generation(
                current,
                current.spoiler_text,
                current.assignment_manifest,
            )

            self.assertGreater(report.temporary_bytes_before, 0)
            self.assertGreater(report.temporary_bytes_after, 0)
            self.assertGreater(len(report.deleted_directories), 2)
            self.assertTrue(current.root.is_dir())
            self.assertTrue(old.root.is_dir())
            self.assertFalse(current.extracted.exists())
            self.assertFalse(current.staging.exists())
            self.assertFalse(current.packaged.exists())
            self.assertFalse(old.packaged.exists())
            self.assertEqual(current_log.read_text(encoding="utf-8"), "current run log\n")
            self.assertEqual(old_log.read_text(encoding="utf-8"), '{"old":"run log"}\n')
            self.assertEqual(set(current.root.iterdir()), {current.logs, current.ownership_marker})
            self.assertIsNone(report.retained_spoiler)
            self.assertFalse(old_spoiler.exists())
            self.assertEqual(report.retained_assignment_manifest, manager.root / "assignment_manifest.json")
            self.assertEqual(
                report.retained_assignment_manifest.read_text(encoding="utf-8"),
                '{"schema":"test","assignments":[]}\n',
            )
            self.assertEqual(cache_asset.read_bytes(), b"vanilla cache stays")

    # Check spoiler copy detection accept old
    def test_spoiler_copy_detection_accepts_old_and_new_header_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_spoiler = root / "old.txt"
            old_spoiler.write_text(
                "Seed: old\nCreated: 2026-10-09\nApp Version: v0.9.1\n\nContents\n",
                encoding="utf-8",
            )
            new_spoiler = root / "new.txt"
            new_spoiler.write_text(
                "Created: 2026-10-10\nTotal Randomized: 1 / Unchanged: 0\n"
                "App Version: v0.9.1\nGame Version: fixture\n"
                "Randomizer Settings:\nSeed: new\nEnabled Initial Inventory\n\nContents\n",
                encoding="utf-8",
            )

            self.assertTrue(_is_randomizer_spoiler_copy(old_spoiler))
            self.assertTrue(_is_randomizer_spoiler_copy(new_spoiler))

    # Check successful cleanup preserve unrecognized output
    def test_successful_cleanup_preserves_unrecognized_output_spoiler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = WorkspaceManager(Path(temporary) / "output")
            workspace = manager.create_generation(normalize_seed("keep-user-file"))
            workspace.spoiler_text.write_text("generated temporary spoiler\n", encoding="utf-8")
            unrecognized = manager.root / "Spoiler.txt"
            unrecognized.write_text("my notes\n", encoding="utf-8")

            report = manager.cleanup_successful_generation(workspace, workspace.spoiler_text)

            self.assertEqual(unrecognized.read_text(encoding="utf-8"), "my notes\n")
            self.assertTrue(any("unrecognized" in warning for warning in report.warnings))

    # Check settings do not persist retired
    def test_settings_do_not_persist_retired_generate_only_choice(self) -> None:
        serialized = AppSettings(
            last_options=GenerationOptions(install_after_generation=False),
        ).to_dict()
        self.assertNotIn("install_after_generation", serialized["last_options"])
        self.assertTrue(AppSettings.from_dict(serialized).last_options.install_after_generation)
        self.assertTrue(
            AppSettings.from_dict({"last_options": {"install_after_generation": False}})
            .last_options.install_after_generation
        )

    # Check successful cleanup leaves unowned generation
    def test_successful_cleanup_leaves_unowned_generation_siblings_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = WorkspaceManager(Path(temporary) / "output")
            workspace = manager.create_generation(normalize_seed("owned"))
            workspace.spoiler_text.write_text("Seed: owned\n", encoding="utf-8")
            unowned = manager.root / "generated" / "user-folder"
            unowned.mkdir()
            payload = unowned / "keep.bin"
            payload.write_bytes(b"leave unowned data alone")

            report = manager.cleanup_successful_generation(workspace, workspace.spoiler_text)

            self.assertTrue(workspace.root.is_dir())
            self.assertTrue(workspace.logs.is_dir())
            self.assertFalse(workspace.staging.exists())
            self.assertEqual(payload.read_bytes(), b"leave unowned data alone")
            self.assertGreater(report.temporary_bytes_after, payload.stat().st_size)

    # Check extraction cache key changes source
    def test_extraction_cache_key_changes_with_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = CacheManager(Path(temporary))
            first = cache.entry_path(game_version="v1", source_hash="ABC", data_revision="r1")
            same = cache.entry_path(game_version="v1", source_hash="abc", data_revision="r1")
            changed = cache.entry_path(game_version="v2", source_hash="abc", data_revision="r1")

            self.assertEqual(first, same)
            self.assertNotEqual(first, changed)
            self.assertTrue(first.is_dir())
            self.assertTrue(changed.is_dir())


if __name__ == "__main__":
    unittest.main()
