"""Version metadata loading and fail-closed game build detection."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.locator import check_installation

_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_VERSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_DEFAULT_EXECUTABLE_PATH = Path("CodeVein2/Binaries/Win64/CodeVein2-Win64-Shipping.exe")
_LOCAL_CATALOG_REVISION = "current"
_WORLD_LOOT_COMPATIBILITY_PROFILES = {"1.0.9.0": "1.0.9"}

# These MD5 values provide a friendly UI label only. They do not grant official
# support or change the local-unverified cache/generation path.
KNOWN_EXECUTABLE_MD5_DISPLAY_VERSIONS = {
    "AED1E2455C9978BC986DC8DD2FB8FDF6": "1.0.2.0",
    "F55ABDC919FCC87208674C5E78F33E02": "1.0.3.0",
    "A68382E1A308443C9B807FB3E803DF93": "1.0.5.0",
    "24F801822281A01AE7634C8D21128B1E": "1.0.6.0",
    "C01534CEE9E893CA1237FB24E95A55F4": "1.0.7.0",
    "A3C395779465AC4009796ED0964942D7": "1.0.9.0",
    "E9BC16005E960B49EDF82C88853F4C75": "2.0.1.0",
    "2E8999C26D26F1B7B9FFCDA0986F44CB": "2.0.2.0",
    "C904F7B7F771AE6C14AD5288EDACBFD5": "2.0.3.0",
}


class VersionConfigurationError(ValueError):
    """Raised when supported game version metadata is invalid."""


@dataclass(frozen=True, slots=True)
class VersionDefinition:
    """Signatures and required assets for one supported game build."""

    version_id: str
    executable_path: Path
    executable_sha256: tuple[str, ...]
    catalog_revision: str
    required_containers: tuple[Path, ...] = ()
    required_asset_paths: tuple[str, ...] = ()

    # Load settings from a dictionary
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VersionDefinition":
        """Parse one version definition from its JSON representation."""
        version_id = data.get("version_id")
        executable = data.get("executable")
        catalog_revision = data.get("catalog_revision")
        if not isinstance(version_id, str) or not _VERSION_ID_PATTERN.fullmatch(version_id):
            raise ValueError("version_id must be a simple, non-empty identifier.")
        if not isinstance(executable, dict):
            raise ValueError("executable must be an object.")
        executable_path = _relative_path(executable.get("path"), "executable.path")
        hashes = executable.get("sha256")
        if not isinstance(hashes, list) or not hashes:
            raise ValueError("executable.sha256 must contain at least one hash.")
        if any(not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value) for value in hashes):
            raise ValueError("Each executable SHA-256 must contain exactly 64 hex digits.")
        if not isinstance(catalog_revision, str) or not catalog_revision.strip():
            raise ValueError("catalog_revision must be a non-empty string.")

        containers = data.get("required_containers", [])
        if not isinstance(containers, list):
            raise ValueError("required_containers must be an array.")
        required_containers = tuple(
            _relative_path(path, "required_containers entry") for path in containers
        )
        assets = data.get("required_asset_paths", [])
        if not isinstance(assets, list) or not assets or any(
            not isinstance(path, str) or not path.strip() for path in assets
        ):
            raise ValueError("required_asset_paths must name at least one mandatory core source package.")
        normalized_assets = tuple(path.strip() for path in assets)
        if (
            any(
                not path.startswith("/Game/") or "\\" in path or "." in path.rsplit("/", 1)[-1]
                or any(part in {"", ".", ".."} for part in path.split("/")[1:])
                for path in normalized_assets
            )
            or len({path.casefold() for path in normalized_assets}) != len(normalized_assets)
        ):
            raise ValueError("required_asset_paths must contain unique /Game package paths.")

        return cls(
            version_id=version_id,
            executable_path=executable_path,
            executable_sha256=tuple(value.casefold() for value in hashes),
            catalog_revision=catalog_revision,
            required_containers=required_containers,
            required_asset_paths=normalized_assets,
        )


# Resolve relative path
def _relative_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a relative path.")
    normalized = value.replace("\\", "/")
    pure_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if (
        pure_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(part in {"..", "."} for part in pure_path.parts)
    ):
        raise ValueError(f"{label} must not be absolute or traverse parent folders.")
    return Path(*pure_path.parts)


# Load version definitions
def load_version_definitions(directory: Path | None = None) -> tuple[VersionDefinition, ...]:
    """Load definitions from ``data/versions`` in stable order."""
    if directory is None:
        directory = application_data_path("versions")
    if not directory.exists():
        return ()

    definitions: list[VersionDefinition] = []
    seen_ids: set[str] = set()
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name.casefold()):
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("version definition root must be an object.")
            definition = VersionDefinition.from_dict(raw)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise VersionConfigurationError(f"Invalid version definition {path.name}: {error}") from error
        if definition.version_id in seen_ids:
            raise VersionConfigurationError(
                f"Duplicate game version identifier: {definition.version_id}."
            )
        seen_ids.add(definition.version_id)
        definitions.append(definition)
    return tuple(sorted(definitions, key=lambda definition: definition.version_id))


@dataclass(frozen=True, slots=True)
class VersionDetection:
    """Officially supported build or an explicitly unverified local-validation build."""

    supported: bool
    version_id: str | None
    catalog_revision: str | None
    executable_path: Path | None
    reason: str
    required_asset_paths: tuple[str, ...] = ()
    local_unverified: bool = False
    display_version: str | None = None

    # Read usable for local validation
    @property
    def usable_for_local_validation(self) -> bool:
        """Whether extraction/local generation may use this detection result."""
        return self.supported or self.local_unverified

    # Select build-specific loot rules
    @property
    def world_loot_compatibility_profile(self) -> str | None:
        """Return a narrowly scoped profile for a recognized executable build."""
        candidates = (self.version_id, self.display_version)
        for candidate in candidates:
            if isinstance(candidate, str):
                normalized = candidate.strip()
                if normalized in _WORLD_LOOT_COMPATIBILITY_PROFILES:
                    return _WORLD_LOOT_COMPATIBILITY_PROFILES[normalized]
                if normalized and not normalized.startswith("local-unverified-"):
                    normalized_four_part = normalized if normalized.count(".") == 3 else normalized + ".0"
                    if normalized_four_part in _WORLD_LOOT_COMPATIBILITY_PROFILES:
                        return _WORLD_LOOT_COMPATIBILITY_PROFILES[normalized_four_part]
        return None


class GameVersionDetector:
    """Match a tested signature, optionally allowing a clearly local-only build."""

    # Initialize this object
    def __init__(
        self,
        definitions_dir: Path | None = None,
        *,
        allow_local_unverified: bool = False,
    ) -> None:
        self._definitions_dir = definitions_dir
        self._allow_local_unverified = allow_local_unverified

    # Detect the installed game version
    def detect(self, game_root: Path) -> VersionDetection:
        """Return a supported version only when a configured signature matches."""
        installation = check_installation(game_root)
        if not installation.valid:
            return VersionDetection(False, None, None, None, installation.reason)

        try:
            definitions = load_version_definitions(self._definitions_dir)
        except VersionConfigurationError as error:
            return VersionDetection(False, None, None, None, str(error))

        found_executable = False
        executable_hashes: dict[Path, tuple[str, str]] = {}

        # Return archive hashes for this build
        def hashes_for(executable: Path) -> tuple[str, str]:
            resolved = executable.resolve()
            if resolved not in executable_hashes:
                executable_hashes[resolved] = _executable_hashes(resolved)
            return executable_hashes[resolved]

        for definition in definitions:
            executable = installation.game_root / definition.executable_path
            if not executable.is_file():
                continue
            found_executable = True
            if any(
                not (installation.game_root / container).is_file()
                for container in definition.required_containers
            ):
                continue
            try:
                actual_sha256, _actual_md5 = hashes_for(executable)
            except OSError as error:
                return VersionDetection(
                    False, None, None, executable,
                    f"Could not read game executable: {error}",
                )
            if actual_sha256 in definition.executable_sha256:
                return VersionDetection(
                    supported=True,
                    version_id=definition.version_id,
                    catalog_revision=definition.catalog_revision,
                    executable_path=executable,
                    reason=f"Supported game version: {definition.version_id}.",
                    required_asset_paths=definition.required_asset_paths,
                    display_version=definition.version_id,
                )

        if self._allow_local_unverified:
            executable_paths = tuple(dict.fromkeys(
                [definition.executable_path for definition in definitions]
                + [_DEFAULT_EXECUTABLE_PATH]
            ))
            for relative_path in executable_paths:
                executable = installation.game_root / relative_path
                if not executable.is_file():
                    continue
                try:
                    actual_sha256, actual_md5 = hashes_for(executable)
                except OSError as error:
                    return VersionDetection(
                        False, None, None, executable,
                        f"Could not read game executable: {error}",
                    )
                return VersionDetection(
                    supported=False,
                    version_id=f"local-unverified-{actual_sha256}",
                    catalog_revision=_LOCAL_CATALOG_REVISION,
                    executable_path=executable,
                    display_version=KNOWN_EXECUTABLE_MD5_DISPLAY_VERSIONS.get(actual_md5.upper()),
                    reason=(
                        "Local validation only: this executable has no matching tested game-version signature. "
                        "Extraction and generation use a cache bound to this executable and archive set; "
                        "this build is not officially supported."
                    ),
                    local_unverified=True,
                )

        reason = (
            "Game executable found, but its build is not supported."
            if found_executable else "No configured game executable was found in the selected folder."
        )
        if not definitions:
            reason = "No tested game-version signatures are configured."
        return VersionDetection(False, None, None, None, reason)


# Read executable hashes
def _executable_hashes(path: Path) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            sha256.update(chunk)
            md5.update(chunk)
    return sha256.hexdigest(), md5.hexdigest()
