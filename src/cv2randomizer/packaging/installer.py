"""Owned-file installation and removal for generated randomizer containers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir

from cv2randomizer import APP_NAME
from cv2randomizer.game.locator import check_installation
from cv2randomizer.packaging.retoc import PackageResult

_STATE_SCHEMA = "cv2-randomizer-installed-mod-v1"
_MOD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}_P\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PACKAGE_EXTENSIONS = (".pak", ".utoc", ".ucas")
_SPOILER_FILENAME = "Spoiler.txt"
_LEGACY_SPOILER_FILENAMES = frozenset({"CV2Randomizer_Spoiler.txt"})
_REPLACE_ATTEMPTS = 3


class InstallationError(RuntimeError):
    """Raised when install state or destination files are not safe to modify."""


@dataclass(frozen=True, slots=True)
class InstallationResult:
    """Files installed under the selected game's Paks/~mods folder."""

    package_name: str
    game_root: Path
    mods_directory: Path
    files: tuple[Path, ...]
    recovery_backup: Path | None = None


@dataclass(frozen=True, slots=True)
class RemovalResult:
    """Owned files removed from a prior installation, if any."""

    package_name: str | None
    game_root: Path | None
    removed_files: tuple[Path, ...]


class InstallManager:
    """Install only package filenames registered to this app, with safe recovery."""

    # Initialize this object
    def __init__(self, state_root: Path | None = None) -> None:
        self.state_root = (state_root or Path(user_data_dir(APP_NAME)) / "installation").expanduser()
        self.state_path = self.state_root / "installed_mod.json"

    # Install the generated mod
    def install(
        self,
        package: PackageResult,
        *,
        game_root: Path,
        spoiler_path: Path | None = None,
        allow_unowned_replacement: bool = False,
    ) -> InstallationResult:
        """Atomically install containers and, when supplied, their spoiler.

        Unowned matching files may only be adopted after explicit user
        confirmation. Existing files are copied to an app-data recovery backup
        before replacement.
        """
        package_name, package_sources, new_hashes = _validate_package(package)
        spoiler_source, spoiler_hash = _validate_spoiler(spoiler_path)
        root, pak_directory = _validated_game_root(game_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        state_path = self._state_path_for(root)
        self.state_path = state_path
        previous = self._read_state(state_path)
        if previous is not None:
            if _path_key(Path(previous["game_root"])) != _path_key(root):
                raise InstallationError("The selected game's Randomizer ownership record points to another installation.")
            if previous["package_name"] != package_name:
                raise InstallationError("The registered package name does not match the package being installed.")

        mods_directory = pak_directory / "~mods"
        if mods_directory.is_symlink():
            raise InstallationError("Refusing to use a symlink as the game's ~mods directory.")
        mods_directory.mkdir(exist_ok=True)
        mods_root = mods_directory.resolve(strict=True)
        if not _is_within(mods_root, pak_directory.resolve(strict=True)):
            raise InstallationError("Resolved ~mods directory escaped the game's Paks folder.")

        old_hashes = dict(previous["files"]) if previous is not None else {}
        previous_spoiler = previous.get("spoiler_file") if previous is not None else None
        if previous_spoiler is not None:
            old_hashes[previous_spoiler["filename"]] = previous_spoiler["sha256"]
        spoiler_record = (
            {"filename": _SPOILER_FILENAME, "sha256": spoiler_hash}
            if spoiler_source is not None and spoiler_hash is not None
            else previous_spoiler
        )
        targets = {filename: mods_root / filename for filename in package_sources}
        if spoiler_record is not None:
            targets[spoiler_record["filename"]] = mods_root / spoiler_record["filename"]
        legacy_spoiler_names: set[str] = set()
        if (
            spoiler_source is not None
            and previous_spoiler is not None
            and previous_spoiler["filename"] in _LEGACY_SPOILER_FILENAMES
            and previous_spoiler["filename"] != _SPOILER_FILENAME
        ):
            legacy_name = previous_spoiler["filename"]
            legacy_spoiler_names.add(legacy_name)
            targets[legacy_name] = mods_root / legacy_name
        staged_sources = dict(package_sources)
        if spoiler_source is not None:
            staged_sources[_SPOILER_FILENAME] = spoiler_source
        backups: dict[str, Path] = {}
        existed_before: set[str] = set()
        modified_owned: set[str] = set()
        unowned_existing: set[str] = set()
        for filename, target in targets.items():
            if target.is_symlink():
                raise InstallationError(f"Refusing to replace a symlink in ~mods: {filename}.")
            if not target.exists():
                continue
            if not target.is_file():
                raise InstallationError(f"Refusing to replace a non-file package path: {filename}.")
            previous_hash = old_hashes.get(filename)
            if previous_hash is None:
                if not allow_unowned_replacement:
                    raise InstallationError(f"Refusing to replace a package not owned by this app: {filename}.")
                existed_before.add(filename)
                unowned_existing.add(filename)
                continue
            existed_before.add(filename)
            if _sha256_file(target) != previous_hash:
                modified_owned.add(filename)

        modified_legacy_spoilers = legacy_spoiler_names & modified_owned
        if modified_legacy_spoilers:
            names = ", ".join(sorted(modified_legacy_spoilers))
            raise InstallationError(
                "Refusing to remove a modified legacy Randomizer spoiler. Back it up and remove it manually, "
                f"then generate again: {names}."
            )

        recovery_backup: Path | None = None
        if modified_owned or unowned_existing:
            if _SPOILER_FILENAME in modified_owned and spoiler_source is None:
                raise InstallationError(
                    "Refusing to replace a modified Randomizer spoiler without a new spoiler file."
                )
            if modified_owned:
                unchanged_containers = (set(new_hashes) & existed_before) - modified_owned
                if not unchanged_containers:
                    changed_names = ", ".join(sorted(modified_owned))
                    raise InstallationError(
                        "Refusing to replace modified Randomizer files because no unchanged owned container "
                        f"confirms the installed package identity: {changed_names}. "
                        "Back up and remove the old Randomizer trio manually, then generate again."
                    )
            recovery_backup = self._backup_modified_install(
                targets=targets,
                filenames=existed_before,
                previous_state=previous,
                game_root=root,
                package_name=package_name,
            )

        try:
            with tempfile.TemporaryDirectory(prefix=".cv2-install-backup-", dir=self.state_root) as temporary:
                backup_root = Path(temporary)
                for filename in existed_before:
                    backup = backup_root / filename
                    shutil.copyfile(targets[filename], backup)
                    if _sha256_file(targets[filename]) != _sha256_file(backup):
                        raise InstallationError(f"Could not preserve the previous owned package: {filename}.")
                    backups[filename] = backup

                committed: list[str] = []
                prepared: list[Path] = []
                try:
                    for filename, source in staged_sources.items():
                        target = targets[filename]
                        temporary_file = mods_root / f".{filename}.{uuid.uuid4().hex}.cv2-tmp"
                        prepared.append(temporary_file)
                        shutil.copyfile(source, temporary_file)
                        if _sha256_file(source) != _sha256_file(temporary_file):
                            raise InstallationError(f"Package copy failed integrity validation: {filename}.")
                    for temporary_file, filename in zip(prepared, staged_sources, strict=True):
                        _replace_staged_file(temporary_file, targets[filename])
                        committed.append(filename)
                    for filename, expected_hash in new_hashes.items():
                        target = targets[filename]
                        if not target.is_file() or _sha256_file(target) != expected_hash:
                            raise InstallationError(
                                f"Installed package failed final hash verification: {filename}."
                            )
                    if spoiler_record is not None:
                        spoiler_target = targets[spoiler_record["filename"]]
                        if (
                            not spoiler_target.is_file()
                            or _sha256_file(spoiler_target) != spoiler_record["sha256"]
                        ):
                            raise InstallationError("Installed spoiler failed final hash verification.")
                    for filename in sorted(legacy_spoiler_names):
                        if filename in existed_before:
                            targets[filename].unlink()
                            committed.append(filename)
                    state = {
                        "schema": _STATE_SCHEMA,
                        "package_name": package_name,
                        "game_root": str(root),
                        "files": new_hashes,
                    }
                    if spoiler_record is not None:
                        state["spoiler_file"] = spoiler_record
                    self._write_state(state_path, state)
                except Exception:
                    for filename in reversed(committed):
                        target = targets[filename]
                        backup = backups.get(filename)
                        if backup is not None:
                            restore = mods_root / f".{filename}.{uuid.uuid4().hex}.cv2-restore"
                            shutil.copyfile(backup, restore)
                            if _sha256_file(backup) != _sha256_file(restore):
                                raise InstallationError(f"Rollback copy failed integrity validation: {filename}.")
                            restore.replace(target)
                        else:
                            target.unlink(missing_ok=True)
                    raise
                finally:
                    for path in prepared:
                        path.unlink(missing_ok=True)
        except InstallationError:
            raise
        except OSError as error:
            raise InstallationError(f"Could not install the randomizer containers: {error}") from error
        installed_names = list(package_sources)
        if spoiler_record is not None:
            installed_names.append(spoiler_record["filename"])
        return InstallationResult(
            package_name,
            root,
            mods_root,
            tuple(targets[name] for name in installed_names),
            recovery_backup,
        )

    # Back up changed mod files
    def _backup_modified_install(
        self,
        *,
        targets: dict[str, Path],
        filenames: set[str],
        previous_state: dict[str, Any] | None,
        game_root: Path,
        package_name: str,
    ) -> Path:
        """Preserve the prior app-registered package before a controlled repair."""
        backup_directory: Path | None = None
        try:
            backup_root = self.state_root / "recovery_backups"
            if backup_root.is_symlink():
                raise InstallationError("Randomizer recovery backup directory must not be a symlink.")
            backup_root.mkdir(parents=True, exist_ok=True)
            resolved_backup_root = backup_root.resolve(strict=True)
            if not _is_within(resolved_backup_root, self.state_root.resolve(strict=True)):
                raise InstallationError("Randomizer recovery backup directory escaped application data.")

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_directory = resolved_backup_root / f"replacement-{timestamp}-{uuid.uuid4().hex[:8]}"
            backup_directory.mkdir()
            copied_hashes: dict[str, str] = {}
            for filename in sorted(filenames):
                source = targets[filename]
                expected_hash = _sha256_file(source)
                backup = backup_directory / filename
                shutil.copyfile(source, backup)
                if _sha256_file(source) != expected_hash or _sha256_file(backup) != expected_hash:
                    raise InstallationError(
                        f"Could not preserve the existing Randomizer file before replacement: {filename}."
                    )
                copied_hashes[filename] = expected_hash
            manifest = {
                "schema": "cv2-randomizer-install-recovery-v1",
                "package_name": package_name,
                "game_root": str(game_root),
                "previous_state": previous_state,
                "files": copied_hashes,
            }
            (backup_directory / "recovery.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as error:
            if backup_directory is not None:
                shutil.rmtree(backup_directory, ignore_errors=True)
            if isinstance(error, InstallationError):
                raise
            if isinstance(error, OSError):
                raise InstallationError(f"Could not back up the existing Randomizer files: {error}") from error
            raise
        return backup_directory

    # Remove Randomizer-owned mod files
    def remove(self, *, game_root: Path | None = None) -> RemovalResult:
        """Remove only unchanged files whose ownership hashes are in app state."""
        state_path = self._state_path_for(game_root.expanduser().resolve()) if game_root is not None else self._single_state_path()
        self.state_path = state_path
        state = self._read_state(state_path)
        if state is None:
            return RemovalResult(None, None, ())
        state_root = Path(state["game_root"]).expanduser().resolve()
        if game_root is not None:
            requested_root = game_root.expanduser().resolve()
            if _path_key(requested_root) != _path_key(state_root):
                raise InstallationError("The requested game path differs from the registered installation.")
        root, pak_directory = _validated_game_root(state_root)
        mods_directory = pak_directory / "~mods"
        if mods_directory.is_symlink():
            raise InstallationError("Refusing to remove packages through a symlinked ~mods directory.")
        mods_root = mods_directory.resolve() if mods_directory.exists() else mods_directory
        if mods_directory.exists() and not _is_within(mods_root, pak_directory.resolve(strict=True)):
            raise InstallationError("Resolved ~mods directory escaped the game's Paks folder.")

        owned_hashes = dict(state["files"])
        spoiler_record = state.get("spoiler_file")
        if spoiler_record is not None:
            owned_hashes[spoiler_record["filename"]] = spoiler_record["sha256"]
        targets = {filename: mods_root / filename for filename in owned_hashes}
        removable: list[Path] = []
        for filename, target in targets.items():
            if target.is_symlink():
                raise InstallationError(f"Refusing to remove a symlink in ~mods: {filename}.")
            if not target.exists():
                continue
            if not target.is_file() or _sha256_file(target) != owned_hashes[filename]:
                raise InstallationError(f"Owned package was changed outside the app; leaving all files untouched: {filename}.")
            removable.append(target)

        self.state_root.mkdir(parents=True, exist_ok=True)
        removed: list[Path] = []
        try:
            with tempfile.TemporaryDirectory(prefix=".cv2-remove-backup-", dir=self.state_root) as temporary:
                backups: dict[Path, Path] = {}
                for target in removable:
                    backup = Path(temporary) / target.name
                    shutil.copyfile(target, backup)
                    if _sha256_file(target) != _sha256_file(backup):
                        raise InstallationError(f"Could not preserve an owned package before removal: {target.name}.")
                    backups[target] = backup
                try:
                    for target in removable:
                        target.unlink()
                        removed.append(target)
                    state_path.unlink(missing_ok=True)
                except Exception:
                    for target in reversed(removed):
                        backup = backups[target]
                        restore = target.with_name(f".{target.name}.{uuid.uuid4().hex}.cv2-restore")
                        shutil.copyfile(backup, restore)
                        if _sha256_file(backup) != _sha256_file(restore):
                            raise InstallationError(f"Removal rollback copy failed integrity validation: {target.name}.")
                        restore.replace(target)
                    raise
        except InstallationError:
            raise
        except OSError as error:
            raise InstallationError(f"Could not remove the randomizer containers: {error}") from error
        return RemovalResult(state["package_name"], root, tuple(removed))

    # Resolve installation ownership path
    def _state_path_for(self, game_root: Path) -> Path:
        registered_path = self._safe_state_path(game_root)
        if registered_path.exists():
            return registered_path

        legacy_path = self._safe_state_path()
        legacy_state = self._read_state(legacy_path)
        if legacy_state is not None and _path_key(Path(legacy_state["game_root"])) == _path_key(game_root):
            return legacy_path
        return registered_path

    # Keep legacy state lookup safe
    def _single_state_path(self) -> Path:
        legacy_path = self._safe_state_path()
        registered_paths: list[Path] = []
        if legacy_path.exists():
            self._read_state(legacy_path)
            registered_paths.append(legacy_path)

        state_root = self.state_root.resolve(strict=True)
        registrations = state_root / "installations"
        if registrations.is_symlink():
            raise InstallationError("Installation state directory must not be a symlink.")
        if registrations.exists():
            registrations = registrations.resolve(strict=True)
            if not _is_within(registrations, state_root):
                raise InstallationError("Installation state directory escaped application data.")
            for path in registrations.glob("*.json"):
                if path.is_symlink():
                    raise InstallationError("Installation state file must not be a symlink.")
                if self._read_state(path) is not None:
                    registered_paths.append(path)

        if len(registered_paths) > 1:
            raise InstallationError("Multiple game installations have Randomizer files. Select a game path before uninstalling.")
        return registered_paths[0] if registered_paths else legacy_path

    # Resolve a safe state file path
    def _safe_state_path(self, game_root: Path | None = None) -> Path:
        self.state_root.mkdir(parents=True, exist_ok=True)
        if self.state_root.is_symlink():
            raise InstallationError("Installation state directory must not be a symlink.")
        state_root = self.state_root.resolve(strict=True)
        if game_root is None:
            path = state_root / "installed_mod.json"
        else:
            registrations = state_root / "installations"
            if registrations.is_symlink():
                raise InstallationError("Installation state directory must not be a symlink.")
            registrations.mkdir(exist_ok=True)
            registrations = registrations.resolve(strict=True)
            if not _is_within(registrations, state_root):
                raise InstallationError("Installation state directory escaped application data.")
            key = hashlib.sha256(_path_key(game_root).encode("utf-8")).hexdigest()
            path = registrations / f"{key}.json"
        if path.is_symlink():
            raise InstallationError("Installation state file must not be a symlink.")
        return path

    # Read state
    def _read_state(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise InstallationError(f"Could not read randomizer ownership state: {error}") from error
        if not isinstance(document, dict) or document.get("schema") != _STATE_SCHEMA:
            raise InstallationError("Randomizer ownership state has an unknown or invalid schema.")
        package_name = document.get("package_name")
        game_root = document.get("game_root")
        files = document.get("files")
        spoiler_file = document.get("spoiler_file")
        expected_names = {
            package_name + extension for extension in _PACKAGE_EXTENSIONS
        } if isinstance(package_name, str) and _MOD_NAME.fullmatch(package_name) else set()
        if (
            not isinstance(package_name, str)
            or not _MOD_NAME.fullmatch(package_name)
            or not isinstance(game_root, str)
            or not game_root
            or not isinstance(files, dict)
            or set(files) != expected_names
            or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in files.values())
            or (
                spoiler_file is not None
                and (
                    not isinstance(spoiler_file, dict)
                    or spoiler_file.get("filename") not in ({_SPOILER_FILENAME} | _LEGACY_SPOILER_FILENAMES)
                    or not isinstance(spoiler_file.get("sha256"), str)
                    or not _HASH.fullmatch(spoiler_file["sha256"])
                )
            )
        ):
            raise InstallationError("Randomizer ownership state is incomplete or malformed.")
        return document

    # Write state
    def _write_state(self, path: Path, document: dict[str, Any]) -> None:
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise


# Replace a staged output file
def _replace_staged_file(source: Path, target: Path) -> None:
    """Retry transient Windows file locks, then explain how to recover safely."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if attempt + 1 < _REPLACE_ATTEMPTS:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise InstallationError(
                f"Windows denied replacing {target.name} in {target.parent} after {_REPLACE_ATTEMPTS} attempts. "
                "Code Vein 2 or another process may still have the installed file open. Close the game and "
                "retry Randomize; if the game is already closed, check that the ~mods folder is writable. "
                "The app will preserve or restore its previously installed files; unrelated mods are untouched."
            ) from error


# Validate package
def _validate_package(package: PackageResult) -> tuple[str, dict[str, Path], dict[str, str]]:
    if not isinstance(package, PackageResult):
        raise InstallationError("Install requires a validated PackageResult.")
    if not isinstance(package.package_name, str) or not _MOD_NAME.fullmatch(package.package_name):
        raise InstallationError("Package name is not an owned randomizer package identifier.")
    expected = {package.package_name + extension for extension in _PACKAGE_EXTENSIONS}
    sources: dict[str, Path] = {}
    for raw_path in package.files:
        if not isinstance(raw_path, Path):
            raise InstallationError("Package files must be filesystem paths.")
        path = raw_path.expanduser().resolve(strict=True)
        if path.name not in expected or path.name in sources:
            raise InstallationError("Package file list must contain exactly one of each owned container.")
        if raw_path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise InstallationError(f"Package container is missing, empty, or a symlink: {raw_path.name}.")
        sources[path.name] = path
    if set(sources) != expected:
        raise InstallationError("Package file list is missing one or more owned containers.")
    hashes = {name: _sha256_file(path) for name, path in sources.items()}
    return package.package_name, sources, hashes


# Validate spoiler
def _validate_spoiler(path: Path | None) -> tuple[Path | None, str | None]:
    if path is None:
        return None, None
    if not isinstance(path, Path):
        raise InstallationError("Spoiler output must be a filesystem path.")
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise InstallationError(f"Spoiler output is missing or unreadable: {error}") from error
    if path.is_symlink() or not resolved.is_file() or resolved.stat().st_size <= 0:
        raise InstallationError("Spoiler output is missing, empty, or a symlink.")
    return resolved, _sha256_file(resolved)


# Check the selected game folder
def _validated_game_root(game_root: Path) -> tuple[Path, Path]:
    if not isinstance(game_root, Path):
        raise InstallationError("Game path must be a filesystem path.")
    check = check_installation(game_root)
    if not check.valid or check.pak_directory is None:
        raise InstallationError(f"Selected game installation is invalid: {check.reason}")
    root = check.game_root.resolve(strict=True)
    pak_directory = check.pak_directory.resolve(strict=True)
    if not _is_within(pak_directory, root):
        raise InstallationError("Resolved game's Paks directory escaped the selected game folder.")
    return root, pak_directory


# Resolve path key
def _path_key(path: Path) -> str:
    return str(path.expanduser().resolve()).casefold()


# Parse SHA-256 file
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
