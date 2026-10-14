"""Filtered, cached extraction of selected IoStore assets through retoc_cli."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import uuid
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.core.resources import (
    ApplicationResourceError,
    resolve_retoc_executable,
    retoc_environment,
)
from cv2randomizer.core.workspace import CacheManager
from cv2randomizer.core.memory_budget import MemoryBudgetScheduler
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.dependency_requests import canonical_package_key
from cv2randomizer.game.locator import check_installation

RETOC_VERSION = "retoc_cli 0.1.5"
_ASSET_EXTENSIONS = frozenset({".uasset", ".uexp", ".umap", ".ubulk", ".uptnl"})
_PRIMARY_EXTENSIONS = frozenset({".uasset", ".umap"})
_RETOC_PRIMARY_EXTENSIONS = (".uasset", ".umap")
_MAX_RETOC_FILTER_ARGUMENT_CHARACTERS = 28000
_PACKAGE_INVENTORY_INDEX_REVISION = 1
_PACKAGE_INVENTORY_INDEX_NAME = "installed-package-index.json.gz"
_HASH_CHUNK_BYTES = 1024 * 1024


class AssetExtractionError(RuntimeError):
    """Raised when a requested source asset cannot be extracted safely."""


@dataclass(frozen=True, slots=True)
class ExtractedAsset:
    """A requested virtual asset and the local package files extracted for it."""

    virtual_path: str
    files: tuple[Path, ...]
    file_hashes: tuple[str, ...] = ()

    # Resolve primary file
    @property
    def primary_file(self) -> Path:
        """Return its .uasset or .umap package header."""
        for path in self.files:
            if path.suffix.casefold() in _PRIMARY_EXTENSIONS:
                return path
        raise AssetExtractionError(f"Extracted asset {self.virtual_path} has no package header.")


@dataclass(frozen=True, slots=True)
class MaterializedAsset:
    """An extracted package copied into an isolated Unreal Content tree."""

    virtual_path: str
    files: tuple[Path, ...]

    # Resolve primary file
    @property
    def primary_file(self) -> Path:
        for path in self.files:
            if path.suffix.casefold() in _PRIMARY_EXTENSIONS:
                return path
        raise AssetExtractionError(f"Materialized asset {self.virtual_path} has no package header.")


# Build archive source fingerprint
def archive_source_fingerprint(pak_directory: Path) -> str:
    """Fingerprint archive metadata plus .utoc contents without hashing multi-GB .ucas files.

    IoStore data files can be tens of gigabytes. Their stable relative path, size,
    and nanosecond modification time are included, while the much smaller .utoc
    indices are content-hashed. The supported game-version signature is an
    additional cache namespace component.
    """
    root = pak_directory.expanduser().resolve()
    if not root.is_dir():
        raise AssetExtractionError(f"Game archive directory does not exist: {root}.")
    archives = tuple(
        sorted(
            (
                path for path in root.iterdir()
                if path.is_file() and path.suffix.casefold() in {".pak", ".utoc", ".ucas"}
            ),
            key=lambda path: path.name.casefold(),
        )
    )
    if not archives:
        raise AssetExtractionError(f"No game archives were found in {root}.")
    if not any(path.suffix.casefold() == ".utoc" for path in archives):
        raise AssetExtractionError(f"No IoStore .utoc index was found in {root}.")

    digest = hashlib.sha256()
    for archive in archives:
        try:
            stat = archive.stat()
            relative = archive.relative_to(root).as_posix().casefold().encode("utf-8")
            digest.update(relative + b"\0")
            digest.update(stat.st_size.to_bytes(8, "big"))
            digest.update(stat.st_mtime_ns.to_bytes(8, "big", signed=False))
            if archive.suffix.casefold() == ".utoc":
                with archive.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                        digest.update(chunk)
        except OSError as error:
            raise AssetExtractionError(f"Could not fingerprint game archive {archive.name}: {error}") from error
    return digest.hexdigest()


# Normalize asset path
def _normalize_asset_path(value: str) -> tuple[str, PurePosixPath]:
    if not isinstance(value, str) or "\\" in value:
        raise AssetExtractionError("Asset paths must be Unreal virtual paths such as /Game/Map/Package.")
    normalized = value.strip()
    if normalized[:6].casefold() == "/game/":
        normalized = "/Game/" + normalized[6:]
    path = PurePosixPath(normalized)
    if (
        not normalized.startswith("/Game/")
        or path.is_absolute() is False
        or path.suffix
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        or len(path.parts) < 3
    ):
        raise AssetExtractionError(f"Invalid Unreal package path {value!r}; expected /Game/... without an extension.")
    return "/" + path.as_posix().lstrip("/"), PurePosixPath(*path.parts[2:])


# Parse retoc package paths
def _parse_retoc_package_paths(output: str) -> frozenset[str]:
    """Normalize retoc's path listing to exact Unreal package paths."""
    packages: set[str] = set()
    for line in output.splitlines():
        value = line.strip().strip("\"'").rstrip(",;")
        game_index = value.find("/Game/")
        if game_index >= 0:
            value = value[game_index:]
        else:
            content_marker = "CodeVein2/Content/"
            content_index = value.find(content_marker)
            if content_index < 0:
                content_marker = "CodeVein2\\Content\\"
                content_index = value.casefold().find(content_marker.casefold())
            if content_index < 0:
                continue
            value = "/Game/" + value[content_index + len(content_marker):].replace("\\", "/")
        value = value.split()[0].strip("\"'\"),;")
        suffix = Path(value).suffix.casefold()
        if suffix in _ASSET_EXTENSIONS:
            value = value[:-len(suffix)]
        if value.startswith("/Game/") and not any(part in {"", ".", ".."} for part in value.split("/")[1:]):
            packages.add(value)
    return frozenset(packages)


# Parse SHA-256 file
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


# Check merge nested cache directory
def _can_merge_nested_cache_directory(destination: Path, cache_entry: Path) -> bool:
    """Allow a package directory that currently exists only as a parent namespace.

    The cache mirrors Unreal package paths. A map package can share its directory
    with generated child maps, so a child extracted first leaves a valid
    directory at the parent's future package path. Merge only when that directory
    contains real child directories and no direct files or invalid manifest.
    """
    if (
        not destination.exists()
        or destination.is_symlink()
        or not destination.is_dir()
        or not _is_within(destination, cache_entry)
    ):
        return False
    try:
        return all(child.is_dir() and not child.is_symlink() for child in destination.iterdir())
    except OSError:
        return False


# Merge staged cache asset
def _merge_staged_cache_asset(staged_asset: Path, destination: Path, added_paths: list[Path]) -> None:
    """Merge staged package files into a namespace directory without copying bytes."""
    marker_name = ".cv2randomizer-asset.json"
    files = sorted(
        (path for path in staged_asset.iterdir() if path.is_file() and path.name != marker_name),
        key=lambda path: path.name.casefold(),
    )
    marker = staged_asset / marker_name
    if not marker.is_file():
        raise AssetExtractionError("Staged cache package is missing its integrity manifest.")
    files.append(marker)
    for source in files:
        target = destination / source.name
        if target.exists() or target.is_symlink():
            raise AssetExtractionError(f"Refusing to overwrite an existing cache file: {target}.")
        temporary = destination / f".cv2-cache-{uuid.uuid4().hex}.tmp"
        added_paths.extend((temporary, target))
        # Staged and destination files live under the same cache root, so a
        # rename is atomic on the supported filesystem and avoids another full
        # read/write pass over each extracted package component.
        source.replace(temporary)
        temporary.replace(target)


# Materialize extracted assets
def materialize_extracted_assets(
    assets: Iterable[ExtractedAsset],
    content_root: Path,
    *,
    verify_hashes: bool = True,
    profile_callback: Callable[..., None] | None = None,
    profile_kind: str = "asset_materialization",
) -> tuple[MaterializedAsset, ...]:
    """Copy validated packages into a Content tree without replacing conflicts.

    Callers reading from a READY immutable cache can disable duplicate SHA-256
    passes; cache identity and entry validation already ran before Generate.
    Byte-copy completeness is still checked by file sizes.
    """
    root = content_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    results: list[MaterializedAsset] = []
    seen: set[str] = set()
    started = time.perf_counter()
    copy_seconds = hash_seconds = 0.0
    copied_bytes = copied_files = 0
    for asset in assets:
        _, relative = _normalize_asset_path(asset.virtual_path)
        key = asset.virtual_path.casefold()
        if key in seen:
            raise AssetExtractionError(f"Duplicate extracted package {asset.virtual_path}.")
        seen.add(key)
        package_dir = root.joinpath(*relative.parts[:-1])
        if not _is_within(package_dir, root) or package_dir.is_symlink():
            raise AssetExtractionError("Resolved materialized package path escaped its Content root.")
        package_dir.mkdir(parents=True, exist_ok=True)
        expected_stem = relative.name.casefold()
        materialized: list[Path] = []
        if not asset.files:
            raise AssetExtractionError(f"Extracted package {asset.virtual_path} has no files.")
        for file_index, source in enumerate(asset.files):
            expected_hash = (
                asset.file_hashes[file_index]
                if file_index < len(asset.file_hashes) and asset.file_hashes[file_index]
                else None
            )
            suffix = source.suffix.casefold()
            if suffix not in _ASSET_EXTENSIONS or source.stem.casefold() != expected_stem or not source.is_file():
                raise AssetExtractionError(f"Invalid cached package member for {asset.virtual_path}: {source.name}.")
            target = package_dir / (relative.name + suffix)
            if not _is_within(target, root) or target.is_symlink():
                raise AssetExtractionError("Resolved materialized file escaped its Content root.")
            if target.exists():
                hash_started = time.perf_counter()
                if not target.is_file() or target.stat().st_size != source.stat().st_size or (
                    verify_hashes and (
                        _sha256_file(target) != expected_hash
                        if expected_hash is not None
                        else _sha256_file(target) != _sha256_file(source)
                    )
                ):
                    raise AssetExtractionError(f"Refusing to overwrite conflicting staged package file {target.name}.")
                if verify_hashes:
                    hash_seconds += time.perf_counter() - hash_started
            else:
                temporary = package_dir / (target.name + ".cv2-tmp")
                try:
                    copy_started = time.perf_counter()
                    shutil.copyfile(source, temporary)
                    copy_seconds += time.perf_counter() - copy_started
                    copied_bytes += temporary.stat().st_size
                    copied_files += 1
                    hash_started = time.perf_counter()
                    if temporary.stat().st_size != source.stat().st_size or (
                        verify_hashes and (
                            _sha256_file(temporary) != expected_hash
                            if expected_hash is not None
                            else _sha256_file(temporary) != _sha256_file(source)
                        )
                    ):
                        raise AssetExtractionError(f"Staged package copy failed integrity validation: {target.name}.")
                    if verify_hashes:
                        hash_seconds += time.perf_counter() - hash_started
                    temporary.replace(target)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            materialized.append(target)
        if not any(path.suffix.casefold() in _PRIMARY_EXTENSIONS for path in materialized):
            raise AssetExtractionError(f"Materialized package {asset.virtual_path} has no .uasset/.umap header.")
        results.append(MaterializedAsset(asset.virtual_path, tuple(sorted(materialized, key=lambda p: p.suffix.casefold()))))
    if profile_callback is not None:
        profile_callback(
            "package_materialization",
            kind=profile_kind,
            packages=len(results),
            files=copied_files,
            bytes_copied=copied_bytes,
            copy_seconds=copy_seconds,
            hash_validation_seconds=hash_seconds,
            elapsed_seconds=time.perf_counter() - started,
        )
    return tuple(results)



# Resolve asset batches
def _asset_batches(
    requests: list[tuple[str, PurePosixPath]],
    *,
    maximum_argument_characters: int = _MAX_RETOC_FILTER_ARGUMENT_CHARACTERS,
) -> tuple[tuple[tuple[str, PurePosixPath], ...], ...]:
    batches: list[tuple[tuple[str, PurePosixPath], ...]] = []
    current: list[tuple[str, PurePosixPath]] = []
    current_size = 0
    for request in requests:
        argument_size = sum(
            len(_retoc_filter_value(request[1], extension)) + len("--filter") + 8
            for extension in _RETOC_PRIMARY_EXTENSIONS
        )
        if current and current_size + argument_size > maximum_argument_characters:
            batches.append(tuple(current))
            current = []
            current_size = 0
        current.append(request)
        current_size += argument_size
    if current:
        batches.append(tuple(current))
    return tuple(batches)


# Format a Retoc path filter
def _retoc_filter_value(relative: PurePosixPath, extension: str) -> str:
    """Return a precise Retoc filter for one package and primary file type.

    Retoc's filter is substring-based. A basename such as ``MainWorld`` matches
    every asset below the MainWorld folder; include the full cooked content path
    and package extension to keep large World Partition maps isolated.
    """
    content_path = PurePosixPath("CodeVein2", "Content", *relative.parts)
    return f"{content_path.as_posix()}{extension}"


class IoStoreAssetExtractor:
    """Request selected cooked assets from retoc_cli and cache validated outputs."""

    # Initialize this object
    def __init__(
        self,
        *,
        executable: Path | None = None,
        cache: CacheManager | None = None,
        timeout_seconds: float = 600,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
        memory_scheduler: MemoryBudgetScheduler | None = None,
        diagnostics_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.executable = executable or resolve_retoc_executable()
        self.cache = cache or CacheManager()
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self._memory_scheduler = memory_scheduler
        self._diagnostics_callback = diagnostics_callback
        self._version_verified = False
        self._cache_source_hash_override: str | None = None
        self._observed_package_inventory: PackageInventory | None = None
        self._observed_inventory_fingerprint: str | None = None
        self._resolved_package_spellings: dict[str, str] = {}
        self._verified_cache_assets: dict[tuple[str, str], tuple[tuple[str, int, int], ...]] = {}
        self._assets_seen: dict[tuple[str, str], ExtractedAsset] = {}
        self._profiling_callback: Callable[..., None] | None = None

    # Reuse extracted data from cache
    def with_cache(
        self,
        cache: CacheManager,
        *,
        package_inventory: PackageInventory | None = None,
    ) -> "IoStoreAssetExtractor":
        """Create an isolated-cache extractor, optionally reusing a caller's validated inventory."""
        clone = IoStoreAssetExtractor(
            executable=self.executable,
            cache=cache,
            timeout_seconds=self.timeout_seconds,
            runner=self.runner,
        )
        clone.set_extraction_telemetry(
            self._memory_scheduler,
            self._diagnostics_callback,
            self._profiling_callback,
        )
        clone._observed_package_inventory = (
            package_inventory if package_inventory is not None else self._observed_package_inventory
        )
        clone._observed_inventory_fingerprint = (
            self._observed_inventory_fingerprint if package_inventory is None else None
        )
        clone._resolved_package_spellings = dict(self._resolved_package_spellings)
        return clone

    # Set extraction telemetry
    def set_extraction_telemetry(
        self,
        scheduler: MemoryBudgetScheduler | None,
        diagnostics_callback: Callable[[str, str], None] | None,
        profiling_callback: Callable[..., None] | None = None,
    ) -> None:
        self._memory_scheduler = scheduler
        self._diagnostics_callback = diagnostics_callback
        self._profiling_callback = profiling_callback

    # Record profile
    def _record_profile(self, operation: str, **fields: Any) -> None:
        if self._profiling_callback is not None:
            self._profiling_callback(operation, **fields)

    # Set cache source hash
    def set_cache_source_hash(self, source_hash: str) -> None:
        """Bind extracted files to a caller's stronger cache identity when required."""
        if not isinstance(source_hash, str) or not source_hash.strip():
            raise ValueError("Cache source hash must be a non-empty string.")
        self._cache_source_hash_override = source_hash.casefold()

    # Build cached asset inventory
    def cached_asset_inventory(self, cache_entry: Path | None = None) -> tuple[ExtractedAsset, ...]:
        """Return assets this extractor has verified or materialized in one cache entry."""
        root_key = str(cache_entry.resolve()).casefold() if cache_entry is not None else None
        return tuple(
            asset for (asset_root, _package_key), asset in sorted(self._assets_seen.items())
            if root_key is None or asset_root == root_key
        )

    # Prime cached asset inventory
    def prime_cached_asset_inventory(
        self,
        cache_entry: Path,
        package_paths: Iterable[str],
    ) -> int:
        """Seed the per-entry cache index from validated, immutable raw assets.

        This is used when rebuilding prepared data from a READY raw cache. The
        game archive identity was checked before reuse and cache inspection has
        validated every package manifest and required source. Retoc then avoids
        rehashing those same bytes merely to rediscover the same cache entries.
        """
        seen: set[str] = set()
        seeded = 0
        for package in package_paths:
            normalized, relative = _normalize_asset_path(package)
            if normalized.casefold() in seen:
                continue
            seen.add(normalized.casefold())
            cached = self._read_cached_asset(
                cache_entry, normalized, relative, verify_hash=False,
            )
            if cached is None:
                continue
            cache_key = (str(cache_entry.resolve()).casefold(), normalized.casefold())
            self._verified_cache_assets[cache_key] = self._cache_asset_signature(cached.files)
            self._remember_cached_asset(cache_entry, normalized, cached)
            seeded += 1
        return seeded

    # Cache this asset in the index
    def _remember_cached_asset(
        self,
        cache_entry: Path,
        normalized: str,
        asset: ExtractedAsset,
    ) -> None:
        root_key = str(cache_entry.resolve()).casefold()
        package_key = normalized.casefold()
        self._assets_seen[(root_key, package_key)] = asset

    # Inspect package inventory
    def inspect_package_inventory(
        self,
        game_root: Path,
        *,
        source_fingerprint: str | None = None,
    ) -> PackageInventory:
        """Load or list installed IoStore package names without modifying assets."""
        installation = check_installation(game_root)
        if not installation.valid or installation.pak_directory is None:
            raise AssetExtractionError(installation.reason)
        if not self.executable.is_file():
            raise AssetExtractionError(
                f"retoc_cli {RETOC_VERSION.rsplit(' ', 1)[-1]} was not found at {self.executable}."
            )
        self._verify_cli_version()
        process_environment = retoc_environment(game_root)
        containers = tuple(sorted(
            (path for path in installation.pak_directory.iterdir()
             if path.is_file() and path.suffix.casefold() == ".utoc"),
            key=lambda path: path.name.casefold(),
        ))
        if not containers:
            raise AssetExtractionError("No IoStore .utoc containers were found.")

        inventory_started = time.perf_counter()
        archive_fingerprint = source_fingerprint or archive_source_fingerprint(installation.pak_directory)
        current_containers = tuple(container.name for container in containers)
        if (
            self._observed_package_inventory is not None
            and self._observed_inventory_fingerprint == archive_fingerprint
            and self._observed_package_inventory.container_names
            == frozenset(name.casefold() for name in current_containers)
        ):
            self._record_profile(
                "package_inventory_index",
                cache_hit=True,
                source="memory",
                package_count=len(self._observed_package_inventory.package_paths),
                containers=len(current_containers),
                retoc_list_launches=0,
                elapsed_seconds=time.perf_counter() - inventory_started,
            )
            return self._observed_package_inventory

        persisted = self._load_package_inventory_index(
            archive_fingerprint,
            current_containers,
        )
        if persisted is not None:
            self._observed_package_inventory = persisted
            self._observed_inventory_fingerprint = archive_fingerprint
            self._record_profile(
                "package_inventory_index",
                cache_hit=True,
                source="persistent",
                package_count=len(persisted.package_paths),
                containers=len(current_containers),
                retoc_list_launches=0,
                elapsed_seconds=time.perf_counter() - inventory_started,
            )
            return persisted

        package_paths: set[str] = set()
        package_containers: list[tuple[str, str]] = []
        packages_by_container: dict[str, tuple[str, ...]] = {}
        for container in containers:
            result = self._run(
                [str(self.executable), "list", "--path", str(container)],
                memory_job=f"Retoc inventory {container.name}",
                estimated_memory_bytes=512 * 1024**2,
                environment=process_environment,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-4000:]
                raise AssetExtractionError(
                    f"retoc_cli could not inspect package inventory in {container.name} "
                    f"(exit {result.returncode}): {detail}"
                )
            listed = _parse_retoc_package_paths(result.stdout)
            package_paths.update(listed)
            package_containers.extend((package_path, container.name) for package_path in listed)
            packages_by_container[container.name] = tuple(sorted(listed, key=str.casefold))

        inventory = PackageInventory.create(
            package_paths,
            (container.name for container in containers),
            package_containers,
        )
        self._observed_package_inventory = inventory
        self._observed_inventory_fingerprint = archive_fingerprint
        self._write_package_inventory_index(archive_fingerprint, packages_by_container)
        self._record_profile(
            "package_inventory_index",
            cache_hit=False,
            source="retoc",
            package_count=len(inventory.package_paths),
            containers=len(current_containers),
            retoc_list_launches=len(containers),
            elapsed_seconds=time.perf_counter() - inventory_started,
        )
        return inventory

    # Build package inventory index path
    def _package_inventory_index_path(self) -> Path:
        return self.cache.root.expanduser() / "package-index" / _PACKAGE_INVENTORY_INDEX_NAME

    # Load package inventory index
    def _load_package_inventory_index(
        self,
        archive_fingerprint: str,
        expected_containers: tuple[str, ...],
    ) -> PackageInventory | None:
        path = self._package_inventory_index_path()
        try:
            document = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except (OSError, EOFError, UnicodeError, json.JSONDecodeError, gzip.BadGzipFile, zlib.error):
            return None
        if (
            not isinstance(document, dict)
            or document.get("schema_revision") != _PACKAGE_INVENTORY_INDEX_REVISION
            or document.get("retoc_version") != RETOC_VERSION
            or document.get("archive_fingerprint") != archive_fingerprint
        ):
            return None
        packages_by_container = document.get("packages_by_container")
        if not isinstance(packages_by_container, dict) or set(packages_by_container) != set(expected_containers):
            return None
        package_paths: list[str] = []
        package_containers: list[tuple[str, str]] = []
        for container, packages in packages_by_container.items():
            if not isinstance(packages, list):
                return None
            for package in packages:
                if not isinstance(package, str):
                    return None
                try:
                    canonical_package_key(package)
                except (TypeError, ValueError):
                    return None
                package_paths.append(package)
                package_containers.append((package, container))
        if not package_paths:
            return None
        return PackageInventory.create(package_paths, expected_containers, package_containers)

    # Write package inventory index
    def _write_package_inventory_index(
        self,
        archive_fingerprint: str,
        packages_by_container: dict[str, tuple[str, ...]],
    ) -> None:
        path = self._package_inventory_index_path()
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        document = {
            "schema_revision": _PACKAGE_INVENTORY_INDEX_REVISION,
            "retoc_version": RETOC_VERSION,
            "archive_fingerprint": archive_fingerprint,
            "packages_by_container": {
                container: list(packages)
                for container, packages in sorted(packages_by_container.items(), key=lambda item: item[0].casefold())
            },
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            compressed = gzip.compress(
                json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                mtime=0,
            )
            temporary.write_bytes(compressed)
            temporary.replace(path)
        except OSError:
            # The index only avoids a future Retoc listing pass; its inability
            # to persist must never make a successful inventory unusable.
            temporary.unlink(missing_ok=True)

    # Extract requested game assets
    def extract_assets(
        self,
        game_root: Path,
        virtual_paths: Iterable[str],
        *,
        optional_virtual_paths: Iterable[str] = (),
        _resolved_package_paths: dict[str, str] | None = None,
        game_version: str,
        data_revision: str,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> tuple[ExtractedAsset, ...]:
        """Extract only requested /Game package paths from a validated installation."""
        extraction_started = time.perf_counter()
        if not game_version.strip() or not data_revision.strip():
            raise AssetExtractionError("Game version and catalog/data revision are required for extraction.")
        requested: list[tuple[str, PurePosixPath]] = []
        seen: set[str] = set()
        for value in virtual_paths:
            normalized, relative = _normalize_asset_path(value)
            key = normalized.casefold()
            if key not in seen:
                seen.add(key)
                requested.append((normalized, relative))
        if not requested:
            raise AssetExtractionError("At least one asset virtual path must be requested.")
        optional_keys: set[str] = set()
        for value in optional_virtual_paths:
            normalized, _relative = _normalize_asset_path(value)
            optional_keys.add(normalized.casefold())
        if not optional_keys.issubset(seen):
            raise AssetExtractionError("Optional asset paths must also appear in the requested package list.")

        inventory = self._observed_package_inventory
        if inventory is not None:
            resolved_requests: dict[str, tuple[str, PurePosixPath]] = {}
            required_resolved: set[str] = set()
            for virtual_path, _relative in requested:
                request_key = virtual_path.casefold()
                resolved = (
                    _resolved_package_paths.get(request_key)
                    if _resolved_package_paths is not None else None
                )
                if resolved is None:
                    resolved = self._resolved_package_spellings.get(request_key)
                if resolved is not None:
                    resolved, variants = resolved, ()
                    if resolved.casefold() not in inventory.package_paths:
                        if _resolved_package_paths is not None:
                            raise AssetExtractionError(
                                f"Pre-resolved package path {resolved} is absent from the active inventory."
                            )
                        resolved = None
                else:
                    resolved, variants = inventory.resolve_package_path_spelling(virtual_path)
                if resolved is None:
                    if variants:
                        detail = ", ".join(variants)
                        raise AssetExtractionError(
                            f"Package path {virtual_path} is ambiguous in the installed game's IoStore inventory: {detail}."
                        )
                    if virtual_path.casefold() in optional_keys:
                        continue
                    raise AssetExtractionError(
                        f"Package path {virtual_path} is absent from the installed game's IoStore inventory."
                    )
                # Preserve the package casing reported by Retoc. Its --filter
                # matching is case-sensitive; PackageInventory still handles
                # logical lookups case-insensitively.
                actual_path, actual_relative = _normalize_asset_path(resolved)
                key = actual_path.casefold()
                resolved_requests.setdefault(key, (actual_path, actual_relative))
                self._resolved_package_spellings[request_key] = actual_path
                self._resolved_package_spellings[key] = actual_path
                if virtual_path.casefold() not in optional_keys:
                    required_resolved.add(key)
            requested = list(resolved_requests.values())
            optional_keys = set(resolved_requests) - required_resolved
            if not requested:
                return ()

        installation = check_installation(game_root)
        if not installation.valid or installation.pak_directory is None:
            raise AssetExtractionError(installation.reason)
        if not self.executable.is_file():
            raise AssetExtractionError(
                f"retoc_cli {RETOC_VERSION.rsplit(' ', 1)[-1]} was not found at {self.executable}. "
                "Place the verified Windows helper there or pass its path to IoStoreAssetExtractor."
            )
        self._verify_cli_version()
        try:
            process_environment = retoc_environment(
                game_root,
                required=self.runner is run_hidden,
            )
        except ApplicationResourceError as error:
            raise AssetExtractionError(str(error)) from error
        source_hash = self._cache_source_hash_override or archive_source_fingerprint(installation.pak_directory)
        cache_entry = self.cache.entry_path(
            game_version=game_version,
            source_hash=source_hash,
            data_revision=data_revision,
        )
        if not cache_entry.is_dir():
            raise AssetExtractionError("The extraction cache directory could not be created.")

        cached_by_path: dict[str, ExtractedAsset] = {}
        missing: list[tuple[str, PurePosixPath]] = []
        cache_scan_started = time.perf_counter()
        progress_interval = max(1, len(requested) // 20)
        for index, (normalized, relative) in enumerate(requested, start=1):
            if progress and (index == 1 or index % progress_interval == 0 or index == len(requested)):
                progress("checking-asset-cache", index, len(requested))
            cache_key = (str(cache_entry.resolve()).casefold(), normalized.casefold())
            known_signature = self._verified_cache_assets.get(cache_key)
            verify_hash = known_signature is None or not self._cache_signature_matches(
                cache_entry, relative, known_signature,
            )
            cached = self._read_cached_asset(
                cache_entry, normalized, relative, verify_hash=verify_hash,
            )
            if cached is None:
                missing.append((normalized, relative))
            else:
                self._verified_cache_assets[cache_key] = self._cache_asset_signature(cached.files)
                cached_by_path[normalized.casefold()] = cached
                self._remember_cached_asset(cache_entry, normalized, cached)
        self._record_profile(
            "asset_cache_scan",
            requested_packages=len(requested),
            cache_hits=len(cached_by_path),
            missing_packages=len(missing),
            elapsed_seconds=time.perf_counter() - cache_scan_started,
        )
        batches = _asset_batches(missing)
        packages_completed = len(cached_by_path)
        for batch_index, batch in enumerate(batches, start=1):
            if progress:
                progress("extracting-assets", packages_completed, len(requested))
            batch_assets = self._extract_batch(
                pak_directory=installation.pak_directory,
                cache_entry=cache_entry,
                requests=batch,
                optional_keys=optional_keys,
                batch_index=batch_index,
                batch_count=len(batches),
                process_environment=process_environment,
            )
            for asset in batch_assets:
                package_key = asset.virtual_path.casefold()
                normalized, relative = _normalize_asset_path(asset.virtual_path)
                cache_key = (str(cache_entry.resolve()).casefold(), package_key)
                self._verified_cache_assets[cache_key] = self._cache_asset_signature(asset.files)
                self._remember_cached_asset(cache_entry, normalized, asset)
                cached_by_path[package_key] = asset
            packages_completed += len(batch)
            if progress:
                progress("extracting-assets", min(packages_completed, len(requested)), len(requested))
        for normalized, _relative in missing:
            # _extract_batch hashes each file after it has reached its final
            # staged cache location and returns that manifest-backed record.
            # Re-opening and re-hashing the same bytes here duplicated I/O.
            if normalized.casefold() not in cached_by_path and normalized.casefold() not in optional_keys:
                raise AssetExtractionError(f"Extracted package failed cache integrity checks: {normalized}.")
        if progress:
            progress("extracting-assets", len(requested), len(requested))
        self._record_profile(
            "asset_extraction_call",
            requested_packages=len(requested),
            extracted_packages=len(missing),
            retoc_batches=len(batches),
            elapsed_seconds=time.perf_counter() - extraction_started,
        )
        return tuple(
            cached_by_path[normalized.casefold()]
            for normalized, _ in requested
            if normalized.casefold() in cached_by_path
        )

    # Run the external helper command
    def _run(
        self,
        arguments: list[str],
        *,
        memory_job: str | None = None,
        estimated_memory_bytes: int = 0,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        started = time.perf_counter()
        if memory_job and self._diagnostics_callback is not None:
            self._diagnostics_callback("retoc", memory_job)
        try:
            if memory_job and self._memory_scheduler is not None:
                with self._memory_scheduler.job(memory_job, estimated_bytes=estimated_memory_bytes):
                    result = self._run_unbudgeted(arguments, environment=environment)
            else:
                result = self._run_unbudgeted(arguments, environment=environment)
            return result
        finally:
            self._record_profile(
                "retoc_invocation",
                command=arguments[1] if len(arguments) > 1 else "unknown",
                label=memory_job,
                elapsed_seconds=time.perf_counter() - started,
            )

    # Run unbudgeted
    def _run_unbudgeted(
        self, arguments: list[str], *, environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            kwargs: dict[str, Any] = {
                "check": False,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": self.timeout_seconds,
            }
            if environment is not None:
                kwargs["env"] = environment
            return self.runner(arguments, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AssetExtractionError(f"Could not run retoc_cli: {error}.") from error

    # Verify cli version
    def _verify_cli_version(self) -> None:
        if self._version_verified:
            return
        result = self._run([str(self.executable), "--version"])
        version = result.stdout.strip()
        if result.returncode != 0 or version != RETOC_VERSION:
            raise AssetExtractionError(
                f"Expected {RETOC_VERSION}, but helper reported {version or result.stderr.strip()!r}."
            )
        self._version_verified = True

    # Read cached asset
    def _read_cached_asset(
        self,
        cache_entry: Path,
        virtual_path: str,
        relative: PurePosixPath,
        *,
        verify_hash: bool = True,
    ) -> ExtractedAsset | None:
        destination = cache_entry / "assets" / Path(*relative.parts)
        if not _is_within(destination, cache_entry) or destination.is_symlink():
            raise AssetExtractionError("Resolved extraction cache path escaped its owned cache directory.")
        manifest_path = destination / ".cv2randomizer-asset.json"
        try:
            metadata: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
            cached_virtual_path = metadata.get("virtual_path")
            if (
                not isinstance(cached_virtual_path, str)
                or cached_virtual_path.casefold() != virtual_path.casefold()
                or not isinstance(metadata.get("files"), list)
            ):
                return None
            files: list[Path] = []
            hashes: list[str] = []
            for entry in metadata["files"]:
                name = entry.get("name")
                expected_hash = entry.get("sha256")
                if not isinstance(name, str) or Path(name).name != name:
                    return None
                path = destination / name
                expected_size = entry.get("size")
                if (
                    path.suffix.casefold() not in _ASSET_EXTENSIONS
                    or not path.is_file()
                    or not isinstance(expected_hash, str)
                    or type(expected_size) is not int
                    or path.stat().st_size != expected_size
                    or (verify_hash and _sha256_file(path) != expected_hash)
                ):
                    return None
                files.append(path)
                hashes.append(expected_hash)
            if not files or not any(path.suffix.casefold() in _PRIMARY_EXTENSIONS for path in files):
                return None
            ordered = sorted(zip(files, hashes, strict=True), key=lambda row: row[0].suffix.casefold())
            return ExtractedAsset(
                virtual_path,
                tuple(path for path, _digest in ordered),
                tuple(digest for _path, digest in ordered),
            )
        except (AttributeError, OSError, json.JSONDecodeError, TypeError):
            return None

    # Cache asset signature
    @staticmethod
    def _cache_asset_signature(files: tuple[Path, ...]) -> tuple[tuple[str, int, int], ...]:
        return tuple(
            (path.name.casefold(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in files
        )

    # Cache signature matches
    @classmethod
    def _cache_signature_matches(
        cls,
        cache_entry: Path,
        relative: PurePosixPath,
        signature: tuple[tuple[str, int, int], ...],
    ) -> bool:
        destination = cache_entry / "assets" / Path(*relative.parts)
        try:
            files = tuple(
                sorted(
                    (path for path in destination.iterdir() if path.is_file() and path.suffix.casefold() in _ASSET_EXTENSIONS),
                    key=lambda path: path.suffix.casefold(),
                )
            )
            return cls._cache_asset_signature(files) == signature
        except OSError:
            return False

    # Extract batch
    def _extract_batch(
        self,
        *,
        pak_directory: Path,
        cache_entry: Path,
        requests: tuple[tuple[str, PurePosixPath], ...],
        optional_keys: set[str],
        batch_index: int,
        batch_count: int,
        process_environment: dict[str, str] | None = None,
    ) -> tuple[ExtractedAsset, ...]:
        """Extract and validate a filtered batch before committing any cache entries."""
        if not requests:
            return ()
        with tempfile.TemporaryDirectory(prefix=".retoc-batch-", dir=cache_entry) as temporary:
            temporary_root = Path(temporary)
            output = temporary_root / "output"
            output.mkdir()
            command = [str(self.executable), "to-legacy"]
            for _, relative in requests:
                for extension in _RETOC_PRIMARY_EXTENSIONS:
                    command.extend(("--filter", _retoc_filter_value(relative, extension)))
            command.extend((
                "--no-shaders", "--no-script-objects", str(pak_directory), str(output),
            ))
            package_names = ", ".join(
                dict.fromkeys((requests[0][0], requests[-1][0]))
            )
            result = self._run(
                command,
                memory_job=f"Retoc extract batch {batch_index}/{batch_count} ({len(requests)} packages; {package_names})",
                estimated_memory_bytes=1536 * 1024**2,
                environment=process_environment,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-4000:]
                names = ", ".join(value for value, _ in requests)
                raise AssetExtractionError(
                    f"retoc_cli failed to extract requested package batch ({names}) (exit {result.returncode}): {detail}"
                )

            requested_by_stem = {
                PurePosixPath("CodeVein2", "Content", *relative.parts).as_posix().casefold(): (virtual_path, relative)
                for virtual_path, relative in requests
            }
            produced: dict[str, list[Path]] = {}
            for path in output.rglob("*"):
                if not path.is_file() or path.suffix.casefold() not in _ASSET_EXTENSIONS:
                    continue
                key = path.with_suffix("").relative_to(output).as_posix().casefold()
                produced.setdefault(key, []).append(path)
            missing = requested_by_stem.keys() - produced.keys()
            missing_required = {
                package_key for package_key in missing
                if requested_by_stem[package_key][0].casefold() not in optional_keys
            }
            if missing_required:
                missing_names = sorted(missing_required)
                raise AssetExtractionError(
                    f"retoc_cli omitted requested packages from the filtered batch: {missing_names}."
                )
            # retoc_cli filters by substring and can emit similarly named sibling
            # Blueprints. Those files stay in this temporary directory; only the
            # exact requested package stems below are copied into the owned cache.

            staged: list[tuple[Path, Path]] = []
            staged_metadata: list[tuple[str, Path, tuple[tuple[str, str], ...]]] = []
            move_seconds = 0.0
            hash_seconds = 0.0
            bytes_moved = 0
            bytes_hashed = 0
            file_count = 0
            for package_key, (virtual_path, relative) in requested_by_stem.items():
                if package_key in missing:
                    continue
                source_files = sorted(produced[package_key], key=lambda path: path.suffix.casefold())
                if not any(path.suffix.casefold() in _PRIMARY_EXTENSIONS for path in source_files):
                    raise AssetExtractionError(f"retoc_cli omitted the package header for {virtual_path}.")
                staged_dir = temporary_root / "cache_assets" / str(len(staged))
                staged_dir.mkdir(parents=True)
                manifest_files: list[dict[str, str]] = []
                for source in source_files:
                    target = staged_dir / source.name
                    if target.exists():
                        raise AssetExtractionError(
                            f"retoc_cli produced duplicate package components for {virtual_path}: {source.name}."
                        )
                    move_started = time.perf_counter()
                    source.replace(target)
                    move_seconds += time.perf_counter() - move_started
                    size = target.stat().st_size
                    bytes_moved += size
                    file_count += 1
                    hash_started = time.perf_counter()
                    digest = _sha256_file(target)
                    hash_seconds += time.perf_counter() - hash_started
                    bytes_hashed += size
                    manifest_files.append({
                        "name": target.name,
                        "size": size,
                        "sha256": digest,
                    })
                (staged_dir / ".cv2randomizer-asset.json").write_text(
                    json.dumps({"virtual_path": virtual_path, "files": manifest_files}, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                destination = cache_entry / "assets" / Path(*relative.parts)
                if not _is_within(destination, cache_entry) or destination.is_symlink():
                    raise AssetExtractionError("Resolved extraction cache path escaped its owned cache directory.")
                if destination.exists() and not _can_merge_nested_cache_directory(destination, cache_entry):
                    raise AssetExtractionError(
                        f"Refusing to replace an existing invalid extraction cache entry for {virtual_path}; clear that cache entry first."
                    )
                staged.append((staged_dir, destination))
                staged_metadata.append((virtual_path, destination, tuple(
                    (row["name"], row["sha256"]) for row in manifest_files
                )))

            committed: list[tuple[Path, bool, list[Path]]] = []
            try:
                for staged_dir, destination in staged:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.is_symlink() or not _is_within(destination, cache_entry):
                        raise AssetExtractionError("Extraction cache destination changed during batch commit.")
                    if destination.exists():
                        if not _can_merge_nested_cache_directory(destination, cache_entry):
                            raise AssetExtractionError("Extraction cache destination changed during batch commit.")
                        added_paths: list[Path] = []
                        committed.append((destination, False, added_paths))
                        _merge_staged_cache_asset(staged_dir, destination, added_paths)
                    else:
                        staged_dir.replace(destination)
                        committed.append((destination, True, []))
            except Exception:
                for destination, created_directory, added_paths in reversed(committed):
                    if created_directory and destination.is_dir() and _is_within(destination, cache_entry):
                        shutil.rmtree(destination)
                    else:
                        for path in reversed(added_paths):
                            if path.is_file() and _is_within(path, cache_entry):
                                path.unlink()
                raise
            self._record_profile(
                "retoc_output_commit",
                batch_index=batch_index,
                batch_count=batch_count,
                requested_packages=len(requests),
                committed_packages=len(staged),
                files=file_count,
                bytes_copied=0,
                bytes_moved=bytes_moved,
                bytes_hashed=bytes_hashed,
                move_seconds=move_seconds,
                hash_seconds=hash_seconds,
            )
            committed_assets: list[ExtractedAsset] = []
            for virtual_path, destination, hashed_files in staged_metadata:
                final_files = tuple(destination / name for name, _digest in hashed_files)
                final_hashes = tuple(digest for _name, digest in hashed_files)
                ordered = sorted(zip(final_files, final_hashes, strict=True), key=lambda pair: pair[0].suffix.casefold())
                asset = ExtractedAsset(
                    virtual_path,
                    tuple(path for path, _digest in ordered),
                    tuple(digest for _path, digest in ordered),
                )
                committed_assets.append(asset)
            return tuple(committed_assets)

    # Extract one
    def _extract_one(
        self,
        *,
        pak_directory: Path,
        cache_entry: Path,
        virtual_path: str,
        relative: PurePosixPath,
    ) -> ExtractedAsset:
        destination = cache_entry / "assets" / Path(*relative.parts)
        if not _is_within(destination, cache_entry) or destination.is_symlink():
            raise AssetExtractionError("Resolved extraction cache path escaped its owned cache directory.")

        with tempfile.TemporaryDirectory(prefix=".retoc-", dir=cache_entry) as temporary:
            temporary_root = Path(temporary)
            output = temporary_root / "output"
            output.mkdir()
            command = [
                str(self.executable),
                "to-legacy",
            ]
            for extension in _RETOC_PRIMARY_EXTENSIONS:
                command.extend(("--filter", _retoc_filter_value(relative, extension)))
            command.extend((
                "--no-shaders",
                "--no-script-objects",
                str(pak_directory),
                str(output),
            ))
            result = self._run(command)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-4000:]
                raise AssetExtractionError(
                    f"retoc_cli failed to extract {virtual_path} (exit {result.returncode}): {detail}"
                )

            package_stem = PurePosixPath("CodeVein2", "Content", *relative.parts).as_posix().casefold()
            extracted_files = tuple(
                sorted(
                    (
                        path for path in output.rglob("*")
                        if path.is_file()
                        and path.suffix.casefold() in _ASSET_EXTENSIONS
                        and path.with_suffix("").relative_to(output).as_posix().casefold() == package_stem
                    ),
                    key=lambda path: path.suffix.casefold(),
                )
            )
            if not extracted_files or not any(
                path.suffix.casefold() in _PRIMARY_EXTENSIONS for path in extracted_files
            ):
                raise AssetExtractionError(
                    f"retoc_cli completed but did not produce the requested package {virtual_path}."
                )

            staged_asset = temporary_root / "cache_asset"
            staged_asset.mkdir()
            file_manifest: list[dict[str, str]] = []
            for source in extracted_files:
                target = staged_asset / source.name
                shutil.copyfile(source, target)
                file_manifest.append({
                    "name": target.name,
                    "size": target.stat().st_size,
                    "sha256": _sha256_file(target),
                })
            (staged_asset / ".cv2randomizer-asset.json").write_text(
                json.dumps(
                    {"virtual_path": virtual_path, "files": file_manifest},
                    indent=2,
                    sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )

            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.is_symlink() or not _is_within(destination, cache_entry):
                    raise AssetExtractionError("Refusing to replace an unsafe extraction cache path.")
                cached = self._read_cached_asset(cache_entry, virtual_path, relative)
                if cached is not None:
                    return cached
                if not _can_merge_nested_cache_directory(destination, cache_entry):
                    raise AssetExtractionError(
                        f"Refusing to replace an existing invalid extraction cache entry for {virtual_path}; clear that cache entry first."
                    )
                added_paths: list[Path] = []
                try:
                    _merge_staged_cache_asset(staged_asset, destination, added_paths)
                except Exception:
                    for path in reversed(added_paths):
                        if path.is_file() and _is_within(path, cache_entry):
                            path.unlink()
                    raise
            else:
                staged_asset.replace(destination)

        cached = self._read_cached_asset(cache_entry, virtual_path, relative)
        if cached is None:
            raise AssetExtractionError(f"Cached output failed integrity checks for {virtual_path}.")
        return cached
