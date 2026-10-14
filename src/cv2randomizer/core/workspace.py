"""Per-generation workspaces and content-addressed extraction cache."""

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir

from cv2randomizer import APP_NAME
from cv2randomizer.core.seed import NormalizedSeed


_GENERATION_WORKSPACE_SCHEMA = "cv2-randomizer-generation-workspace-v1"
_LEGACY_GENERATION_FOLDER = re.compile(
    r"\d{8}T\d{6}\.\d{6}Z-[A-Za-z0-9_-]{1,32}-[0-9a-f]{10}-[0-9a-f]{8}\Z"
)


@dataclass(frozen=True, slots=True)
class GenerationWorkspace:
    """Owned paths created for one randomizer generation."""

    root: Path
    extracted: Path
    staging: Path
    packaged: Path
    logs: Path

    # Build the workspace manifest
    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    # Resolve the spoiler log path
    @property
    def spoiler_log(self) -> Path:
        return self.root / "spoiler_log.csv"

    # Resolve assignment manifest path
    @property
    def assignment_manifest(self) -> Path:
        return self.root / "assignment_manifest.json"

    # Resolve spoiler text path
    @property
    def spoiler_text(self) -> Path:
        return self.packaged / "Spoiler.txt"

    # Resolve output ownership marker
    @property
    def ownership_marker(self) -> Path:
        return self.root / ".cv2randomizer-generation.json"


@dataclass(frozen=True, slots=True)
class GenerationCleanupReport:
    """Disk usage and retained files after a successful installed generation."""

    temporary_bytes_before: int
    temporary_bytes_after: int
    deleted_directories: tuple[Path, ...]
    retained_spoiler: Path | None
    warnings: tuple[str, ...] = ()
    retained_assignment_manifest: Path | None = None

    # Return reclaimed byte count
    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.temporary_bytes_before - self.temporary_bytes_after)


class WorkspaceManager:
    """Create isolated output directories under the user's app data folder."""

    # Initialize this object
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(user_data_dir(APP_NAME)) / "workspace"

    # Create a clean generation workspace
    def create_generation(
        self,
        seed: NormalizedSeed,
        *,
        timestamp: datetime | None = None,
    ) -> GenerationWorkspace:
        """Create an empty, unique workspace and its standard child folders."""
        instant = timestamp or datetime.now(UTC)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        instant = instant.astimezone(UTC)
        seed_digest = hashlib.sha256(seed.display_value.encode("utf-8")).hexdigest()[:10]
        seed_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", seed.display_value).strip("-_")[:32]
        if not seed_slug:
            seed_slug = "seed"
        folder_name = (
            f"{instant:%Y%m%dT%H%M%S.%fZ}-{seed_slug}-{seed_digest}-{uuid.uuid4().hex[:8]}"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_link_or_junction(self.root):
            raise ValueError("The Randomizer output root must not be a symlink or junction.")
        generation_root = self.root / "generated"
        if _is_link_or_junction(generation_root):
            raise ValueError("The Randomizer generation root must not be a symlink or junction.")
        workspace_root = generation_root / folder_name
        workspace_root.mkdir(parents=True, exist_ok=False)
        extracted = workspace_root / "extracted"
        staging = workspace_root / "staging"
        packaged = workspace_root / "packaged"
        logs = workspace_root / "logs"
        for directory in (extracted, staging, packaged, logs):
            directory.mkdir()
        workspace = GenerationWorkspace(workspace_root, extracted, staging, packaged, logs)
        workspace.ownership_marker.write_text(
            json.dumps({"schema": _GENERATION_WORKSPACE_SCHEMA, "id": folder_name}) + "\n",
            encoding="utf-8",
        )
        return workspace

    # Describe cleanup successful generation
    def cleanup_successful_generation(
        self,
        workspace: GenerationWorkspace,
        spoiler_source: Path,
        assignment_manifest_source: Path | None = None,
    ) -> GenerationCleanupReport:
        """Keep each run's logs and the assignment manifest; remove bulky output.

        Persistent extraction/cache data is stored outside ``root/generated`` and is
        never traversed by this method. The spoiler is installed directly into the
        game's ``~mods`` folder and is not retained under the app output root.
        Arbitrary siblings and links are left alone.
        """
        root = self.root.expanduser()
        if _is_link_or_junction(root):
            return GenerationCleanupReport(0, 0, (), None, ("Output root is a symlink or junction; cleanup skipped.",))
        try:
            resolved_root = root.resolve(strict=True)
            generation_root = root / "generated"
            if _is_link_or_junction(generation_root):
                return GenerationCleanupReport(0, 0, (), None, ("Generation root is a symlink or junction; cleanup skipped.",))
            resolved_generation_root = generation_root.resolve(strict=True)
            if resolved_generation_root.parent != resolved_root:
                return GenerationCleanupReport(0, 0, (), None, ("Generation root escaped the owned output directory; cleanup skipped.",))
            resolved_workspace = workspace.root.resolve(strict=True)
            if resolved_workspace.parent != resolved_generation_root or _is_link_or_junction(workspace.root):
                return GenerationCleanupReport(0, 0, (), None, ("Current workspace is outside the owned generation root; cleanup skipped.",))
            marker = workspace.ownership_marker
            if _is_link_or_junction(marker):
                return GenerationCleanupReport(0, 0, (), None, ("Current workspace ownership marker is unsafe; cleanup skipped.",))
            marker_document = json.loads(marker.read_text(encoding="utf-8"))
            if (
                not isinstance(marker_document, dict)
                or marker_document.get("schema") != _GENERATION_WORKSPACE_SCHEMA
                or marker_document.get("id") != workspace.root.name
            ):
                return GenerationCleanupReport(0, 0, (), None, ("Current workspace ownership could not be verified; cleanup skipped.",))
            source = spoiler_source.expanduser()
            if _is_link_or_junction(source) or not source.is_file():
                return GenerationCleanupReport(0, 0, (), None, ("Generated Spoiler.txt is missing or unsafe; cleanup skipped.",))
            resolved_source = source.resolve(strict=True)
            if not _is_descendant(resolved_source, resolved_workspace):
                return GenerationCleanupReport(0, 0, (), None, ("Spoiler.txt is outside the current workspace; cleanup skipped.",))
            resolved_assignment_source: Path | None = None
            if assignment_manifest_source is not None:
                assignment_source = assignment_manifest_source.expanduser()
                if _is_link_or_junction(assignment_source) or not assignment_source.is_file():
                    return GenerationCleanupReport(0, 0, (), None, ("Assignment manifest is missing or unsafe; cleanup skipped.",))
                resolved_assignment_source = assignment_source.resolve(strict=True)
                if not _is_descendant(resolved_assignment_source, resolved_workspace):
                    return GenerationCleanupReport(0, 0, (), None, ("Assignment manifest is outside the current workspace; cleanup skipped.",))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            return GenerationCleanupReport(0, 0, (), None, (f"Could not validate generation cleanup targets: {error}",))

        temporary_bytes_before = _tree_size(resolved_generation_root)
        spoiler_target = resolved_root / "Spoiler.txt"
        assignment_target = resolved_root / "assignment_manifest.json"
        if _is_link_or_junction(assignment_target) or (assignment_target.exists() and not assignment_target.is_file()):
            return GenerationCleanupReport(
                temporary_bytes_before, temporary_bytes_before, (), None,
                ("Persistent assignment-manifest destination is unsafe; generation files were retained.",),
            )
        warnings: list[str] = []
        if spoiler_target.exists() or _is_link_or_junction(spoiler_target):
            if _is_link_or_junction(spoiler_target):
                warnings.append("Left a symlink at output/Spoiler.txt untouched.")
            elif not spoiler_target.is_file():
                warnings.append("Left a non-file at output/Spoiler.txt untouched.")
            elif _is_randomizer_spoiler_copy(spoiler_target):
                try:
                    spoiler_target.unlink()
                except OSError as error:
                    warnings.append(f"Could not remove the prior output Spoiler.txt: {error}")
            else:
                warnings.append("Left an unrecognized output/Spoiler.txt untouched.")
        try:
            if resolved_assignment_source is not None:
                _copy_file_atomically(resolved_assignment_source, assignment_target)
        except OSError as error:
            return GenerationCleanupReport(
                temporary_bytes_before, temporary_bytes_before, (), None,
                (*warnings, f"Could not retain the assignment manifest; generation files were kept: {error}"),
            )

        deleted: list[Path] = []
        for child in resolved_generation_root.iterdir():
            if _is_link_or_junction(child) or not child.is_dir():
                continue
            if not self._is_owned_generation_directory(child, resolved_generation_root):
                continue
            try:
                if _contains_link_or_junction(child):
                    warnings.append(f"Skipped generation directory containing a link or junction: {child.name}")
                    continue
                if child.resolve(strict=True).parent != resolved_generation_root:
                    warnings.append(f"Skipped escaped generation directory: {child.name}")
                    continue
                logs_directory = child / "logs"
                if not logs_directory.is_dir() or _is_link_or_junction(logs_directory):
                    warnings.append(f"Skipped generation directory without a safe logs folder: {child.name}")
                    continue
                for entry in child.iterdir():
                    if entry == logs_directory or entry.name == ".cv2randomizer-generation.json":
                        continue
                    try:
                        if entry.is_dir():
                            shutil.rmtree(entry)
                            deleted.append(entry)
                        else:
                            entry.unlink()
                    except OSError as error:
                        warnings.append(f"Could not remove {entry.name} from {child.name}: {error}")
            except OSError as error:
                warnings.append(f"Could not remove {child.name}: {error}")

        return GenerationCleanupReport(
            temporary_bytes_before,
            _tree_size(resolved_generation_root),
            tuple(deleted),
            None,
            tuple(warnings),
            assignment_target if resolved_assignment_source is not None and assignment_target.is_file() else None,
        )


    # Check owned generation directory
    @staticmethod
    def _is_owned_generation_directory(path: Path, generation_root: Path) -> bool:
        if path.parent != generation_root or _is_link_or_junction(path):
            return False
        marker = path / ".cv2randomizer-generation.json"
        if marker.is_file() and not _is_link_or_junction(marker):
            try:
                document = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return False
            return (
                isinstance(document, dict)
                and document.get("schema") == _GENERATION_WORKSPACE_SCHEMA
                and document.get("id") == path.name
                and (path / "logs").is_dir()
                and not _is_link_or_junction(path / "logs")
            )
        expected_children = ("extracted", "staging", "packaged", "logs")
        if not all((path / name).is_dir() and not _is_link_or_junction(path / name) for name in expected_children):
            return False
        # Remove only workspaces from the previous app format that match its exact
        # generated name and expected directory structure.
        return bool(_LEGACY_GENERATION_FOLDER.fullmatch(path.name))


# Clear output directory
def clear_output_directory(
    output_root: Path,
    *,
    app_data_root: Path,
    protected_paths: tuple[Path, ...] = (),
) -> int:
    """Remove the contents of an app-owned output folder without following links."""
    root = output_root.expanduser()
    if _is_link_or_junction(root):
        raise ValueError("The output folder must not be a symlink or junction.")

    resolved_data = app_data_root.expanduser().resolve()
    resolved_root = root.resolve()
    if resolved_root == resolved_data or not _is_descendant(resolved_root, resolved_data):
        raise ValueError("The output folder must be inside app data and separate from its root.")
    for protected_path in protected_paths:
        resolved_protected = protected_path.expanduser().resolve()
        if resolved_protected == resolved_root or _is_descendant(resolved_protected, resolved_root):
            raise ValueError("The output folder contains protected app data and cannot be cleared.")

    if not root.exists():
        return 0
    if not root.is_dir():
        raise ValueError("The output path is not a folder.")

    removed_entries = 0
    for child in tuple(root.iterdir()):
        removed_entries += _remove_output_entry(child)
    return removed_entries


# Remove output entry
def _remove_output_entry(path: Path) -> int:
    """Delete one output entry, treating links and junctions as leaf entries."""
    if path.is_symlink():
        path.unlink()
        return 1
    if _is_junction(path):
        path.rmdir()
        return 1
    if path.is_dir():
        removed = sum(_remove_output_entry(child) for child in tuple(path.iterdir()))
        path.rmdir()
        return removed + 1
    path.unlink()
    return 1


class CacheManager:
    """Resolve extracted asset storage, optionally as one replaceable cache."""

    # Initialize this object
    def __init__(self, root: Path | None = None, *, single_entry: bool = False) -> None:
        self.root = root or Path(user_cache_dir(APP_NAME)) / "extracted"
        self.single_entry = single_entry

    # Build entry path
    def entry_path(
        self,
        *,
        game_version: str,
        source_hash: str,
        data_revision: str,
        create: bool = True,
    ) -> Path:
        """Return a content-addressed entry or the app's single reusable entry."""
        components = (game_version, source_hash.casefold(), data_revision)
        if any(not value for value in components):
            raise ValueError("Game version, source hash, and data revision are required.")
        if self.single_entry:
            path = self.root / "READY"
        else:
            encoded = json.dumps(components, ensure_ascii=False, separators=(",", ":"))
            key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            path = self.root / key
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path


# Check link or junction
def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or _is_junction(path)


# Check junction
def _is_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


# Check descendant
def _is_descendant(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


# Describe tree size
def _tree_size(root: Path) -> int:
    """Count regular file bytes without following links or junctions."""
    if not root.is_dir() or _is_link_or_junction(root):
        return 0
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories
            if not _is_link_or_junction(current_path / name)
        ]
        for name in files:
            path = current_path / name
            if _is_link_or_junction(path):
                continue
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


# Copy file atomically
def _copy_file_atomically(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size != source.stat().st_size:
            raise OSError(f"{target.name} copy size verification failed.")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


# Check randomizer spoiler copy
def _is_randomizer_spoiler_copy(path: Path) -> bool:
    """Recognize only prior app-generated spoiler files before removing them."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            header = handle.read(64 * 1024).split("\n\n", 1)[0]
    except (OSError, UnicodeError):
        return False
    lines = header.splitlines()
    return (
        bool(lines)
        and any(line.startswith("Seed: ") for line in lines)
        and any(line.startswith("Created: ") for line in lines)
        and any(re.fullmatch(r"App Version: v\d+(?:\.\d+)*", line) for line in lines)
    )


# Check link or junction
def _contains_link_or_junction(root: Path) -> bool:
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        entries = (*directories, *files)
        if any(_is_link_or_junction(current_path / name) for name in entries):
            return True
        directories[:] = [
            name for name in directories
            if not _is_link_or_junction(current_path / name)
        ]
    return False
