from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cv2randomizer.packaging.installer import InstallManager, InstallationError
from cv2randomizer.packaging.retoc import PackageResult


class InstallManagerTests(unittest.TestCase):
    # Read game
    def _game(self, root: Path) -> tuple[Path, Path, bytes]:
        game_root = root / "game"
        paks = game_root / "CodeVein2" / "Content" / "Paks"
        paks.mkdir(parents=True)
        vanilla = paks / "pakchunk0-Windows.utoc"
        vanilla_bytes = b"vanilla container must remain untouched"
        vanilla.write_bytes(vanilla_bytes)
        return game_root, paks, vanilla_bytes

    # Resolve a package asset path
    def _package(self, root: Path, version: str) -> PackageResult:
        package_root = root / ("package-" + version)
        package_root.mkdir()
        files = tuple(package_root / ("CV2Randomizer_P" + extension) for extension in (".pak", ".utoc", ".ucas"))
        for extension, path in zip((".pak", ".utoc", ".ucas"), files, strict=True):
            path.write_bytes((version + extension).encode())
        return PackageResult(
            "CV2Randomizer_P", files,
            ("../../../CodeVein2/Content/Blueprint/GameSystem/Shop/DT_ShopLineup.uasset",),
        )

    # Read spoiler
    def _spoiler(self, root: Path, text: str) -> Path:
        path = root / f"spoiler-{text}.txt"
        path.write_text(f"Seed: {text}\n", encoding="utf-8")
        return path

    # Check installs updates removes owned containers
    def test_installs_updates_and_removes_only_owned_containers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, vanilla_bytes = self._game(root)
            unrelated = paks / "~mods" / "OtherMod_P.pak"
            unrelated.parent.mkdir()
            unrelated.write_bytes(b"other mod")
            manager = InstallManager(root / "app-state")
            first = self._package(root, "first")
            installed = manager.install(first, game_root=game_root)
            self.assertEqual(installed.package_name, "CV2Randomizer_P")
            self.assertEqual(len(installed.files), 3)
            self.assertTrue(manager.state_path.is_file())
            self.assertEqual((paks / "pakchunk0-Windows.utoc").read_bytes(), vanilla_bytes)
            self.assertEqual(unrelated.read_bytes(), b"other mod")

            replacement = self._package(root, "replacement")
            manager.install(replacement, game_root=game_root)
            self.assertEqual((paks / "~mods" / "CV2Randomizer_P.ucas").read_bytes(), b"replacement.ucas")

            removed = manager.remove(game_root=game_root)
            self.assertEqual(removed.package_name, "CV2Randomizer_P")
            self.assertEqual({path.suffix for path in removed.removed_files}, {".pak", ".utoc", ".ucas"})
            self.assertFalse(manager.state_path.exists())
            self.assertFalse(any(path.exists() for path in installed.files))
            self.assertTrue(unrelated.is_file())
            self.assertEqual(unrelated.read_bytes(), b"other mod")
            self.assertEqual((paks / "pakchunk0-Windows.utoc").read_bytes(), vanilla_bytes)
            self.assertTrue((paks / "~mods").is_dir())

    # Keep multiple game installs separate
    def test_installs_and_uninstalls_multiple_game_roots_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_game, first_paks, _ = self._game(root / "first")
            second_game, second_paks, _ = self._game(root / "second")
            manager = InstallManager(root / "app-state")

            first_package = self._package(root, "first")
            second_package = self._package(root, "second")
            first_mods = first_paks / "~mods"
            first_mods.mkdir()
            for source in first_package.files:
                shutil.copyfile(source, first_mods / source.name)
            legacy_state = {
                "schema": "cv2-randomizer-installed-mod-v1",
                "package_name": first_package.package_name,
                "game_root": str(first_game.resolve()),
                "files": {
                    source.name: hashlib.sha256(source.read_bytes()).hexdigest()
                    for source in first_package.files
                },
            }
            legacy_state_path = root / "app-state" / "installed_mod.json"
            legacy_state_path.parent.mkdir(parents=True)
            legacy_state_path.write_text(json.dumps(legacy_state), encoding="utf-8")

            second_install = manager.install(second_package, game_root=second_game)

            self.assertTrue(all((first_mods / source.name).is_file() for source in first_package.files))
            self.assertTrue(all(path.is_file() for path in second_install.files))
            self.assertNotEqual(manager.state_path, legacy_state_path)
            self.assertEqual(
                json.loads(legacy_state_path.read_text(encoding="utf-8"))["game_root"],
                str(first_game.resolve()),
            )
            with self.assertRaisesRegex(InstallationError, "Select a game path before uninstalling"):
                manager.remove()

            first_removal = manager.remove(game_root=first_game)
            self.assertEqual(first_removal.game_root, first_game.resolve())
            self.assertFalse(any((first_mods / source.name).exists() for source in first_package.files))
            self.assertTrue(all(path.is_file() for path in second_install.files))

            second_removal = manager.remove(game_root=second_game)
            self.assertEqual(second_removal.game_root, second_game.resolve())
            self.assertFalse(any(path.exists() for path in second_install.files))

    # Check refuses overwrite conflicting unowned file
    def test_refuses_to_overwrite_a_conflicting_unowned_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, _ = self._game(root)
            mods = paks / "~mods"
            mods.mkdir()
            conflict = mods / "CV2Randomizer_P.pak"
            conflict.write_bytes(b"manual package")
            manager = InstallManager(root / "app-state")

            with self.assertRaisesRegex(InstallationError, "not owned by this app"):
                manager.install(self._package(root, "new"), game_root=game_root)

            self.assertEqual(conflict.read_bytes(), b"manual package")
            self.assertFalse((mods / "CV2Randomizer_P.ucas").exists())
            self.assertFalse(manager.state_path.exists())

    # Check confirmed unowned randomizer files are
    def test_confirmed_unowned_randomizer_files_are_backed_up_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, _ = self._game(root)
            mods = paks / "~mods"
            mods.mkdir()
            old_files = {
                "CV2Randomizer_P.pak": b"previous randomizer pak",
                "CV2Randomizer_P.utoc": b"previous randomizer utoc",
                "CV2Randomizer_P.ucas": b"previous randomizer ucas",
                "Spoiler.txt": b"previous spoiler",
            }
            for filename, contents in old_files.items():
                (mods / filename).write_bytes(contents)
            unrelated = mods / "OtherMod_P.pak"
            unrelated.write_bytes(b"unrelated mod")
            manager = InstallManager(root / "app-state")

            result = manager.install(
                self._package(root, "new"),
                game_root=game_root,
                spoiler_path=self._spoiler(root, "new"),
                allow_unowned_replacement=True,
            )

            self.assertIsNotNone(result.recovery_backup)
            backup = result.recovery_backup
            self.assertTrue(backup.is_dir())
            for filename, contents in old_files.items():
                self.assertEqual((backup / filename).read_bytes(), contents)
            recovery = json.loads((backup / "recovery.json").read_text(encoding="utf-8"))
            self.assertIsNone(recovery["previous_state"])
            self.assertEqual(set(recovery["files"]), set(old_files))
            self.assertEqual((mods / "CV2Randomizer_P.pak").read_bytes(), b"new.pak")
            self.assertEqual((mods / "Spoiler.txt").read_text(encoding="utf-8"), "Seed: new\n")
            self.assertEqual(unrelated.read_bytes(), b"unrelated mod")
            self.assertTrue(manager.state_path.is_file())

    # Check changed owned file blocks remove
    def test_changed_owned_file_blocks_remove_without_deleting_any_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, _ = self._game(root)
            manager = InstallManager(root / "app-state")
            result = manager.install(self._package(root, "first"), game_root=game_root)
            changed = paks / "~mods" / "CV2Randomizer_P.utoc"
            changed.write_bytes(b"changed outside app")

            with self.assertRaisesRegex(InstallationError, "changed outside the app"):
                manager.remove(game_root=game_root)

            self.assertTrue(all(path.is_file() for path in result.files))
            self.assertEqual(changed.read_bytes(), b"changed outside app")
            self.assertTrue(manager.state_path.is_file())

    # Check modified owned container is backed
    def test_modified_owned_container_is_backed_up_before_controlled_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, _ = self._game(root)
            manager = InstallManager(root / "app-state")
            first = manager.install(self._package(root, "first"), game_root=game_root)
            unrelated = paks / "~mods" / "OtherMod_P.pak"
            unrelated.write_bytes(b"unrelated mod")
            changed = paks / "~mods" / "CV2Randomizer_P.ucas"
            changed.write_bytes(b"manual test variant ucas")
            changed_utoc = paks / "~mods" / "CV2Randomizer_P.utoc"
            changed_utoc.write_bytes(b"manual test variant utoc")

            manager.install(self._package(root, "replacement"), game_root=game_root)

            installed = paks / "~mods"
            self.assertEqual((installed / "CV2Randomizer_P.pak").read_bytes(), b"replacement.pak")
            self.assertEqual((installed / "CV2Randomizer_P.ucas").read_bytes(), b"replacement.ucas")
            self.assertEqual((installed / "CV2Randomizer_P.utoc").read_bytes(), b"replacement.utoc")
            self.assertEqual(unrelated.read_bytes(), b"unrelated mod")
            backup_directories = list((root / "app-state" / "recovery_backups").iterdir())
            self.assertEqual(len(backup_directories), 1)
            backup = backup_directories[0]
            self.assertEqual((backup / "CV2Randomizer_P.ucas").read_bytes(), b"manual test variant ucas")
            self.assertEqual((backup / "CV2Randomizer_P.utoc").read_bytes(), b"manual test variant utoc")
            self.assertEqual((backup / "CV2Randomizer_P.pak").read_bytes(), b"first.pak")
            manifest = json.loads((backup / "recovery.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], "cv2-randomizer-install-recovery-v1")
            self.assertEqual(
                manifest["files"]["CV2Randomizer_P.ucas"],
                hashlib.sha256(b"manual test variant ucas").hexdigest(),
            )
            self.assertEqual(set(manifest["files"]), {path.name for path in first.files})

    # Check update refuses modified owned trio
    def test_update_refuses_modified_owned_trio_without_an_unchanged_identity_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, _ = self._game(root)
            manager = InstallManager(root / "app-state")
            manager.install(self._package(root, "first"), game_root=game_root)
            mods = paks / "~mods"
            for extension in (".pak", ".utoc", ".ucas"):
                (mods / f"CV2Randomizer_P{extension}").write_bytes(f"changed{extension}".encode())

            with self.assertRaisesRegex(InstallationError, "no unchanged owned container"):
                manager.install(self._package(root, "replacement"), game_root=game_root)

            self.assertEqual((mods / "CV2Randomizer_P.pak").read_bytes(), b"changed.pak")
            self.assertEqual((mods / "CV2Randomizer_P.utoc").read_bytes(), b"changed.utoc")
            self.assertEqual((mods / "CV2Randomizer_P.ucas").read_bytes(), b"changed.ucas")
            self.assertFalse((root / "app-state" / "recovery_backups").exists())

    # Check install retries transient access denial
    def test_install_retries_a_transient_access_denial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, _ = self._game(root)
            manager = InstallManager(root / "app-state")
            manager.install(self._package(root, "first"), game_root=game_root)
            replacement = self._package(root, "replacement")
            actual_replace = os.replace
            denials = 0

            # Read transient denial
            def transient_denial(source, target):
                nonlocal denials
                if Path(target).name == "CV2Randomizer_P.pak" and denials == 0:
                    denials += 1
                    raise PermissionError(5, "Access is denied", str(target))
                return actual_replace(source, target)

            with (
                patch("cv2randomizer.packaging.installer.os.replace", side_effect=transient_denial),
                patch("cv2randomizer.packaging.installer.time.sleep"),
            ):
                manager.install(replacement, game_root=game_root)

            self.assertEqual(denials, 1)
            self.assertEqual((paks / "~mods" / "CV2Randomizer_P.pak").read_bytes(), b"replacement.pak")

    # Check install access denial explains lock
    def test_install_access_denial_explains_lock_and_preserves_previous_trio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, _ = self._game(root)
            manager = InstallManager(root / "app-state")
            manager.install(self._package(root, "first"), game_root=game_root)
            mods = paks / "~mods"
            old_contents = {
                extension: (mods / f"CV2Randomizer_P{extension}").read_bytes()
                for extension in (".pak", ".utoc", ".ucas")
            }
            actual_replace = os.replace
            attempts = 0

            # Read persistent denial
            def persistent_denial(source, target):
                nonlocal attempts
                if Path(target).name == "CV2Randomizer_P.pak" and str(source).endswith(".cv2-tmp"):
                    attempts += 1
                    raise PermissionError(5, "Access is denied", str(target))
                return actual_replace(source, target)

            with (
                patch("cv2randomizer.packaging.installer.os.replace", side_effect=persistent_denial),
                patch("cv2randomizer.packaging.installer.time.sleep"),
                self.assertRaisesRegex(InstallationError, "Close the game and retry Randomize"),
            ):
                manager.install(self._package(root, "replacement"), game_root=game_root)

            self.assertEqual(attempts, 3)
            self.assertEqual(
                {
                    extension: (mods / f"CV2Randomizer_P{extension}").read_bytes()
                    for extension in (".pak", ".utoc", ".ucas")
                },
                old_contents,
            )
            self.assertFalse(any(mods.glob("*.cv2-tmp")))
            self.assertTrue(manager.state_path.is_file())

    # Check remove without ownership record does
    def test_remove_without_an_ownership_record_does_not_guess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, _ = self._game(root)
            mods = paks / "~mods"
            mods.mkdir()
            manual = mods / "CV2Randomizer_P.pak"
            manual.write_bytes(b"manual file")
            result = InstallManager(root / "app-state").remove(game_root=game_root)
            self.assertEqual(result.removed_files, ())
            self.assertEqual(manual.read_bytes(), b"manual file")

    # Check spoiler is installed owned removed
    def test_spoiler_is_installed_owned_and_removed_with_the_randomizer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, _paks, _ = self._game(root)
            manager = InstallManager(root / "app-state")
            spoiler = self._spoiler(root, "first")

            first = manager.install(
                self._package(root, "first"), game_root=game_root, spoiler_path=spoiler,
            )
            installed_spoiler = first.mods_directory / "Spoiler.txt"
            self.assertTrue(installed_spoiler.is_file())
            self.assertEqual(installed_spoiler.read_bytes(), spoiler.read_bytes())
            state = manager._read_state(manager.state_path)
            self.assertEqual(state["spoiler_file"]["filename"], installed_spoiler.name)

            replacement_spoiler = self._spoiler(root, "replacement")
            manager.install(
                self._package(root, "replacement"),
                game_root=game_root,
                spoiler_path=replacement_spoiler,
            )
            self.assertEqual(installed_spoiler.read_bytes(), replacement_spoiler.read_bytes())

            removed = manager.remove(game_root=game_root)
            self.assertIn(installed_spoiler, removed.removed_files)
            self.assertFalse(installed_spoiler.exists())

    # Check spoiler conflict is not overwritten
    def test_spoiler_conflict_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, paks, _ = self._game(root)
            mods = paks / "~mods"
            mods.mkdir()
            conflict = mods / "Spoiler.txt"
            conflict.write_text("belongs to another mod", encoding="utf-8")
            manager = InstallManager(root / "app-state")

            with self.assertRaisesRegex(InstallationError, "not owned by this app"):
                manager.install(
                    self._package(root, "new"),
                    game_root=game_root,
                    spoiler_path=self._spoiler(root, "new"),
                )

            self.assertEqual(conflict.read_text(encoding="utf-8"), "belongs to another mod")
            self.assertFalse((mods / "CV2Randomizer_P.pak").exists())

    # Check install migrates owned legacy spoiler
    def test_install_migrates_owned_legacy_spoiler_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, _paks, _ = self._game(root)
            manager = InstallManager(root / "app-state")
            old_spoiler = self._spoiler(root, "old")
            first = manager.install(
                self._package(root, "old"), game_root=game_root, spoiler_path=old_spoiler,
            )
            previous_path = first.mods_directory / "Spoiler.txt"
            legacy_path = first.mods_directory / "CV2Randomizer_Spoiler.txt"
            previous_path.replace(legacy_path)
            state = manager._read_state(manager.state_path)
            state["spoiler_file"]["filename"] = legacy_path.name
            manager._write_state(manager.state_path, state)

            replacement_spoiler = self._spoiler(root, "new")
            replacement = manager.install(
                self._package(root, "new"), game_root=game_root, spoiler_path=replacement_spoiler,
            )

            self.assertFalse(legacy_path.exists())
            installed_spoiler = replacement.mods_directory / "Spoiler.txt"
            self.assertTrue(installed_spoiler.is_file())
            self.assertEqual(installed_spoiler.read_bytes(), replacement_spoiler.read_bytes())
            self.assertEqual(manager._read_state(manager.state_path)["spoiler_file"]["filename"], "Spoiler.txt")

    # Check install preserve modified legacy spoiler
    def test_install_preserves_modified_legacy_spoiler_and_refuses_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root, _paks, _ = self._game(root)
            manager = InstallManager(root / "app-state")
            first = manager.install(
                self._package(root, "old"),
                game_root=game_root,
                spoiler_path=self._spoiler(root, "old"),
            )
            legacy_path = first.mods_directory / "CV2Randomizer_Spoiler.txt"
            current = first.mods_directory / "Spoiler.txt"
            current.replace(legacy_path)
            legacy_path.write_text("user-edited spoiler", encoding="utf-8")
            state = manager._read_state(manager.state_path)
            state["spoiler_file"]["filename"] = legacy_path.name
            manager._write_state(manager.state_path, state)

            with self.assertRaisesRegex(InstallationError, "modified legacy Randomizer spoiler"):
                manager.install(
                    self._package(root, "new"),
                    game_root=game_root,
                    spoiler_path=self._spoiler(root, "new"),
                )

            self.assertEqual(legacy_path.read_text(encoding="utf-8"), "user-edited spoiler")
            self.assertFalse(current.exists())


if __name__ == "__main__":
    unittest.main()
