"""Patch dialogue skip through Interact progress."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command
from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    IoStoreAssetExtractor,
    materialize_extracted_assets,
)
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION


SUBTITLE_WIDGET_PACKAGE = "/Game/UI/Subtitles/WDT_Subtitles"


class DialogueSkipError(RuntimeError):
    """Raised when the optional patch cannot be validated."""


@dataclass(frozen=True, slots=True)
class DialogueSkipResult:
    output_files: tuple[Path, ...]
    applied: bool
    reason: str | None = None


class DialogueSkipSerializer:
    """Patch the subtitle widget's hold callback."""

    # Initialize the serializer
    def __init__(
        self,
        *,
        helper_project: Path | None = None,
        dotnet_executable: str = "dotnet",
        engine_version: str = SUPPORTED_ENGINE_VERSION,
        timeout_seconds: float = 1800,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
    ) -> None:
        self.helper_project = (helper_project or resolve_uassetapi_helper()).resolve()
        self.dotnet_executable = dotnet_executable
        self.engine_version = engine_version
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        if engine_version != SUPPORTED_ENGINE_VERSION:
            raise ValueError(f"Dialogue skip serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Patch the subtitle widget
    def patch_subtitle_widget(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        input_file: Path,
        output_file: Path,
    ) -> DialogueSkipResult:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        source = input_file.expanduser().resolve(strict=True)
        output = output_file.expanduser().resolve()
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise DialogueSkipError("Dialogue skip helper, workspace, or CV2.usmap is missing.")
        if (
            not source.is_file() or not _is_within(source, root) or not _is_within(output, root)
            or source == output or source.suffix.casefold() != ".uasset"
            or output.suffix.casefold() != ".uasset"
            or source.stem != "WDT_Subtitles" or output.stem != source.stem
        ):
            raise DialogueSkipError("Subtitle widget paths do not match the audited source identity.")
        if any(path.exists() for path in _package_sidecars(output)):
            raise DialogueSkipError("Refusing to overwrite an existing subtitle widget package.")

        command = uassetapi_command(
            self.helper_project,
            self.dotnet_executable,
            "patch-dialogue-skip-subtitle-widget",
            str(root),
            str(source),
            str(output),
            str(mappings),
            self.engine_version,
        )
        try:
            try:
                completed = self.runner(
                    command, check=False, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=self.timeout_seconds, cwd=root,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise DialogueSkipError(f"Could not run the subtitle widget helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise DialogueSkipError(f"Subtitle widget patch failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise DialogueSkipError("Subtitle widget helper returned no result.")
            try:
                response: Any = json.loads(lines[-1])
            except json.JSONDecodeError as error:
                raise DialogueSkipError("Subtitle widget helper returned invalid JSON.") from error
            if not isinstance(response, dict):
                raise DialogueSkipError("Subtitle widget helper returned invalid JSON data.")
            if response.get("status") == "unsupported":
                _remove_package_outputs((output,))
                return DialogueSkipResult(
                    (), False, str(response.get("reason") or "Subtitle widget layout is unsupported.")
                )

            expected_outputs = {path.resolve() for path in _package_sidecars(output) if path.is_file()}
            raw_files = response.get("outputFiles")
            output_files = tuple(
                Path(value).resolve(strict=True) for value in raw_files if isinstance(value, str)
            ) if isinstance(raw_files, list) else ()
            if (
                response.get("status") != "patched"
                or response.get("virtualPackage") != SUBTITLE_WIDGET_PACKAGE
                or response.get("inputSource") != "configured Interact hold-progress"
                or response.get("activationPoint") != "first positive SetSkipIndicatorPercent_Interact progress"
                or response.get("skipGuard") != "IsInteractSkipProhibited"
                or response.get("outputAction") != "OnForceClose -> SkipStatement"
                or response.get("closeHandler") != "unchanged"
                or response.get("inputRecovery")
                != "native 0.5-second debounce and zero-progress reset; prohibited progress does not latch"
                or response.get("reopenedAndValidated") is not True
                or not output_files
                or {str(path).casefold() for path in output_files}
                != {str(path).casefold() for path in expected_outputs}
            ):
                raise DialogueSkipError("UAssetAPI did not validate the subtitle widget patch.")
            if any(not _is_within(path, root) or path.stat().st_size <= 0 for path in output_files):
                raise DialogueSkipError("Subtitle widget output is empty or escaped the generation workspace.")
            return DialogueSkipResult(output_files, True)
        except Exception:
            _remove_package_outputs((output,))
            raise


# Apply progress-triggered dialogue skip
def apply_tap_to_skip_dialogue(
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: DialogueSkipSerializer,
    progress: Callable[[str, int, int], None] | None = None,
) -> DialogueSkipResult:
    """Call the existing skip path on initial Interact progress."""
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    package_path = SUBTITLE_WIDGET_PACKAGE
    try:
        extracted = extractor.extract_assets(
            game_root,
            (package_path,),
            optional_virtual_paths=(package_path,),
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        extracted_by_path = {item.virtual_path.casefold(): item for item in extracted}
        if set(extracted_by_path) - {package_path.casefold()}:
            raise DialogueSkipError("Extraction returned an unexpected subtitle widget package.")
        item = extracted_by_path.get(package_path.casefold())
        if item is None:
            return DialogueSkipResult((), False, "The subtitle widget is absent from this game version.")

        materialized = materialize_extracted_assets(
            (item,), root / "source" / "dialogue-skip" / "Content"
        )
        if len(materialized) != 1 or materialized[0].virtual_path.casefold() != package_path.casefold():
            raise DialogueSkipError("Materialization returned a missing or unexpected subtitle widget package.")
        relative = Path(*package_path.removeprefix("/Game/").split("/")).with_suffix(".uasset")
        output = output_root / relative
        if output.exists() or any(path.exists() for path in _package_sidecars(output)):
            raise DialogueSkipError("Refusing to overwrite a staged subtitle widget package.")
        if progress:
            progress("patching-dialogue-skip", 0, 1)
        try:
            result = serializer.patch_subtitle_widget(
                work_root=root,
                mappings_path=mappings_path,
                input_file=materialized[0].primary_file,
                output_file=output,
            )
        except Exception:
            _remove_package_outputs((output,))
            raise
        if not result.applied:
            _remove_package_outputs((output,))
            return result
        if progress:
            progress("patching-dialogue-skip", 1, 1)
        return result
    except (AssetExtractionError, OSError, RuntimeError, ValueError) as error:
        if isinstance(error, DialogueSkipError):
            raise
        raise DialogueSkipError(f"Could not prepare the subtitle widget patch: {error}") from error


# List generated package files
def _package_sidecars(header: Path) -> tuple[Path, ...]:
    return (header, *(header.with_suffix(extension) for extension in (".uexp", ".ubulk", ".uptnl")))


# Remove incomplete package files
def _remove_package_outputs(headers: tuple[Path, ...]) -> None:
    for header in headers:
        for path in _package_sidecars(header):
            path.unlink(missing_ok=True)


# Check path ownership
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True
