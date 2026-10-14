"""Validation of a manually selected Code Vein 2 installation."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InstallationCheck:
    """Result of checking the expected game folder structure."""

    valid: bool
    game_root: Path
    pak_directory: Path | None
    reason: str


# Check installation
def check_installation(game_root: Path) -> InstallationCheck:
    """Check for Code Vein 2's expected content and archive directories."""
    root = game_root.expanduser().resolve()
    if not root.is_dir():
        return InstallationCheck(False, root, None, "The selected game folder does not exist.")

    pak_directory = root / "CodeVein2" / "Content" / "Paks"
    if not pak_directory.is_dir():
        return InstallationCheck(
            False,
            root,
            None,
            "The selected folder does not contain CodeVein2/Content/Paks.",
        )

    archive_suffixes = {".pak", ".utoc", ".ucas"}
    try:
        has_archive = any(
            child.is_file() and child.suffix.casefold() in archive_suffixes
            for child in pak_directory.iterdir()
        )
    except OSError as error:
        return InstallationCheck(
            False,
            root,
            pak_directory,
            f"Could not read the game's Paks folder: {error}",
        )
    if not has_archive:
        return InstallationCheck(
            False,
            root,
            pak_directory,
            "The game's Paks folder contains no .pak, .utoc, or .ucas archives.",
        )

    return InstallationCheck(True, root, pak_directory, "Game folder structure is valid.")
