"""DEV-only extraction of the requested Booster ScenarioClass packages.

This utility deliberately stays outside the normal Generate flow. It delegates
all game archive access, filtering, validation, and caching to
``IoStoreAssetExtractor`` and only materializes the exact requested packages.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from cv2randomizer.core.config import SettingsStore  # noqa: E402
from cv2randomizer.game.asset_extraction import (  # noqa: E402
    AssetExtractionError,
    ExtractedAsset,
    IoStoreAssetExtractor,
    MaterializedAsset,
    materialize_extracted_assets,
)
from cv2randomizer.game.locator import check_installation  # noqa: E402
from cv2randomizer.game.versioning import GameVersionDetector  # noqa: E402


EXPECTED_PACKAGE_COUNT = 31
DATA_REVISION_PREFIX = "booster-scenario-research-v1"
LOCAL_EXECUTABLE = Path("CodeVein2/Binaries/Win64/CodeVein2-Win64-Shipping.exe")


@dataclass(frozen=True, slots=True)
class PackageResult:
    virtual_path: str
    outputs: tuple[Path, ...] = ()
    error: str | None = None

    # Read succeeded
    @property
    def succeeded(self) -> bool:
        return self.error is None and bool(self.outputs)


# Read package paths
def read_package_paths(path: Path, *, expected_count: int = EXPECTED_PACKAGE_COUNT) -> tuple[str, ...]:
    """Read a strict list of unique /Game package paths with the requested count."""
    if type(expected_count) is not int or expected_count <= 0:
        raise ValueError("Expected package count must be a positive integer.")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        raise ValueError(f"Could not read package list {path}: {error}") from error
    packages = tuple(line.strip() for line in lines if line.strip())
    if len(packages) != expected_count:
        raise ValueError(
            f"Expected exactly {expected_count} package paths in {path}, got {len(packages)}."
        )
    if len({package.casefold() for package in packages}) != expected_count:
        raise ValueError(f"Package list {path} contains duplicate paths.")
    for package in packages:
        parts = package.split("/")
        if (
            not package.startswith("/Game/")
            or "\\" in package
            or len(parts) < 3
            or any(part in {"", ".", ".."} for part in parts[1:])
            or Path(package).suffix
        ):
            raise ValueError(f"Invalid Unreal package path in {path}: {package!r}.")
    return packages


# Read research version ID
def _research_version_id(game_root: Path) -> tuple[str, str]:
    """Use official version detection when available, otherwise label a local hash."""
    detection = GameVersionDetector().detect(game_root)
    if detection.supported and detection.version_id:
        return detection.version_id, detection.reason

    executable = game_root.expanduser().resolve() / LOCAL_EXECUTABLE
    if not executable.is_file():
        raise ValueError(
            f"The game version is not officially recognized ({detection.reason}) and the local "
            f"research executable was not found at {executable}."
        )
    digest = hashlib.sha256()
    with executable.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"local-unverified-{digest.hexdigest()[:16]}", detection.reason


# Extract one
def _extract_one(
    extractor: IoStoreAssetExtractor,
    game_root: Path,
    virtual_path: str,
    *,
    game_version: str,
    data_revision: str,
) -> ExtractedAsset:
    extracted = extractor.extract_assets(
        game_root,
        (virtual_path,),
        game_version=game_version,
        data_revision=data_revision,
    )
    if len(extracted) != 1 or extracted[0].virtual_path.casefold() != virtual_path.casefold():
        raise AssetExtractionError(f"Extractor did not return the exact requested package {virtual_path}.")
    return extracted[0]


# Extract packages
def extract_packages(
    package_paths: tuple[str, ...],
    *,
    game_root: Path,
    content_root: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor | None = None,
) -> tuple[PackageResult, ...]:
    """Batch-extract exact package paths, falling back per package on batch errors."""
    service = extractor or IoStoreAssetExtractor()
    assets_by_path: dict[str, ExtractedAsset] = {}
    extraction_errors: dict[str, str] = {}
    batch_error: str | None = None
    try:
        extracted = service.extract_assets(
            game_root,
            package_paths,
            game_version=game_version,
            data_revision=data_revision,
        )
        assets_by_path = {asset.virtual_path.casefold(): asset for asset in extracted}
        if set(assets_by_path) != {package.casefold() for package in package_paths}:
            raise AssetExtractionError("Batch extraction did not return the exact requested package set.")
    except (AssetExtractionError, OSError) as error:
        # The extractor commits successful batches atomically. Retry one-by-one
        # only when a whole batch fails so we can report exact missing packages.
        batch_error = str(error)
        assets_by_path.clear()

    if batch_error is not None:
        for virtual_path in package_paths:
            try:
                assets_by_path[virtual_path.casefold()] = _extract_one(
                    service,
                    game_root,
                    virtual_path,
                    game_version=game_version,
                    data_revision=data_revision,
                )
            except (AssetExtractionError, OSError, ValueError) as error:
                # Keep extracting the remaining exact requests and report each
                # failure under its own virtual package path.
                assets_by_path.pop(virtual_path.casefold(), None)
                extraction_errors[virtual_path.casefold()] = str(error)

    results: list[PackageResult] = []
    for virtual_path in package_paths:
        asset = assets_by_path.get(virtual_path.casefold())
        if asset is None:
            detail = extraction_errors.get(virtual_path.casefold())
            if detail is None:
                detail = batch_error or "The extractor did not return this package."
            results.append(PackageResult(virtual_path, error=detail))
            continue
        try:
            materialized = materialize_extracted_assets((asset,), content_root)
            if len(materialized) != 1:
                raise AssetExtractionError("Materializer did not return exactly one requested package.")
            results.append(PackageResult(virtual_path, materialized[0].files))
        except (AssetExtractionError, OSError) as error:
            results.append(PackageResult(virtual_path, error=str(error)))
    return tuple(results)


# Build parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages-file", type=Path, required=True, help="Text file containing the requested /Game package paths.")
    parser.add_argument("--output-content-root", type=Path, required=True, help="Destination corresponding to the Unreal CodeVein2/Content directory.")
    parser.add_argument("--game-root", type=Path, help="Game installation root; defaults to the path saved in app settings.")
    parser.add_argument(
        "--expected-count",
        type=int,
        default=EXPECTED_PACKAGE_COUNT,
        help=f"Expected package count (default: {EXPECTED_PACKAGE_COUNT}).",
    )
    return parser


# Start the application
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        package_paths = read_package_paths(
            args.packages_file.expanduser().resolve(strict=True),
            expected_count=args.expected_count,
        )
        configured_path = SettingsStore().load().game_path
        game_root = args.game_root or (Path(configured_path) if configured_path else None)
        if game_root is None:
            raise ValueError("No game root was provided and the application has no saved game path.")
        game_root = game_root.expanduser().resolve(strict=True)
        installation = check_installation(game_root)
        if not installation.valid:
            raise ValueError(installation.reason)
        game_version, version_note = _research_version_id(game_root)
        data_revision = f"{DATA_REVISION_PREFIX}-{hashlib.sha256(args.packages_file.read_bytes()).hexdigest()[:16]}"
        content_root = args.output_content_root.expanduser().resolve()
        if content_root == game_root or content_root.is_relative_to(game_root):
            raise ValueError("Research output must be outside the installed game directory.")
    except (OSError, ValueError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 2

    print(f"Game root: {game_root}")
    print(f"Game version namespace: {game_version}")
    if game_version.startswith("local-unverified-"):
        print(f"Version status: local research only ({version_note}); official support unchanged.")
    print(f"Requested package count: {len(package_paths)}")
    print(f"Output Content root: {content_root}")
    results = extract_packages(
        package_paths,
        game_root=game_root,
        content_root=content_root,
        game_version=game_version,
        data_revision=data_revision,
    )

    successful = tuple(result for result in results if result.succeeded)
    failed = tuple(result for result in results if not result.succeeded)
    print(f"Successful package count: {len(successful)}")
    print(f"Failed package count: {len(failed)}")
    print("Output paths:")
    for result in successful:
        for output in result.outputs:
            print(f"  {result.virtual_path} -> {output}")
    if failed:
        print("Failures:")
        for result in failed:
            print(f"  {result.virtual_path}: {result.error}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
