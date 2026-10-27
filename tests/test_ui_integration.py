from __future__ import annotations

import os
import tempfile
import time
import unittest
from threading import Event, get_ident
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QTextBrowser

from cv2randomizer.core.config import SettingsStore
from cv2randomizer.core.models import AppSettings, GenerationOptions
from cv2randomizer.core.seed import normalize_seed
from cv2randomizer.core.workspace import GenerationWorkspace
from cv2randomizer.generation.pipeline import GenerationError, GenerationResult
from cv2randomizer.game.versioning import VersionDetection
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.game_data_cache import GameDataCacheSnapshot, GameDataCacheState
from cv2randomizer.packaging.installer import InstallationResult, RemovalResult
from cv2randomizer.randomizer.shops import ShopRandomizationPlan
from cv2randomizer.randomizer.world_loot import WorldLootRandomizationPlan
from cv2randomizer.ui.main_window import MainWindow


class _FakeEngine:
    # Initialize this object
    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests = []
        self.extractions = []

    # Generate randomized package output
    def generate(self, request, *, progress=None):
        self.requests.append(request)
        if progress:
            progress("packaging-mod", 1, 1)
        workspace = GenerationWorkspace(
            self.root,
            self.root / "extracted",
            self.root / "staging",
            self.root / "packaged",
            self.root / "logs",
        )
        installation = None
        if request.options.install_after_generation:
            game_root = request.game_root
            mods = game_root / "CodeVein2" / "Content" / "Paks" / "~mods"
            installation = InstallationResult(
                "CV2Randomizer_P", game_root, mods,
                (mods / "CV2Randomizer_P.pak", mods / "CV2Randomizer_P.utoc", mods / "CV2Randomizer_P.ucas"),
            )
        return GenerationResult(
            workspace=workspace,
            seed=normalize_seed(request.seed),
            game_version="fixture-build",
            catalog_revision="current",
            dry_run=False,
            world_plan=WorldLootRandomizationPlan((), (), (), ()),
            shop_plan=ShopRandomizationPlan((), (), (), ()),
            material_preparation=None,
            world_result=None,
            shop_result=None,
            material_result=None,
            output_content_root=workspace.staging / "Content",
            output_files=(),
            manifest_path=workspace.manifest,
            spoiler_path=None,
            package_result=None,
            installation_result=installation,
            config_fingerprint="c" * 64,
            content_fingerprint="d" * 64,
            assignment_manifest_sha256="e" * 64,
        )

    # Extract game data
    def extract_game_data(self, game_root, mappings_path, *, progress=None, reuse_cached_raw=False):
        self.extractions.append((game_root, mappings_path))
        if progress:
            progress("extracting-game-data", 1, 1)
        return GameDataCacheSnapshot(
            GameDataCacheState.READY,
            self.root / "fresh-cache",
            "fixture cache ready",
            "fixture-build",
            "current",
        )


class _FailingEngine:
    # Generate randomized package output
    def generate(self, request, *, progress=None):
        raise GenerationError("unsupported fixture build")


class _BlockingEngine:
    # Initialize this object
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    # Generate randomized package output
    def generate(self, request, *, progress=None):
        progress("creating-workspace", 0, 1)
        self.started.set()
        self.release.wait(3)
        progress("packaging-mod", 0, 1)
        raise AssertionError("cancel checkpoint should have raised")


class _FakeInstaller:
    # Initialize this object
    def __init__(self) -> None:
        self.game_roots = []

    # Remove Randomizer-owned mod files
    def remove(self, *, game_root=None):
        self.game_roots.append(game_root)
        return RemovalResult(None, game_root, ())


class MainWindowIntegrationTests(unittest.TestCase):
    # Set Up Class
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    # Wait for task
    def _wait_for_task(self, window: MainWindow) -> None:
        deadline = time.monotonic() + 5
        while window._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertIsNone(window._worker, "background task did not finish")

    # Read ready window
    def _ready_window(self, *, cache_path: Path | None = None, **kwargs) -> MainWindow:
        window = MainWindow(**kwargs)
        self.assertFalse(window.windowIcon().isNull())
        registry = {
            "world_loot": ["/Game/Fixture/World"],
            "materials": ["/Game/Fixture/Materials"],
            "shops": [
                "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup",
                "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_DLC",
                "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_Luxury",
                "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_Trade",
            ],
            "enemy_drops": ["/Game/Fixture/EnemyDrops"],
            "blood_codes": ["/Game/Fixture/BloodCodes"],
            "boosters": ["/Game/Fixture/Boosters"],
            "extractor": ["/Game/Fixture/Extractor"],
            "initial_inventory": ["/Game/Fixture/InitialInventory"],
            "defensive_gadget_grants": ["/Game/Fixture/Gadgets"],
            "bequeathed_formae_grants": ["/Game/Fixture/Formae"],
        }
        packages = ("/Game/Fixture/Core", *(package for group in registry.values() for package in group))
        detection = VersionDetection(
            True, "fixture-build", "current", None, "Fixture build", ("/Game/Fixture/Core",),
        )
        snapshot = GameDataCacheSnapshot(
            GameDataCacheState.READY,
            cache_path or Path("fixture-cache"),
            "fixture cache ready",
            "fixture-build",
            "current",
            packages[1:],
            (),
            (),
            ("/Game/Fixture/Core",),
            PackageInventory.create(packages),
            {
                "source_registry": registry,
                "extracted_package_inventory": list(packages),
            },
        )
        window._game_version_detection = detection
        window._game_data_cache_snapshot = snapshot
        window._cache_ready = True
        window._apply_cache_state()
        return window

    # Check each startup generate seed ignores
    def test_each_startup_generates_a_seed_and_ignores_the_saved_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = SettingsStore(root / "settings.json")
            settings.save(AppSettings(last_seed="previous-session-seed"))
            with patch("cv2randomizer.ui.main_window.secrets.randbelow", side_effect=(123, 456)):
                first = MainWindow(
                    generation_engine=_FakeEngine(root / "generation-a"),
                    installer=_FakeInstaller(),
                    settings_store=settings,
                )
                self.assertEqual(first.seed_input.text(), "123")
                first.seed_input.setEnabled(True)
                first.randomize_healing_check.setChecked(False)
                self.assertEqual(first.seed_input.text(), "123")
                first.close()

                second = MainWindow(
                    generation_engine=_FakeEngine(root / "generation-b"),
                    installer=_FakeInstaller(),
                    settings_store=settings,
                )
                self.assertEqual(second.seed_input.text(), "456")
                self.assertNotEqual(first.seed_input.text(), second.seed_input.text())
                second.close()

    # Check game path placeholder is not
    def test_game_path_placeholder_is_not_a_selected_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            self.assertEqual(
                window.game_path.placeholderText(),
                r"C:\Program Files (x86)\Steam\steamapps\common\CodeVein2",
            )
            self.assertEqual(window.game_path.text(), "")
            with patch("cv2randomizer.ui.main_window.QMessageBox.warning") as show_warning:
                window._start_generation()
            self.assertEqual(show_warning.call_args.args[1], "Game path required")
            self.assertEqual(window._worker, None)
            window.close()

    # Browse from selected game folder
    def test_game_path_browse_starts_at_selected_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected-game"
            selected.mkdir()
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.setText(str(selected))

            with patch(
                "cv2randomizer.ui.main_window.QFileDialog.getExistingDirectory",
                return_value="",
            ) as browse:
                window._browse_game_path()

            self.assertEqual(browse.call_args.args[2], str(selected))
            window.close()

    # Browse from app folder when selected folder is unavailable
    def test_game_path_browse_falls_back_to_app_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_folder = root / "app"
            app_folder.mkdir()
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.setText(str(root / "unavailable-game"))

            def browsable(directory: Path) -> bool:
                return directory == app_folder

            with (
                patch.object(window, "_directory_can_be_browsed", side_effect=browsable),
                patch("cv2randomizer.ui.main_window.application_root", return_value=app_folder),
                patch(
                    "cv2randomizer.ui.main_window.QFileDialog.getExistingDirectory",
                    return_value="",
                ) as browse,
            ):
                window._browse_game_path()

            self.assertEqual(browse.call_args.args[2], str(app_folder))
            window.close()

    # Browse from app folder when no folder is selected
    def test_game_path_browse_uses_app_folder_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_folder = root / "app"
            app_folder.mkdir()
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.clear()

            def browsable(directory: Path) -> bool:
                return directory == app_folder

            with (
                patch.object(window, "_directory_can_be_browsed", side_effect=browsable),
                patch("cv2randomizer.ui.main_window.application_root", return_value=app_folder),
                patch(
                    "cv2randomizer.ui.main_window.QFileDialog.getExistingDirectory",
                    return_value="",
                ) as browse,
            ):
                window._browse_game_path()

            self.assertEqual(browse.call_args.args[2], str(app_folder))
            window.close()

    # Check saved output directory outside app
    def test_saved_output_directory_outside_app_data_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_data = root / "app-data"
            external_output = root / "user-output"
            external_output.mkdir()
            sentinel = external_output / "keep.txt"
            sentinel.write_text("user data", encoding="utf-8")
            settings = SettingsStore(root / "settings.json")
            settings.save(AppSettings(output_dir=str(external_output)))

            with (
                patch("cv2randomizer.ui.main_window.user_data_dir", return_value=str(app_data)),
                patch(
                    "cv2randomizer.ui.main_window.activity_log_path",
                    return_value=root / "logs" / "log.txt",
                ),
            ):
                window = MainWindow(
                    generation_engine=_FakeEngine(root / "generation"),
                    installer=_FakeInstaller(),
                    settings_store=settings,
                )

            self.assertEqual(window._output_path, app_data / "output")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user data")
            self.assertFalse((external_output / "generated").exists())
            window.close()

    # Check randomizer options remain available until
    def test_randomizer_options_remain_available_until_cache_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = MainWindow(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            self.assertTrue(window.randomizer_page.isEnabled())
            self.assertFalse(hasattr(window, "randomizer_scroll"))
            self.assertFalse(window.seed_input.isEnabled())
            self.assertFalse(window.copy_seed_button.isEnabled())
            self.assertFalse(window.generate_install_button.isEnabled())
            self.assertTrue(window.extract_game_data_button.property("needsExtraction"))
            self.assertFalse(window.extract_game_data_button.isEnabled())
            self.assertFalse(hasattr(window, "generate_only_button"))
            self.assertTrue(window.game_path.isEnabled())
            self.assertTrue(window.game_browse_button.isEnabled())
            self.assertFalse(hasattr(window, "mappings_path"))
            self.assertFalse(hasattr(window, "mappings_browse_button"))
            self.assertIsNotNone(window._mappings_path)
            diagnostic_labels = {
                label.text() for label in window.tabs.widget(2).findChildren(QLabel)
            }
            self.assertEqual(window.windowTitle(), "Code Vein 2 Randomizer v1.0.0")
            self.assertIn("v1.0.0", diagnostic_labels)
            self.assertFalse(any("CV2.usmap" in text for text in diagnostic_labels))
            self.assertTrue(window.clear_cache_button.isEnabled())
            self.assertTrue(window.clear_output_button.isEnabled())
            self.assertEqual(window.game_data_status_label.text(), "Game Data: select a folder to verify")
            window.close()

    # Check clear extracted game data runs
    def test_clear_extracted_game_data_runs_in_background_with_busy_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            started = Event()
            release = Event()
            clear_thread_ids: list[int] = []

            # Clear old files in background
            def slow_clear() -> None:
                clear_thread_ids.append(get_ident())
                started.set()
                if not release.wait(3):
                    raise TimeoutError("test cache clear was not released")

            with (
                patch.object(window._game_data_cache_store, "clear", side_effect=slow_clear),
                patch("cv2randomizer.ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes),
            ):
                window.clear_cache_button.click()
                self.assertTrue(started.wait(1), "cache clear did not start promptly")
                self.assertEqual(window.status_label.text(), "Clearing extracted game data…")
                self.assertEqual((window.progress_bar.minimum(), window.progress_bar.maximum()), (0, 0))
                self.assertFalse(window.clear_cache_button.isEnabled())
                self.assertFalse(window.generate_install_button.isEnabled())
                self.assertNotEqual(clear_thread_ids[0], get_ident())
                release.set()
                self._wait_for_task(window)

            self.assertEqual(window.status_label.text(), "Extracted game data cleared.")
            self.assertEqual((window.progress_bar.minimum(), window.progress_bar.maximum()), (0, 100))
            self.assertEqual(window.progress_bar.value(), 100)
            self.assertFalse(window._cache_ready)
            self.assertIsNone(window._game_data_cache_snapshot)
            self.assertEqual(window.game_data_status_label.text(), "Game Data: Not Extracted")
            self.assertEqual(window._generation_engine.extractions, [])
            window.close()

    # Check clear old output runs background
    def test_clear_old_output_runs_in_background_with_busy_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            started = Event()
            release = Event()
            clear_thread_ids: list[int] = []

            # Clear old files in background
            def slow_clear(*_args, **_kwargs) -> int:
                clear_thread_ids.append(get_ident())
                started.set()
                if not release.wait(3):
                    raise TimeoutError("output clear was not released")
                return 4

            with (
                patch(
                    "cv2randomizer.ui.main_window.clear_output_directory",
                    side_effect=slow_clear,
                ) as clear,
                patch(
                    "cv2randomizer.ui.main_window.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ) as confirmation,
            ):
                window.clear_output_button.click()
                self.assertTrue(started.wait(1), "output clear did not start promptly")
                self.assertIn("output folder", confirmation.call_args.args[2])
                self.assertNotIn(str(window._output_path), confirmation.call_args.args[2])
                self.assertEqual(window.status_label.text(), "Clearing old output…")
                self.assertEqual((window.progress_bar.minimum(), window.progress_bar.maximum()), (0, 0))
                self.assertFalse(window.clear_output_button.isEnabled())
                self.assertFalse(window.clear_cache_button.isEnabled())
                self.assertNotEqual(clear_thread_ids[0], get_ident())
                self.assertEqual(clear.call_args.args[0], window._output_path)
                release.set()
                self._wait_for_task(window)

            self.assertEqual(window.status_label.text(), "Old output cleared.")
            self.assertEqual((window.progress_bar.minimum(), window.progress_bar.maximum()), (0, 100))
            self.assertEqual(window.progress_bar.value(), 100)
            window.close()

    # Check clear old output cancel leaves
    def test_clear_old_output_cancel_leaves_output_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            with (
                patch("cv2randomizer.ui.main_window.clear_output_directory") as clear,
                patch(
                    "cv2randomizer.ui.main_window.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.No,
                ),
            ):
                window.clear_output_button.click()
            clear.assert_not_called()
            self.assertIsNone(window._worker)
            self.assertTrue(window.clear_output_button.isEnabled())
            window.close()

    # Check clear cache cancel leaves cache
    def test_clear_cache_cancel_leaves_cache_state_and_files_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            snapshot = window._game_data_cache_snapshot
            with (
                patch.object(window._game_data_cache_store, "clear") as clear,
                patch("cv2randomizer.ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as confirmation,
            ):
                window.clear_cache_button.click()
            clear.assert_not_called()
            self.assertNotIn(str(window._cache_path), confirmation.call_args.args[2])
            self.assertIs(window._game_data_cache_snapshot, snapshot)
            self.assertTrue(window._cache_ready)
            self.assertEqual(window.status_label.text(), "Select a game folder to verify extracted data.")
            window.close()

    # Check clear cache failure invalidates memory
    def test_clear_cache_failure_invalidates_the_in_memory_ready_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            with (
                patch.object(window._game_data_cache_store, "clear", side_effect=OSError("cache is locked")),
                patch("cv2randomizer.ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes),
                patch("cv2randomizer.ui.main_window.QMessageBox.critical"),
                patch.object(window, "_start_game_check"),
            ):
                window.clear_cache_button.click()
                self._wait_for_task(window)

            self.assertFalse(window._cache_ready)
            self.assertIsNone(window._game_data_cache_snapshot)
            self.assertFalse(window.generate_install_button.isEnabled())
            self.assertEqual(window.game_data_status_label.text(), "Game Data: Clearing…")
            window.close()

    # Check reextract confirm build replacement without
    def test_reextract_confirm_builds_replacement_without_clearing_current_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = _FakeEngine(root / "generation")
            window = self._ready_window(
                generation_engine=engine,
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.setText(str(root / "game"))
            window._apply_cache_state()
            events = []
            with (
                patch.object(window._game_data_cache_store, "has_contents", return_value=True),
                patch.object(window._game_data_cache_store, "clear", side_effect=lambda: events.append("clear")),
                patch("cv2randomizer.ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as question,
            ):
                original_extract = engine.extract_game_data

                # Record extract
                def record_extract(*args, **kwargs):
                    events.append("extract")
                    return original_extract(*args, **kwargs)

                engine.extract_game_data = record_extract
                window.extract_game_data_button.click()
                self._wait_for_task(window)

            self.assertEqual(events, ["extract"])
            self.assertEqual(question.call_args.args[1], "Re-extract Game Data")
            self.assertIn("current cache remains available if extraction fails", question.call_args.args[2])
            self.assertTrue(window._cache_ready)
            self.assertEqual(window.game_data_status_label.text(), "Game Data: Ready")
            window.close()

    # Check reextract cancel does not clear
    def test_reextract_cancel_does_not_clear_or_start_extract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = _FakeEngine(root / "generation")
            window = self._ready_window(
                generation_engine=engine,
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.setText(str(root / "game"))
            window._apply_cache_state()
            snapshot = window._game_data_cache_snapshot
            with (
                patch.object(window._game_data_cache_store, "has_contents", return_value=True),
                patch.object(window._game_data_cache_store, "clear") as clear,
                patch("cv2randomizer.ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.No),
            ):
                window.extract_game_data_button.click()
            clear.assert_not_called()
            self.assertEqual(engine.extractions, [])
            self.assertTrue(window._cache_ready)
            self.assertIs(window._game_data_cache_snapshot, snapshot)
            window.close()

    # Check reextract does not clear existing
    def test_reextract_does_not_clear_existing_cache_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = _FakeEngine(root / "generation")
            window = self._ready_window(
                generation_engine=engine,
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.setText(str(root / "game"))
            window._apply_cache_state()
            with (
                patch.object(window._game_data_cache_store, "has_contents", return_value=True),
                patch.object(window._game_data_cache_store, "clear") as clear,
                patch("cv2randomizer.ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes),
                patch("cv2randomizer.ui.main_window.QMessageBox.critical"),
                patch.object(window, "_start_game_check"),
            ):
                window.extract_game_data_button.click()
                self._wait_for_task(window)
            clear.assert_not_called()
            self.assertEqual(len(engine.extractions), 1)
            self.assertTrue(window._cache_ready)
            self.assertEqual(window.game_data_status_label.text(), "Game Data: Ready")
            window.close()

    # Check game detection results accept windows
    def test_game_detection_results_accept_windows_slash_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "CodeVein2"
            typed_path = str(root).replace("\\", "/")
            worker_path = str(Path(typed_path))
            window = MainWindow(
                generation_engine=_FakeEngine(Path(temporary) / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(Path(temporary) / "settings.json"),
            )
            window.game_path.setText(typed_path)

            detection = VersionDetection(False, None, None, None, "No tested game-version signatures are configured.")
            window._on_game_check_result(worker_path, detection)
            self.assertEqual(window.status_label.text(), "Validating cached game data…")
            self.assertFalse(hasattr(window, "_support_status_label"))

            snapshot = GameDataCacheSnapshot(
                GameDataCacheState.INVALID, None, detection.reason,
            )
            window._on_game_cache_result(worker_path, snapshot)
            self.assertEqual(window.game_data_status_label.text(), "Game Data: Invalid")
            self.assertEqual(window.status_label.text(), "Extracted game data needs validation or extraction.")
            self.assertEqual(window.progress_bar.maximum(), 100)
            window.close()

    # Check startup verification use visible busy
    def test_startup_verification_uses_visible_busy_status_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = MainWindow(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            selected = root / "missing-game"
            window.game_path.setText(str(selected))
            window._start_game_check(str(selected))
            self.assertEqual(window.status_label.text(), "Verifying extracted game data…")
            self.assertEqual((window.progress_bar.minimum(), window.progress_bar.maximum()), (0, 0))

            window._on_task_progress("extracting-game-data", 0, 0)
            self.assertEqual(window.status_label.text(), "Extracting game data…")
            self.assertEqual((window.progress_bar.minimum(), window.progress_bar.maximum()), (0, 0))
            for worker in tuple(window._scan_workers):
                worker.wait()
            self.app.processEvents()
            window.close()

    # Check local unverified install can extract
    def test_local_unverified_install_can_extract_and_use_a_ready_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            ready_snapshot = window._game_data_cache_snapshot
            detection = VersionDetection(
                False,
                "local-unverified-fixture",
                "current",
                root / "CodeVein2.exe",
                "Local validation only; this build is not officially supported.",
                (),
                True,
            )
            window.game_path.setText(str(root / "game"))
            window._game_version_detection = detection
            window._game_data_cache_snapshot = None
            window._cache_ready = False
            window._apply_cache_state()
            self.assertTrue(window.extract_game_data_button.isEnabled())
            self.assertTrue(window.extract_game_data_button.property("needsExtraction"))
            self.assertFalse(window.generate_install_button.isEnabled())
            self.assertFalse(window.generate_install_button.property("randomizeReady"))

            window._game_data_cache_snapshot = ready_snapshot
            window._cache_ready = True
            window._apply_cache_state()
            self.assertFalse(window.extract_game_data_button.property("needsExtraction"))
            self.assertTrue(window.generate_install_button.isEnabled())
            self.assertTrue(window.generate_install_button.property("randomizeReady"))
            self.assertTrue(window.randomizer_page.isEnabled())
            window.close()

    # Check known game version label does
    def test_known_game_version_label_does_not_change_local_validation_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            game_path = str(root / "game")
            window.game_path.setText(game_path)
            detection = VersionDetection(
                False,
                "local-unverified-fixture",
                "current",
                root / "CodeVein2-Win64-Shipping.exe",
                "Local validation only; this build is not officially supported.",
                (),
                True,
                "1.0.3.0",
            )

            window._on_game_check_result(game_path, detection)

            self.assertEqual(window._game_version_label.text(), "1.0.3.0")
            self.assertIn("display only", window._game_version_label.toolTip())
            self.assertTrue(detection.local_unverified)
            self.assertFalse(detection.supported)
            self.assertEqual(detection.version_id, "local-unverified-fixture")
            self.assertIsNotNone(window._game_data_cache_snapshot)
            window._on_game_cache_result(game_path, window._game_data_cache_snapshot)
            self.assertEqual(
                window.game_data_status_label.text(),
                "Game Data: Ready (local validation, v1.0.3.0)",
            )

            window._game_version_detection = VersionDetection(
                True,
                "2.0.3.0",
                "current",
                root / "CodeVein2-Win64-Shipping.exe",
                "Supported game version: 2.0.3.0.",
                (),
                False,
                "2.0.3.0",
            )
            window._on_game_cache_result(game_path, window._game_data_cache_snapshot)
            self.assertEqual(window.game_data_status_label.text(), "Game Data: Ready (v2.0.3.0)")
            window.close()

    # Check retired generate preset controls are
    def test_retired_generate_and_preset_controls_are_absent_without_changing_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            button_texts = {button.text() for button in window.findChildren(QPushButton)}
            self.assertIn("Randomize", button_texts)
            self.assertTrue({"Generate Only", "Recommended", "Everything Safe", "Disable All"}.isdisjoint(button_texts))
            self.assertFalse(hasattr(window, "generate_only_button"))
            self.assertFalse(hasattr(window, "preset_buttons"))
            self.assertFalse(hasattr(window, "install_after_generation_check"))
            saved_options = AppSettings(last_options=GenerationOptions(install_after_generation=False)).to_dict()["last_options"]
            self.assertNotIn("install_after_generation", saved_options)
            self.assertFalse(hasattr(window, "world_loot_check"))
            self.assertTrue(window._current_generation_options().randomize_world_loot)
            self.assertTrue(window.randomize_healing_check.isChecked())
            self.assertFalse(hasattr(window, "bequeathed_formae_check"))
            visible_checkbox_texts = {
                checkbox.text() for checkbox in window.findChildren(QCheckBox)
                if not checkbox.isHidden()
            }
            self.assertNotIn("Randomize World Loot", visible_checkbox_texts)
            self.assertNotIn("Randomize Bequeathed Forma Quest Rewards", visible_checkbox_texts)
            self.assertFalse(window.materials_check.isChecked())
            self.assertFalse(window.materials_group.isHidden())
            self.assertIn("Randomize Renewable Ingredients", window.materials_check.text())
            self.assertIn(
                "only material pickups that respawn after resting",
                window.materials_check.toolTip().casefold(),
            )
            self.assertIn(
                "every pickup of the same material gets the same replacement",
                window.materials_check.toolTip().casefold(),
            )
            window.materials_check.setChecked(True)
            self.assertTrue(window._current_generation_options().randomize_materials)
            self.assertTrue(window.shops_check.isChecked())
            self.assertFalse(window.enemy_drops_check.isChecked())
            self.assertFalse(window.blood_codes_check.isChecked())
            self.assertFalse(window.boosters_check.isChecked())
            self.assertFalse(window.extractor_recipes_check.isChecked())
            self.assertFalse(window.initial_inventory_check.isChecked())
            self.assertTrue(window._current_generation_options().randomize_bequeathed_formae_quest_rewards)
            window.close()

    # Check DLC option is removed old
    def test_dlc_option_is_removed_and_old_settings_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            self.assertFalse(hasattr(window, "include_dlc_check"))
            restored = GenerationOptions.from_dict({"include_dlc_content": True})
            self.assertFalse(hasattr(restored, "include_dlc_content"))
            self.assertNotIn("include_dlc_content", restored.to_dict())
            window.close()

    # Check shop world mix option is
    def test_shop_world_mix_option_is_persisted_and_defaults_off_for_old_settings(self) -> None:
        self.assertFalse(GenerationOptions.from_dict({"randomize_shops": True}).mix_shops_with_world)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = SettingsStore(root / "settings.json")
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=settings,
            )
            self.assertFalse(window.mix_shops_with_world_check.isChecked())
            self.assertTrue(window.mix_shops_with_world_check.isEnabled())
            window.mix_shops_with_world_check.setChecked(True)
            self.assertTrue(window._current_generation_options().mix_shops_with_world)
            window._save_settings()
            self.assertTrue(settings.load().last_options.mix_shops_with_world)
            self.assertTrue(window.mix_shops_with_world_check.isEnabled())
            self.assertTrue(window.mix_shops_with_world_check.isChecked())
            window.close()

    # Check shop world mix is disabled
    def test_shop_world_mix_is_disabled_and_clamped_when_shops_are_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = SettingsStore(root / "settings.json")
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=settings,
            )
            window.mix_shops_with_world_check.setChecked(True)
            window.shops_check.setChecked(False)
            self.assertFalse(window.mix_shops_with_world_check.isEnabled())
            # Keep the visible preference while the parent is off, but the
            # effective option and persisted value must remain safely clamped.
            self.assertTrue(window.mix_shops_with_world_check.isChecked())
            self.assertFalse(window._current_generation_options().mix_shops_with_world)
            window._save_settings()
            self.assertFalse(settings.load().last_options.mix_shops_with_world)
            window.close()

    # Check NPC trader unlock is independent
    def test_npc_trader_unlock_is_independent_of_reward_randomization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.shops_check.setChecked(False)
            window.unlock_npc_trader_shops_check.setChecked(True)
            options = window._current_generation_options()
            self.assertFalse(options.randomize_shops)
            self.assertFalse(options.mix_shops_with_world)
            self.assertTrue(options.unlock_npc_trader_shops)
            window.close()

    # Check all user option values survive
    def test_all_user_option_values_survive_close_and_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = SettingsStore(root / "settings.json")
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation-a"),
                installer=_FakeInstaller(),
                settings_store=settings,
            )
            expected = {
                "randomize_initial_inventory": True,
                "randomize_blood_codes": True,
                "randomize_boosters": True,
                "randomize_extractor_recipes": True,
                "cheaper_booster_extractor": False,
                "randomize_shops": True,
                "mix_shops_with_world": True,
                "unlock_npc_trader_shops": True,
                "unlock_all_booster_slots": True,
                "randomize_enemy_drops": True,
                "randomize_healing": False,
                "motorcycle_unlocked": True,
                "second_weapon_unlocked": True,
                "disable_tutorials": True,
            }
            controls = {
                "randomize_initial_inventory": window.initial_inventory_check,
                "randomize_blood_codes": window.blood_codes_check,
                "randomize_boosters": window.boosters_check,
                "randomize_extractor_recipes": window.extractor_recipes_check,
                "cheaper_booster_extractor": window.cheaper_booster_extractor_check,
                "randomize_shops": window.shops_check,
                "mix_shops_with_world": window.mix_shops_with_world_check,
                "unlock_npc_trader_shops": window.unlock_npc_trader_shops_check,
                "unlock_all_booster_slots": window.unlock_all_booster_slots_check,
                "randomize_enemy_drops": window.enemy_drops_check,
                "randomize_healing": window.randomize_healing_check,
                "motorcycle_unlocked": window.motorcycle_unlocked_check,
                "second_weapon_unlocked": window.second_weapon_unlocked_check,
                "disable_tutorials": window.disable_tutorials_check,
            }
            for name, control in controls.items():
                control.setChecked(expected[name])
            window.close()

            restored_options = settings.load().last_options
            for name, value in expected.items():
                self.assertEqual(getattr(restored_options, name), value, name)

            reopened = self._ready_window(
                generation_engine=_FakeEngine(root / "generation-b"),
                installer=_FakeInstaller(),
                settings_store=settings,
            )
            for name, control in controls.items():
                self.assertEqual(getattr(reopened, {
                    "randomize_initial_inventory": "initial_inventory_check",
                    "randomize_blood_codes": "blood_codes_check",
                    "randomize_boosters": "boosters_check",
                    "randomize_extractor_recipes": "extractor_recipes_check",
                    "cheaper_booster_extractor": "cheaper_booster_extractor_check",
                    "randomize_shops": "shops_check",
                    "mix_shops_with_world": "mix_shops_with_world_check",
                    "unlock_npc_trader_shops": "unlock_npc_trader_shops_check",
                    "unlock_all_booster_slots": "unlock_all_booster_slots_check",
                    "randomize_enemy_drops": "enemy_drops_check",
                    "randomize_healing": "randomize_healing_check",
                    "motorcycle_unlocked": "motorcycle_unlocked_check",
                    "second_weapon_unlocked": "second_weapon_unlocked_check",
                    "disable_tutorials": "disable_tutorials_check",
                }[name]).isChecked(), expected[name], name)
            self.assertTrue(reopened._current_generation_options().motorcycle_unlocked)
            self.assertTrue(reopened._current_generation_options().second_weapon_unlocked)
            reopened.close()

    # Check shops master include NPC traders
    def test_shops_master_includes_npc_traders_and_ignores_retired_controls(self) -> None:
        self.assertTrue(GenerationOptions.from_dict({"randomize_shops": True}).randomize_npc_traders)
        self.assertFalse(GenerationOptions.from_dict({"randomize_shops": False, "randomize_npc_traders": True}).randomize_npc_traders)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = SettingsStore(root / "settings.json")
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=settings,
            )
            self.assertFalse(hasattr(window, "npc_traders_check"))
            self.assertFalse(hasattr(window, "npc_trader_mode_combo"))
            self.assertFalse(hasattr(window, "normal_shops_check"))
            self.assertFalse(hasattr(window, "luxury_shops_check"))
            self.assertFalse(hasattr(window, "trade_shops_check"))
            options = window._current_generation_options()
            self.assertTrue(options.randomize_npc_traders)
            self.assertEqual(options.npc_traders_mode, "only")
            self.assertTrue(window._has_selected_randomizer())
            self.assertTrue(options.randomize_shops)
            window._save_settings()
            self.assertEqual(settings.load().last_options, options)
            self.assertNotIn("randomize_npc_traders", options.to_dict())
            window.close()

    # Check feature grid aligns rows misc
    def test_feature_grid_aligns_rows_and_misc_spans_both_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            page = window.randomizer_options_page
            self.assertIsInstance(window.randomizer_page, QScrollArea)
            self.assertEqual(
                window.randomizer_page.verticalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAsNeeded,
            )
            grid = page.layout().itemAt(0).layout()
            self.assertIsInstance(grid, QGridLayout)
            self.assertEqual(page.layout().count(), 1)
            self.assertEqual(page.layout().spacing(), 0)
            self.assertEqual(page.layout().contentsMargins().top(), 10)
            self.assertEqual(
                window.tabs.sizePolicy().verticalPolicy(),
                QSizePolicy.Policy.Expanding,
            )
            self.assertEqual(grid.count(), 7)
            self.assertEqual(window.height(), 500)
            self.assertEqual(
                window.shops_check.text(),
                "Randomize Shops and NPC Traders (Finite-stock items)",
            )
            self.assertEqual(
                window.unlock_npc_trader_shops_check.text(),
                "Unlock all NPC Trader Shop Tiers",
            )
            self.assertFalse(window.unlock_npc_trader_shops_check.isChecked())
            self.assertEqual(
                window.boosters_check.text(),
                "Mix Boosters with World Loot",
            )
            self.assertEqual(
                window.extractor_recipes_check.text(),
                "Randomize Booster Extractor",
            )
            self.assertNotIn("Shuffle crafted Boosters; costs and requirements stay fixed.", {
                label.text() for label in window.findChildren(QLabel)
            })
            titles = {group.title() for group in window.findChildren(QGroupBox)}
            self.assertTrue({"Healing Items and Ingredients", "Initial Inventory and Blood Codes", "Boosters", "Enemy Drops", "Shops", "Key Items and Quests", "Miscellaneous"}.issubset(titles))
            self.assertNotIn("Blood Codes", titles)
            self.assertNotIn("Initial Inventory", titles)
            self.assertNotIn("Healing Items", titles)
            self.assertNotIn("Ingredients", titles)
            self.assertNotIn("Gameplay", titles)
            self.assertNotIn("Unlocks", titles)
            self.assertFalse(any(
                "Test Ground Materials at the Pipe Shop" in text
                for text in (
                    *(control.text() for control in window.findChildren(QCheckBox)),
                    *(label.text() for label in window.findChildren(QLabel)),
                )
            ))
            self.assertNotIn("Extractor / Booster Crafting", titles)
            self.assertEqual(
                window.extractor_recipes_check.text(),
                "Randomize Booster Extractor",
            )
            self.assertEqual(
                window.unlock_all_booster_slots_check.text(),
                "Unlock all 6 Booster Slots (New Game only)",
            )
            self.assertEqual(
                window.unlock_npc_trader_shops_check.text(),
                "Unlock all NPC Trader Shop Tiers",
            )
            self.assertEqual(
                window.motorcycle_unlocked_check.text(),
                "Unlock Motorcycle at Start (New Game only)",
            )
            self.assertEqual(
                window.second_weapon_unlocked_check.text(),
                "Unlock Second Weapon Slot (New Game only)",
            )
            self.assertEqual(
                window.disable_tutorials_check.text(),
                "Disable Tutorial Pop-ups",
            )
            self.assertEqual(
                window.tap_to_skip_dialogue_check.text(),
                "Tap Instead of Holding to Skip Dialogue (Experimental)",
            )
            self.assertTrue(window.tap_to_skip_dialogue_check.isEnabled())
            self.assertEqual(
                window.low_blood_code_mastery_exp_check.text(),
                "Unrestricted Blood Code Mastery",
            )
            groups_by_title = {group.title(): group for group in window.findChildren(QGroupBox)}
            miscellaneous = groups_by_title["Miscellaneous"]
            self.assertEqual(grid.getItemPosition(6), (3, 0, 1, 2))
            initial_blood_codes = groups_by_title["Initial Inventory and Blood Codes"]
            self.assertIs(window.initial_inventory_check.parentWidget(), initial_blood_codes)
            self.assertIs(window.blood_codes_check.parentWidget(), initial_blood_codes)
            self.assertEqual(initial_blood_codes.layout().count(), 4)
            self.assertEqual(initial_blood_codes.layout().spacing(), 6)
            self.assertEqual(
                (
                    initial_blood_codes.layout().contentsMargins().top(),
                    initial_blood_codes.layout().contentsMargins().bottom(),
                ),
                (0, 0),
            )
            self.assertIsNotNone(initial_blood_codes.layout().itemAt(0).spacerItem())
            self.assertEqual(
                initial_blood_codes.layout().itemAt(0).spacerItem().sizeHint().height(),
                8,
            )
            self.assertIs(
                initial_blood_codes.layout().itemAt(1).widget(),
                window.initial_inventory_check,
            )
            self.assertIs(
                initial_blood_codes.layout().itemAt(2).widget(),
                window.blood_codes_check,
            )
            self.assertIsNotNone(initial_blood_codes.layout().itemAt(3).spacerItem())
            self.assertEqual(
                initial_blood_codes.layout().itemAt(3).spacerItem().sizeHint().height(),
                6,
            )
            healing_ingredients = groups_by_title["Healing Items and Ingredients"]
            self.assertIs(window.randomize_healing_check.parentWidget(), healing_ingredients)
            self.assertIs(window.materials_check.parentWidget(), healing_ingredients)
            self.assertIn(
                "shuffle only among ingredient materials",
                window.materials_check.text(),
            )
            self.assertEqual(healing_ingredients.layout().count(), 4)
            self.assertEqual(healing_ingredients.layout().spacing(), 6)
            self.assertEqual(
                (
                    healing_ingredients.layout().contentsMargins().top(),
                    healing_ingredients.layout().contentsMargins().bottom(),
                ),
                (0, 0),
            )
            self.assertIsNotNone(healing_ingredients.layout().itemAt(0).spacerItem())
            self.assertEqual(
                healing_ingredients.layout().itemAt(0).spacerItem().sizeHint().height(),
                8,
            )
            self.assertIs(healing_ingredients.layout().itemAt(1).widget(), window.randomize_healing_check)
            self.assertIs(healing_ingredients.layout().itemAt(2).widget(), window.materials_check)
            self.assertIsNotNone(healing_ingredients.layout().itemAt(3).spacerItem())
            self.assertEqual(
                healing_ingredients.layout().itemAt(3).spacerItem().sizeHint().height(),
                6,
            )
            self.assertEqual(grid.getItemPosition(4), (2, 0, 1, 1))
            self.assertEqual(grid.getItemPosition(5), (2, 1, 1, 1))
            miscellaneous_outer_layout = miscellaneous.layout()
            self.assertEqual(miscellaneous_outer_layout.count(), 3)
            self.assertIsNotNone(miscellaneous_outer_layout.itemAt(0).spacerItem())
            self.assertIsNotNone(miscellaneous_outer_layout.itemAt(2).spacerItem())
            self.assertEqual(
                miscellaneous_outer_layout.itemAt(0).spacerItem().sizeHint().height(),
                8,
            )
            self.assertEqual(
                miscellaneous_outer_layout.itemAt(2).spacerItem().sizeHint().height(),
                6,
            )
            miscellaneous_grid = miscellaneous_outer_layout.itemAt(1).layout()
            self.assertIsInstance(miscellaneous_grid, QGridLayout)
            self.assertEqual(miscellaneous_grid.verticalSpacing(), 6)
            self.assertEqual(
                (
                    miscellaneous_outer_layout.contentsMargins().top(),
                    miscellaneous_outer_layout.contentsMargins().bottom(),
                ),
                (0, 0),
            )
            self.assertEqual(miscellaneous_grid.count(), 8)
            self.assertEqual(
                [
                    miscellaneous_grid.getItemPosition(index)[:2]
                    for index in range(miscellaneous_grid.count())
                ],
                [(0, 0), (1, 0), (2, 0), (3, 0), (0, 1), (1, 1), (2, 1), (3, 1)],
            )
            for control in (
                window.unlock_all_booster_slots_check,
                window.unlock_npc_trader_shops_check,
                window.motorcycle_unlocked_check,
                window.second_weapon_unlocked_check,
                window.disable_tutorials_check,
                window.low_blood_code_mastery_exp_check,
                window.cheaper_booster_extractor_check,
                window.tap_to_skip_dialogue_check,
            ):
                self.assertIs(control.parentWidget(), miscellaneous)
                self.assertTrue(
                    control.layout().itemAt(0).alignment()
                    & Qt.AlignmentFlag.AlignVCenter
                )
                self.assertTrue(
                    control.layout().itemAt(1).alignment()
                    & Qt.AlignmentFlag.AlignVCenter
                )
            window.low_blood_code_mastery_exp_check.setChecked(True)
            self.assertTrue(window._current_generation_options().low_blood_code_mastery_exp)
            window.tap_to_skip_dialogue_check.setChecked(True)
            self.assertTrue(window._current_generation_options().tap_to_skip_dialogue)
            self.assertFalse(hasattr(window, "test_unidentified_shop_materials_check"))
            self.assertFalse(hasattr(window, "disable_preorder_weapons_check"))
            self.assertTrue(
                window.blood_codes_check.layout().itemAt(0).alignment()
                & Qt.AlignmentFlag.AlignVCenter
            )
            self.assertTrue(
                window.blood_codes_check.layout().itemAt(1).alignment()
                & Qt.AlignmentFlag.AlignVCenter
            )
            self.assertIsNot(window.unlock_all_booster_slots_check.parentWidget(), groups_by_title["Boosters"])
            self.assertIsNot(window.unlock_npc_trader_shops_check.parentWidget(), groups_by_title["Shops"])
            all_groups = [grid.itemAt(index).widget() for index in range(grid.count())]
            self.assertEqual(
                [grid.getItemPosition(index) for index in range(grid.count())],
                [
                    (0, 0, 1, 1),
                    (0, 1, 1, 1),
                    (1, 0, 1, 1),
                    (1, 1, 1, 1),
                    (2, 0, 1, 1),
                    (2, 1, 1, 1),
                    (3, 0, 1, 2),
                ],
            )
            window.resize(1000, 500)
            window.show()
            self.app.processEvents()
            self.assertIn("QGroupBox { border: 1px solid #c7c7c7", self.app.styleSheet())
            self.assertIn("QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #202020", self.app.styleSheet())
            self.assertIn("QCheckBox:disabled { color: #929292; }", self.app.styleSheet())
            self.assertIn('QPushButton[needsExtraction="true"] { background: #fff0c2', self.app.styleSheet())
            self.assertGreaterEqual(window.progress_bar.height(), 18)
            self.assertEqual(window.size().width(), 1000)
            self.assertEqual(window.size().height(), 500)
            self.assertTrue(all(group.isVisible() for group in all_groups))
            self.assertTrue(all(
                group.geometry().right() <= page.width()
                for group in all_groups
            ))
            self.assertGreater(
                miscellaneous.geometry().width(),
                initial_blood_codes.geometry().width(),
            )
            self.assertEqual(
                healing_ingredients.geometry().width(),
                groups_by_title["Shops"].geometry().width(),
            )
            self.assertLessEqual(
                abs(
                    groups_by_title["Key Items and Quests"].geometry().width()
                    - healing_ingredients.geometry().width()
                ),
                1,
            )
            self.assertNotIn("Bequeathed Formae", titles)
            self.assertNotIn("Content Mixing", titles)
            self.assertNotIn("Blood Codes & Boosters", titles)
            self.assertFalse(hasattr(window, "dlc_shops_check"))
            self.assertFalse(hasattr(window, "jails_check"))
            self.assertFalse(hasattr(window, "include_dlc_check"))
            window.extractor_recipes_check.setChecked(True)
            self.assertTrue(window._current_generation_options().randomize_extractor_recipes)
            self.assertFalse(window.cheaper_booster_extractor_check.isChecked())
            self.assertTrue(window.cheaper_booster_extractor_check.isEnabled())
            self.assertIn("only 1x of each material", window.cheaper_booster_extractor_check.text())
            window.extractor_recipes_check.setChecked(False)
            window.cheaper_booster_extractor_check.setChecked(True)
            self.assertTrue(window._current_generation_options().cheaper_booster_extractor)
            self.assertFalse(window._current_generation_options().randomize_extractor_recipes)
            self.assertEqual(window.cheaper_booster_extractor_check.parent().title(), "Miscellaneous")
            window.initial_inventory_check.setChecked(True)
            self.assertTrue(window._current_generation_options().randomize_initial_inventory)
            self.assertEqual(
                window.randomize_healing_check.text(),
                "Randomize Healing Items (Nectars and Blood Beads join World Loot)",
            )
            self.assertEqual(
                window.enemy_drops_check.text(),
                "Randomize Enemy Drops (Shuffles enemy drops, including drops shared with bosses; one-time rewards join World Loot)",
            )
            self.assertEqual(
                window.initial_inventory_check.text(),
                "Randomize Initial Inventory (Weapon, Defensive Formae and Jail)",
            )
            self.assertIn("first Blood Code stays vanilla", window.initial_inventory_check.toolTip())
            self.assertTrue(window.initial_inventory_check._label.wordWrap())
            window.initial_inventory_check.setChecked(False)
            QTest.mouseClick(
                window.initial_inventory_check._label,
                Qt.MouseButton.LeftButton,
            )
            self.assertTrue(window.initial_inventory_check.isChecked())
            self.assertFalse(hasattr(window, "spoiler_log_check"))
            self.assertFalse(window._current_generation_options().generate_spoiler_log)
            self.assertEqual(window.open_install_folder_button.text(), "Open Install Folder")
            root_layout = window.centralWidget().layout()
            seed_row = root_layout.itemAt(0).layout()
            self.assertIs(seed_row.itemAt(5).widget(), window.copy_seed_button)
            self.assertIs(seed_row.itemAt(6).widget(), window.random_seed_button)
            self.assertLess(
                window.copy_seed_button.geometry().right(),
                window.random_seed_button.geometry().left(),
            )
            window.seed_input.setText("clipboard-seed")
            window.copy_seed_button.click()
            self.assertEqual(QApplication.clipboard().text(), "clipboard-seed")
            self.assertIs(root_layout.itemAt(2).widget(), window.tabs)
            self.assertIs(root_layout.itemAt(3).widget(), window.actions_row)
            self.assertIs(root_layout.itemAt(4).widget(), window.status_group)
            actions_layout = window.actions_row.layout()
            self.assertIs(actions_layout.itemAt(0).widget(), window.extract_game_data_button)
            self.assertIs(actions_layout.itemAt(1).widget(), window.game_data_status_label)
            self.assertLess(
                window.extract_game_data_button.geometry().right(),
                window.game_data_status_label.geometry().left(),
            )
            self.assertLess(
                window.game_data_status_label.geometry().right(),
                window.generate_install_button.geometry().left(),
            )
            self.assertLessEqual(
                window.open_install_folder_button.geometry().right(),
                window.actions_row.width(),
            )
            self.assertEqual(
                {window.extract_game_data_button.parentWidget(), window.generate_install_button.parentWidget(), window.remove_button.parentWidget(),
                 window.open_install_folder_button.parentWidget()},
                {window.actions_row},
            )
            self.assertEqual(window.seed_input.height(), window.random_seed_button.height())
            self.assertEqual(window.seed_input.height(), window.copy_seed_button.height())
            self.assertEqual(window.game_path.height(), window.game_browse_button.height())
            self.assertTrue(all(
                control.height() == 30
                for control in (*window.findChildren(QLineEdit), *window.findChildren(QPushButton))
            ))
            key_quest_group = groups_by_title["Key Items and Quests"]
            self.assertEqual(
                [checkbox.text() for checkbox in key_quest_group.findChildren(QCheckBox)],
                [
                    "Randomize Keys (In Progress)",
                    "Randomize Quest Items (In Progress)",
                ],
            )
            self.assertEqual(key_quest_group.layout().count(), 4)
            self.assertEqual(key_quest_group.layout().spacing(), 6)
            self.assertIsNotNone(key_quest_group.layout().itemAt(0).spacerItem())
            self.assertIsNotNone(key_quest_group.layout().itemAt(3).spacerItem())
            self.assertEqual(
                key_quest_group.layout().itemAt(0).spacerItem().sizeHint().height(),
                8,
            )
            self.assertEqual(
                key_quest_group.layout().itemAt(3).spacerItem().sizeHint().height(),
                6,
            )
            self.assertTrue(all(
                not checkbox.isEnabled()
                for checkbox in key_quest_group.findChildren(QCheckBox)
            ))
            self.assertFalse(window.randomize_keys_check.isChecked())
            self.assertFalse(window.randomize_quest_items_check.isChecked())
            window.resize(900, 500)
            self.app.processEvents()
            self.assertEqual((window.width(), window.height()), (900, 500))
            self.assertEqual(window.randomizer_page.verticalScrollBar().maximum(), 0)
            self.assertTrue(all(
                group.geometry().bottom() <= page.height()
                for group in all_groups
            ))
            self.assertTrue(all(
                group.height() >= group.minimumSizeHint().height()
                for group in all_groups
            ))
            self.assertTrue(window.status_group.isVisible())
            window.resize(1000, 500)
            self.app.processEvents()
            status_height = window.status_group.height()
            actions_height = window.actions_row.height()
            progress_height = window.progress_bar.height()
            status_bottom_gap = (
                window.centralWidget().height()
                - window.status_group.geometry().bottom()
                - 1
            )
            actions_status_gap = (
                window.status_group.geometry().top()
                - window.actions_row.geometry().bottom()
                - 1
            )
            tabs_bottom = window.tabs.geometry().bottom()
            tabs_height = window.tabs.height()
            actions_top = window.actions_row.geometry().top()
            flexible_gap = actions_top - tabs_bottom - 1
            window.resize(1000, 570)
            self.app.processEvents()
            self.assertEqual(window.randomizer_page.verticalScrollBar().maximum(), 0)
            self.assertEqual(window.status_group.height(), status_height)
            self.assertEqual(window.actions_row.height(), actions_height)
            self.assertEqual(window.progress_bar.height(), progress_height)
            self.assertEqual(
                window.centralWidget().height()
                - window.status_group.geometry().bottom()
                - 1,
                status_bottom_gap,
            )
            self.assertEqual(
                window.status_group.geometry().top()
                - window.actions_row.geometry().bottom()
                - 1,
                actions_status_gap,
            )
            self.assertEqual(window.tabs.height(), tabs_height + 70)
            self.assertLessEqual(
                abs(
                    window.actions_row.geometry().top()
                    - window.tabs.geometry().bottom()
                    - 1
                    - flexible_gap
                ),
                1,
            )
            self.assertEqual(
                [
                    groups_by_title[left].geometry().top()
                    for left in (
                        "Initial Inventory and Blood Codes",
                        "Shops",
                        "Healing Items and Ingredients",
                    )
                ],
                [
                    groups_by_title[right].geometry().top()
                    for right in (
                        "Boosters",
                        "Enemy Drops",
                        "Key Items and Quests",
                    )
                ],
            )
            for left, right in (
                ("Initial Inventory and Blood Codes", "Boosters"),
                ("Shops", "Enemy Drops"),
                ("Healing Items and Ingredients", "Key Items and Quests"),
            ):
                self.assertEqual(
                    groups_by_title[left].height(),
                    groups_by_title[right].height(),
                )
                self.assertLessEqual(
                    abs(
                        groups_by_title[left].width()
                        - groups_by_title[right].width()
                    ),
                    1,
                )
            for upper, lower in (
                ("Initial Inventory and Blood Codes", "Shops"),
                ("Shops", "Healing Items and Ingredients"),
                ("Healing Items and Ingredients", "Miscellaneous"),
            ):
                self.assertEqual(
                    groups_by_title[lower].geometry().top()
                    - groups_by_title[upper].geometry().bottom()
                    - 1,
                    grid.verticalSpacing(),
                )
            window.resize(1000, 500)
            self.app.processEvents()
            self.assertEqual(
                [button.text() for button in window.tabs.widget(2).findChildren(QPushButton)],
                [
                    "Open Output Folder",
                    "Open Settings Folder",
                    "Open Cache Folder",
                    "Clear Old Output",
                    "Clear Extracted Game Data",
                ],
            )
            self.assertFalse(hasattr(window, "inventory_export_button"))
            self.assertFalse(hasattr(window, "_support_status_label"))
            self.assertEqual(window.status_group.title(), "Status")
            self.assertFalse(hasattr(window, "activity_log"))
            self.assertEqual(window.tabs.tabText(1), "Rules")
            self.assertEqual(window.tabs.tabText(2), "Diagnostic")
            self.assertEqual(window.tabs.tabText(3), "About")
            self.assertEqual(window.tabs.count(), 4)
            self.assertTrue(window.mapping_warning_label.isHidden())
            diagnostic_labels = {
                label.text() for label in window.tabs.widget(2).findChildren(QLabel)
            }
            self.assertFalse(any(label.casefold() == "logs" for label in diagnostic_labels))
            self.assertTrue(any(label.casefold() == "settings" for label in diagnostic_labels))
            self.assertIn("v1.0.0", diagnostic_labels)
            self.assertFalse(any("algorithm" in text.casefold() for text in diagnostic_labels))
            self.assertFalse(any("latest generate" in text.casefold() for text in diagnostic_labels))
            self.assertFalse(any("CV2.usmap" in text for text in diagnostic_labels))
            window.close()

    # Check rules tab explains randomizer behavior
    def test_rules_tab_explains_randomizer_behavior_in_plain_language(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            self.assertEqual(
                [window.tabs.tabText(index) for index in range(window.tabs.count())],
                ["Item Randomizer", "Rules", "Diagnostic", "About"],
            )
            rules = window.rules_page.findChild(QTextBrowser, "rulesTextBrowser")
            self.assertIsNotNone(rules)
            text = rules.toPlainText()
            for phrase in (
                "World Loot",
                "Renewable Ingredients",
                "click Randomize",
                "ground item A → ground item B",
                "Nectar → another ground item",
                "Sap of Life → Onion",
                "enemy drop A → enemy drop B",
                "Blood Code A → Blood Code B",
                "Booster A → ground item B",
                "recipe reward A → recipe reward B",
                "shop item A → shop item B",
                "shop item A → ground item B",
                "starting weapon A → starting weapon B",
                "Pre-order items are left alone",
                "Tools Shop in MagMell is left alone",
                "Items with infinite shop stock are left alone",
                "Quest rewards are still being reviewed",
                "leaves the original game files untouched",
                "Uninstall Randomizer",
                "~mods folder",
                "Compatibility",
                "fully compatible with Code Vein 2 v2.0.3",
                "Other versions may have missing or different files",
                "left unchanged and counted by category in Spoiler.txt",
                "Tap Instead of Holding to Skip Dialogue (Experimental)",
                "quick press of your configured Interact button",
                "keyboard or controller",
                "unsupported layout",
            ):
                self.assertIn(phrase, text)
            self.assertLess(
                text.index("leaves the original game files untouched"),
                text.index("Choose a seed"),
            )
            self.assertNotIn("Generate & Install", text)
            window.close()

    # Check about tab displays canonical metadata
    def test_about_tab_displays_canonical_metadata_icon_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            self.assertEqual(
                [window.tabs.tabText(index) for index in range(window.tabs.count())],
                ["Item Randomizer", "Rules", "Diagnostic", "About"],
            )
            window.resize(1280, 720)
            window.show()
            window.tabs.setCurrentWidget(window.about_page)
            self.app.processEvents()

            about_labels = {
                label.objectName(): label
                for label in window.about_page.findChildren(QLabel)
            }
            self.assertEqual(
                about_labels["aboutApplicationName"].text(),
                "Code Vein 2 Randomizer",
            )
            self.assertEqual(about_labels["aboutApplicationVersion"].text(), "Version v1.0.0")
            self.assertEqual(
                about_labels["aboutApplicationDescription"].text(),
                "Item randomizer for Code Vein 2.",
            )
            self.assertTrue(window.about_icon_label.property("iconLoaded"))
            self.assertTrue(window.about_icon_label.property("iconPngLoaded"))
            self.assertTrue(window.about_icon_label.property("iconPath").endswith("ico\\icon.png"))
            self.assertFalse(window.about_icon_label.pixmap().isNull())
            self.assertTrue(window.about_nexus_link.isEnabled())
            self.assertIn(
                '<a href="https://www.nexusmods.com/codevein2/mods/220"',
                window.about_nexus_link.text(),
            )
            self.assertIn("color:#005A9E", window.about_github_link.text())
            self.assertIn("text-decoration:underline", window.about_steam_link.text())
            self.assertEqual(window.about_page.findChildren(QScrollArea), [])
            self.assertLess(window.about_page.layout().sizeHint().height(), 720)
            self.assertLess(window.about_page.layout().sizeHint().width(), 1280)

            with patch(
                "cv2randomizer.ui.main_window.QDesktopServices.openUrl",
                return_value=True,
            ) as open_url:
                window.about_github_link.linkActivated.emit(
                    "https://github.com/mmachida/CodeVein2-Randomizer"
                )
                window.about_steam_link.linkActivated.emit(
                    "https://store.steampowered.com/app/2362060/CODE_VEIN_II/"
                )
                window.about_nexus_link.linkActivated.emit(
                    "https://www.nexusmods.com/codevein2/mods/220"
                )
            self.assertEqual(
                [call.args[0].toString() for call in open_url.call_args_list],
                [
                    "https://github.com/mmachida/CodeVein2-Randomizer",
                    "https://store.steampowered.com/app/2362060/CODE_VEIN_II/",
                    "https://www.nexusmods.com/codevein2/mods/220",
                ],
            )

            with patch(
                "cv2randomizer.ui.main_window.QDesktopServices.openUrl",
                return_value=False,
            ), patch("cv2randomizer.ui.main_window.QMessageBox.warning") as warning:
                window.about_github_link.linkActivated.emit(
                    "https://github.com/mmachida/CodeVein2-Randomizer"
                )
            warning.assert_called_once()

            with patch(
                "cv2randomizer.ui.main_window.NEXUS_MODS_URL",
                "https://www.nexusmods.com/codevein2/mods/220",
            ) as configured_url:
                configured_page = window._build_about_tab()
                self.assertIn(
                    f'<a href="{configured_url}"',
                    window.about_nexus_link.text(),
                )
                self.assertIn("color:#005A9E", window.about_nexus_link.text())
                self.assertTrue(window.about_nexus_link.isEnabled())
                configured_page.deleteLater()

            with patch(
                "cv2randomizer.ui.main_window.application_resource",
                return_value=root / "missing" / "icon.png",
            ):
                missing_icon_page = window._build_about_tab()
            self.assertIsNotNone(missing_icon_page.layout())
            self.assertFalse(window.about_icon_label.property("iconPngLoaded"))
            missing_icon_page.deleteLater()
            window.close()

    # Check open install folder targets selected
    def test_open_install_folder_targets_selected_games_mods_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pak_directory = root / "CodeVein2" / "Content" / "Paks"
            pak_directory.mkdir(parents=True)
            (pak_directory / "pakchunk0-Windows.pak").write_bytes(b"archive")
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.setText(str(root))

            with patch.object(window, "_open_folder") as open_folder:
                window._open_install_folder()

            open_folder.assert_called_once_with(pak_directory / "~mods")
            self.assertTrue((pak_directory / "~mods").is_dir())
            window.close()
    # Check generate install use visible manual
    def test_generate_install_uses_the_visible_manual_seed_and_finishes_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = _FakeEngine(root / "generation")
            window = self._ready_window(
                generation_engine=engine,
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
                cache_path=root / "cache",
            )
            window.game_path.setText(str(root / "game"))
            window.randomize_healing_check.setChecked(False)
            window.seed_input.setText("ui-seed")

            with patch(
                "cv2randomizer.ui.main_window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ) as confirm_replace:
                window.generate_install_button.click()
                self._wait_for_task(window)
                self.assertEqual(confirm_replace.call_args.args[1], "Replace installed Randomizer?")
                self.assertIn("overwrite the currently installed mod", confirm_replace.call_args.args[2])
                self.assertNotIn("backed up", confirm_replace.call_args.args[2])
                self.assertNotIn(str(root), confirm_replace.call_args.args[2])
                self.assertEqual(
                    confirm_replace.call_args.args[4],
                    QMessageBox.StandardButton.No,
                )
            self.assertTrue(engine.requests[-1].options.install_after_generation)
            self.assertTrue(engine.requests[-1].options.randomize_world_loot)
            self.assertFalse(engine.requests[-1].options.randomize_healing)
            self.assertTrue(engine.requests[-1].options.randomize_bequeathed_formae_quest_rewards)
            self.assertEqual(engine.requests[-1].seed, "ui-seed")
            self.assertEqual(window.seed_input.text(), "ui-seed")
            self.assertEqual(engine.requests[-1].game_data_cache_entry, root / "cache")
            self.assertTrue(engine.requests[-1].replace_existing_randomizer)
            self.assertEqual(window.progress_bar.value(), 100)
            self.assertEqual(window.status_label.text(), "Randomizer completed")
            self.assertFalse(hasattr(window, "_latest_reproducibility_label"))
            first_options = engine.requests[-1].options
            with patch(
                "cv2randomizer.ui.main_window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window.generate_install_button.click()
                self._wait_for_task(window)
            self.assertEqual(engine.requests[-1].seed, "ui-seed")
            self.assertEqual(engine.requests[-1].options, first_options)
            self.assertEqual(window.seed_input.text(), "ui-seed")
            self.assertIsNone(SettingsStore(root / "settings.json").load().mappings_path)
            window.close()

    # Check cancel replace confirmation does not
    def test_cancel_replace_confirmation_does_not_start_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = _FakeEngine(root / "generation")
            window = self._ready_window(
                generation_engine=engine,
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.setText(str(root / "game"))
            with patch(
                "cv2randomizer.ui.main_window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as confirm_replace:
                window.generate_install_button.click()

            self.assertIn("overwrite the currently installed mod", confirm_replace.call_args.args[2])
            self.assertNotIn(str(root), confirm_replace.call_args.args[2])
            self.assertEqual(engine.requests, [])
            self.assertIsNone(window._worker)
            window.close()

    # Check generation error shows concise actionable
    def test_generation_error_shows_a_concise_actionable_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = self._ready_window(
                generation_engine=_FailingEngine(),
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.setText(str(root / "game"))
            with (
                patch(
                    "cv2randomizer.ui.main_window.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch("cv2randomizer.ui.main_window.QMessageBox.critical") as show_error,
            ):
                window.generate_install_button.click()
                self._wait_for_task(window)
            self.assertEqual(window.status_label.text(), "Task failed. See the activity log for details.")
            self.assertIn("unsupported fixture build", show_error.call_args.args[2])
            window.close()

    # Check cancel requests cooperative stop at
    def test_cancel_requests_cooperative_stop_at_pipeline_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = _BlockingEngine()
            window = self._ready_window(
                generation_engine=engine,
                installer=_FakeInstaller(),
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.game_path.setText(str(root / "game"))
            with patch(
                "cv2randomizer.ui.main_window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window.generate_install_button.click()
            self.assertTrue(engine.started.wait(2), "generation did not reach its first checkpoint")
            window.cancel_button.click()
            engine.release.set()
            self._wait_for_task(window)
            self.assertIn("Generation cancelled", window.status_label.text())
            self.assertEqual(window.progress_bar.value(), 0)
            window.close()

    # Check remove button runs background reports
    def test_remove_button_runs_in_background_and_reports_no_registered_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = _FakeInstaller()
            window = self._ready_window(
                generation_engine=_FakeEngine(root / "generation"),
                installer=installer,
                settings_store=SettingsStore(root / "settings.json"),
            )
            window.remove_button.click()
            self._wait_for_task(window)
            self.assertEqual(installer.game_roots, [None])
            self.assertIn("No installed Randomizer", window.status_label.text())
            window.close()


if __name__ == "__main__":
    unittest.main()



