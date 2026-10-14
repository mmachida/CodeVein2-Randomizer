"""Resolve immutable resources shipped with or researched for the app."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import sys
from pathlib import Path

from cv2randomizer.game.usmap import UsmapError, read_usmap


class ApplicationResourceError(RuntimeError):
    """Raised when a required bundled application resource is unavailable."""


_LOGGER = logging.getLogger(__name__)
_OODLE_DLL_NAME = "oo2core_9_win64.dll"
_GAME_EXECUTABLE_NAME = "CodeVein2-Win64-Shipping.exe"


# Find the app or checkout root
def application_root() -> Path:
    """Return the application root without depending on the current directory."""
    if getattr(sys, "frozen", False):
        extraction_root = getattr(sys, "_MEIPASS", None)
        if extraction_root:
            return Path(extraction_root).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


# List application resource
def application_resource(*parts: str) -> Path:
    """Resolve a file distributed with the app (or a file in the source checkout)."""
    return application_root().joinpath(*parts).resolve()


# Resolve runtime catalogs and version metadata
def application_data_path(*parts: str) -> Path:
    """Resolve runtime catalogs and version metadata."""
    return application_resource("data", *parts)


# Find the project root
def project_root() -> Path:
    """Return the checkout root in development or the resource root in a frozen app."""
    return application_root()


# Resolve uassetapi helper
def resolve_uassetapi_helper() -> Path:
    """Find the self-contained helper in a release build or project in development."""
    if getattr(sys, "frozen", False):
        return application_resource("tools", "UAssetAPIWorldLoot.exe")
    return application_root() / "tools" / "uassetapi-worldloot" / "UAssetAPIWorldLoot.csproj"


# Resolve app icon
def resolve_app_icon() -> Path:
    """Return the application icon bundled with the project and release build."""
    icon = application_resource("ico", "icon.ico")
    if not icon.is_file():
        raise ApplicationResourceError(f"Application icon is missing: {icon}")
    return icon


# Resolve retoc executable
def resolve_retoc_executable() -> Path:
    """Find packaged Retoc or the development copy."""
    if getattr(sys, "frozen", False):
        return application_resource("tools", "retoc.exe")
    return application_root() / "workspace" / "tools" / "retoc-v0.1.5" / "retoc.exe"


# Describe uassetapi command
def uassetapi_command(
    helper: Path,
    dotnet_executable: str,
    *arguments: str,
    no_restore: bool = False,
) -> list[str]:
    """Construct the command for either the bundled helper or development project."""
    resolved = helper.expanduser().resolve()
    if resolved.suffix.casefold() == ".exe":
        return [str(resolved), *arguments]
    command = [dotnet_executable, "run", "--configuration", "Release"]
    if no_restore:
        command.append("--no-restore")
    command.extend(("--project", str(resolved), "--", *arguments))
    return command


# Resolve game executable
def _resolve_game_executable(game_root: Path, executable_path: Path | None) -> Path | None:
    """Use the detected executable, or inspect the game's known install layouts."""
    if executable_path is not None:
        detected = executable_path.expanduser().resolve()
        if detected.is_file():
            return detected
        _LOGGER.warning("Detected Code Vein 2 executable is unavailable: %s", detected)
        return detected

    candidates = (
        game_root / "CodeVein2" / "Binaries" / "Win64" / _GAME_EXECUTABLE_NAME,
        game_root / "Binaries" / "Win64" / _GAME_EXECUTABLE_NAME,
    )
    for candidate in candidates:
        exists = candidate.is_file()
        _LOGGER.info("Checking installed Code Vein 2 executable candidate: %s (file=%s)", candidate, exists)
        if exists:
            return candidate.resolve()
    return None


# Describe oodle candidates
def _oodle_candidates(game_root: Path, executable_path: Path | None) -> tuple[Path, ...]:
    """Build a small, deterministic list of known UE game Oodle locations."""
    candidates: list[Path] = []
    if executable_path is not None:
        executable = executable_path.expanduser().resolve()
        candidates.append(executable.parent / _OODLE_DLL_NAME)

    # Code Vein 2 ships its executable below the CodeVein2 directory. Other UE
    # packaged games may put Oodle beside the install-root executable instead.
    candidates.extend((
        game_root / "CodeVein2" / "Binaries" / "Win64" / _OODLE_DLL_NAME,
        game_root / "Engine" / "Binaries" / "ThirdParty" / "Oodle" / "Win64" / _OODLE_DLL_NAME,
        game_root / "Binaries" / "Win64" / _OODLE_DLL_NAME,
        game_root / "Engine" / "Binaries" / "Win64" / _OODLE_DLL_NAME,
        game_root / "CodeVein2" / "Engine" / "Binaries" / "ThirdParty" / "Oodle" / "Win64" / _OODLE_DLL_NAME,
    ))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return tuple(unique)


# Describe validated oodle file
def _validated_oodle_file(candidate: Path, allowed_root: Path) -> Path | None:
    """Canonicalize a candidate and ensure it is a real file under its owner root."""
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
        if (
            resolved.name.casefold() != _OODLE_DLL_NAME.casefold()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
        ):
            return None
        resolved.relative_to(allowed_root)
        return resolved
    except (OSError, ValueError):
        return None


# Retoc environment
def retoc_environment(
    game_root: Path | None,
    *,
    required: bool = False,
    executable_path: Path | None = None,
) -> dict[str, str] | None:
    """Expose installed or app-bundled Oodle to Retoc.

    The selected game installation remains the primary source. A local copy under
    the app's ``tools`` directory is the fallback used by packaged releases and
    development checkouts.
    """
    if game_root is None:
        return None
    resolved_root = game_root.expanduser().resolve()
    resolved_executable = _resolve_game_executable(resolved_root, executable_path)
    candidates = _oodle_candidates(resolved_root, resolved_executable)
    app_root = application_root().resolve()
    app_local_candidate = app_root / "tools" / _OODLE_DLL_NAME
    search_locations = tuple((candidate, resolved_root, "game install") for candidate in candidates) + (
        (app_local_candidate, app_root, "app-local tools fallback"),
    )
    selected: Path | None = None
    for candidate, allowed_root, source in search_locations:
        exists = candidate.is_file()
        _LOGGER.info("Checking Oodle candidate (%s): %s (file=%s)", source, candidate, exists)
        if not exists:
            continue
        selected = _validated_oodle_file(candidate, allowed_root)
        if selected is None:
            _LOGGER.warning("Rejecting invalid or non-canonical Oodle candidate (%s): %s", source, candidate)
            continue
        _LOGGER.info(
            "Using Oodle DLL for Retoc: %s (source=%s, game_root=%s, executable=%s)",
            selected,
            source,
            resolved_root,
            resolved_executable or "not supplied",
        )
        break

    if selected is None:
        if required:
            checked_paths = "\n  - ".join(
                f"{path} ({source})" for path, _owner, source in search_locations
            )
            raise ApplicationResourceError(
                f"Could not find {_OODLE_DLL_NAME}, required by Retoc to read Oodle-compressed game archives.\n"
                f"Resolved game root: {resolved_root}\n"
                f"Detected executable: {resolved_executable or 'not supplied'}\n"
                f"Checked candidate locations:\n  - {checked_paths}\n"
                "The app bundle or selected game installation must contain this DLL."
            )
        return None
    environment = os.environ.copy()
    current_path = environment.get("PATH", "")
    environment["PATH"] = str(selected.parent) + (os.pathsep + current_path if current_path else "")
    return environment

# Resolve cv2 USMAP
def resolve_cv2_usmap(*, root: Path | None = None) -> Path:
    """Resolve the single canonical CV2 mapping and verify it is readable."""
    base = (root or application_root()).expanduser().resolve()
    candidates = (
        base / "workspace" / "research_assets" / "CV2.usmap",
        base / "_internal" / "workspace" / "research_assets" / "CV2.usmap",
    )
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            read_usmap(candidate)
            return candidate.resolve(strict=True)
        except (OSError, UsmapError, ValueError):
            continue
    raise ApplicationResourceError(
        "The bundled CV2.usmap mapping is missing or unreadable. "
        "Restore workspace/research_assets/CV2.usmap and rebuild the application."
    )


# Describe cv2 USMAP SHA-256
def cv2_usmap_sha256(path: Path | None = None) -> str:
    """Return the digest of the canonical mapping resource."""
    resolved = path or resolve_cv2_usmap()
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ApplicationResourceError(f"Cannot read bundled CV2.usmap at {resolved}: {error}") from error
    return digest.hexdigest()
