"""Build and verify IoStore mod containers from validated legacy staging."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.core.resources import resolve_retoc_executable

RETOC_VERSION = "retoc_cli 0.1.5"
ENGINE_VERSION = "UE5_4"
_PACKAGE_EXTENSIONS = (".pak", ".ucas", ".utoc")
_CONTENT_EXTENSIONS = {".uasset", ".umap", ".uexp", ".ubulk", ".uptnl"}
_MOD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_MOUNT_PREFIX = "../../../CodeVein2/Content/"


class PackageBuildError(RuntimeError):
    """Raised when a staged Content tree cannot produce a verified mod package."""


@dataclass(frozen=True, slots=True)
class PackageResult:
    """A verified `.pak`, `.utoc`, and `.ucas` set emitted from staging."""

    package_name: str
    files: tuple[Path, ...]
    package_paths: tuple[str, ...]


class RetocPackager:
    """Invoke the pinned retoc_cli to create the game's IoStore mod format."""

    # Initialize this object
    def __init__(
        self,
        *,
        executable: Path | None = None,
        timeout_seconds: float = 1800,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
    ) -> None:
        self.executable = executable or resolve_retoc_executable()
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self._process_environment: dict[str, str] = {}
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Resolve the package path
    def package(
        self,
        content_root: Path,
        *,
        work_root: Path,
        output_directory: Path,
        mod_name: str = "CV2Randomizer",
        input_files: tuple[Path, ...] | None = None,
    ) -> PackageResult:
        """Package validated legacy assets with a `CodeVein2/Content` mount point."""
        package_started = time.perf_counter()
        timings: dict[str, float] = {}
        if not isinstance(mod_name, str) or not _MOD_NAME.fullmatch(mod_name):
            raise PackageBuildError("Mod name must use 1-64 ASCII letters, digits, underscores, or hyphens.")
        root = work_root.expanduser().resolve(strict=True)
        source_root = content_root.expanduser().resolve(strict=True)
        destination = output_directory.expanduser().resolve()
        if not root.is_dir() or not source_root.is_dir():
            raise PackageBuildError("The workspace and staged Content path must be directories.")
        if not _is_within(source_root, root) or not _is_within(destination, root):
            raise PackageBuildError("Package inputs and outputs must stay inside the generation workspace.")
        if source_root == destination or source_root in destination.parents:
            raise PackageBuildError("Package output must be separate from the staged Content tree.")
        if not self.executable.is_file():
            raise PackageBuildError(f"retoc_cli {RETOC_VERSION.rsplit(' ', 1)[-1]} was not found at {self.executable}.")
        phase_started = time.perf_counter()
        staged_files = (
            _validate_content_tree(source_root)
            if input_files is None
            else _validate_content_inventory(source_root, input_files)
        )
        timings["retoc_registry_validation_seconds"] = time.perf_counter() - phase_started
        if not staged_files:
            raise PackageBuildError("Staged Content is empty; there are no assets to package.")
        expected_package_paths = tuple(sorted(
            _MOUNT_PREFIX + path.relative_to(source_root).as_posix()
            for path in staged_files
            if path.suffix.casefold() in {".uasset", ".umap"}
        ))
        if not expected_package_paths:
            raise PackageBuildError("Staging contains no `.uasset` or `.umap` package headers.")

        package_name = mod_name + "_P"
        destination.mkdir(parents=True, exist_ok=True)
        targets = tuple(destination / (package_name + extension) for extension in _PACKAGE_EXTENSIONS)
        if any(target.exists() or target.is_symlink() for target in targets):
            raise PackageBuildError(f"Refusing to overwrite an existing package named {package_name}.")

        committed: list[Path] = []
        try:
            with tempfile.TemporaryDirectory(prefix=".cv2-package-", dir=root) as temporary:
                temporary_root = Path(temporary)
                executable = temporary_root / self.executable.name
                shutil.copyfile(self.executable, executable)
                if _sha256_file(self.executable) != _sha256_file(executable):
                    raise PackageBuildError("Staged Retoc executable failed integrity validation.")
                self._process_environment = _no_oodle_environment()
                phase_started = time.perf_counter()
                self._verify_version(temporary_root, executable=executable)
                timings["retoc_version_check_seconds"] = time.perf_counter() - phase_started

                mount_root = temporary_root / "input"
                mounted_content = mount_root / "CodeVein2" / "Content"
                phase_started = time.perf_counter()
                mount_metrics = _copy_content_tree(source_root, mounted_content, staged_files)
                timings["retoc_mount_copy_and_integrity_seconds"] = time.perf_counter() - phase_started
                package_root = temporary_root / "generated"
                package_root.mkdir()
                temporary_utoc = package_root / (package_name + ".utoc")
                phase_started = time.perf_counter()
                self._run(
                    [str(executable), "to-zen", "--version", ENGINE_VERSION,
                     str(mount_root), str(temporary_utoc)],
                    cwd=temporary_root,
                    operation="create IoStore containers",
                )
                timings["retoc_to_zen_seconds"] = time.perf_counter() - phase_started
                generated = tuple(package_root / (package_name + extension) for extension in _PACKAGE_EXTENSIONS)
                if any(not path.is_file() or path.stat().st_size <= 0 for path in generated):
                    raise PackageBuildError("retoc_cli did not produce all three non-empty mod containers.")

                phase_started = time.perf_counter()
                info = self._run(
                    [str(executable), "info", str(temporary_utoc)],
                    cwd=temporary_root,
                    operation="inspect IoStore container info",
                )
                timings["retoc_info_seconds"] = time.perf_counter() - phase_started
                compression_line = next(
                    (line.strip().split(":", 1)[1].strip()
                     for line in info.stdout.splitlines()
                     if line.strip().startswith("compression_methods:")),
                    None,
                )
                if compression_line != "[]":
                    raise PackageBuildError(
                        "Retoc output did not confirm an uncompressed container "
                        f"(compression_methods={compression_line!r})."
                    )

                phase_started = time.perf_counter()
                listing = self._run(
                    [str(executable), "list", "--path", str(temporary_utoc)],
                    cwd=temporary_root,
                    operation="inspect IoStore package paths",
                )
                timings["retoc_list_seconds"] = time.perf_counter() - phase_started
                observed_package_paths = _listed_package_paths(listing.stdout)
                if set(observed_package_paths) != set(expected_package_paths):
                    missing = sorted(set(expected_package_paths) - set(observed_package_paths))
                    unexpected = sorted(set(observed_package_paths) - set(expected_package_paths))
                    raise PackageBuildError(
                        f"IoStore path inventory mismatch (missing={missing}, unexpected={unexpected})."
                    )

                phase_started = time.perf_counter()
                verification = self._run(
                    [str(executable), "verify", str(temporary_utoc)],
                    cwd=temporary_root,
                    operation="verify IoStore container",
                )
                if "verified" not in verification.stdout.casefold():
                    raise PackageBuildError("retoc_cli did not confirm the generated `.utoc` container.")
                timings["retoc_verify_seconds"] = time.perf_counter() - phase_started

                phase_started = time.perf_counter()
                for source, target in zip(generated, targets, strict=True):
                    if source.stat().st_size <= 0:
                        raise PackageBuildError(f"Generated package is empty: {source.name}.")
                    source.replace(target)
                    committed.append(target)
                timings["retoc_output_commit_seconds"] = time.perf_counter() - phase_started
            timings["retoc_total_seconds"] = time.perf_counter() - package_started
            report = {
                "timings_seconds": timings,
                "input_package_count": len(expected_package_paths),
                "input_file_count": len(staged_files),
                "input_bytes": sum(path.stat().st_size for path in staged_files),
                "integrity_hash_file_reads": 0,
                "mount_hardlink_files": mount_metrics["hardlink_files"],
                "mount_copy_fallback_files": mount_metrics["copy_fallback_files"],
                "mount_copy_fallback_bytes": mount_metrics["copy_fallback_bytes"],
                "output_commit_strategy": "same-volume atomic rename",
                "output_files": [
                    {"name": path.name, "size": path.stat().st_size}
                    for path in targets
                ],
                "retoc_info": info.stdout.strip(),
                "retoc_verify": verification.stdout.strip(),
                "retoc_listed_package_count": len(observed_package_paths),
            }
            diagnostics = root / "logs" / "retoc_timing.json"
            diagnostics.parent.mkdir(parents=True, exist_ok=True)
            temporary_report = diagnostics.with_name(diagnostics.name + ".tmp")
            temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary_report.replace(diagnostics)
            return PackageResult(package_name, targets, expected_package_paths)
        except Exception as error:
            for path in committed:
                if _is_within(path, destination) and path.is_file():
                    path.unlink()
            if isinstance(error, PackageBuildError):
                raise
            raise PackageBuildError(f"Could not build the mod package: {error}") from error

    # Verify version
    def _verify_version(self, cwd: Path, *, executable: Path) -> None:
        result = self._run([str(executable), "--version"], cwd=cwd, operation="check retoc_cli version")
        if result.stdout.strip() != RETOC_VERSION:
            raise PackageBuildError(
                f"Expected {RETOC_VERSION}, but retoc_cli reported {result.stdout.strip() or result.stderr.strip()!r}."
            )

    # Run the external helper command
    def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        operation: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                cwd=cwd,
                env=self._process_environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PackageBuildError(f"Could not {operation}: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-3000:]
            raise PackageBuildError(f"Could not {operation} (exit {result.returncode}): {detail}")
        return result


# Read no oodle environment
def _no_oodle_environment() -> dict[str, str]:
    """Give Retoc only the standard Windows runtime search path for this test mode."""
    environment = os.environ.copy()
    system_root = environment.get("SystemRoot") or environment.get("WINDIR")
    if system_root:
        environment["PATH"] = os.pathsep.join((str(Path(system_root) / "System32"), system_root))
    else:
        environment["PATH"] = ""
    return environment


# Validate content tree
def _validate_content_tree(content_root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in content_root.rglob("*"):
        if path.is_symlink():
            raise PackageBuildError("Staged Content must not contain symlinks.")
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if not _is_within(resolved, content_root):
            raise PackageBuildError("A staged package resolves outside the Content tree.")
        if path.suffix.casefold() not in _CONTENT_EXTENSIONS:
            raise PackageBuildError(f"Unsupported staged file extension: {path.suffix}.")
        if path.stat().st_size <= 0:
            raise PackageBuildError(f"Staged package file is empty: {path.name}.")
        files.append(resolved)
    headers = {path.with_suffix(".uexp").resolve() for path in files if path.suffix.casefold() in {".uasset", ".umap"}}
    actual_exports = {path.resolve() for path in files if path.suffix.casefold() == ".uexp"}
    if headers != actual_exports:
        missing = sorted(path.name for path in headers - actual_exports)
        orphaned = sorted(path.name for path in actual_exports - headers)
        raise PackageBuildError(f"Staged package sidecars do not match headers (missing={missing}, orphaned={orphaned}).")
    return tuple(sorted(files, key=lambda path: path.relative_to(content_root).as_posix().casefold()))


# Validate content inventory
def _validate_content_inventory(content_root: Path, input_files: tuple[Path, ...]) -> tuple[Path, ...]:
    """Validate the caller-owned changed-file registry without walking Content."""
    files: set[Path] = set()
    for value in input_files:
        candidate = Path(value)
        if candidate.is_symlink():
            raise PackageBuildError("Staged Content must not contain symlinks.")
        resolved = candidate.resolve(strict=True)
        if not _is_within(resolved, content_root):
            raise PackageBuildError("A registered package file resolves outside staged Content.")
        parent = candidate.parent
        while parent != content_root and parent != parent.parent:
            if parent.is_symlink():
                raise PackageBuildError("Staged Content must not contain symlink directories.")
            parent = parent.parent
        if not resolved.is_file():
            raise PackageBuildError("A registered staged package file is not a regular file.")
        if resolved.suffix.casefold() not in _CONTENT_EXTENSIONS:
            raise PackageBuildError(f"Unsupported staged file extension: {resolved.suffix}.")
        if resolved.stat().st_size <= 0:
            raise PackageBuildError(f"Staged package file is empty: {resolved.name}.")
        files.add(resolved)
    if len(files) != len(input_files):
        raise PackageBuildError("The changed-package registry contains duplicate file entries.")
    headers = {path.with_suffix(".uexp").resolve() for path in files if path.suffix.casefold() in {".uasset", ".umap"}}
    actual_exports = {path.resolve() for path in files if path.suffix.casefold() == ".uexp"}
    if headers != actual_exports:
        missing = sorted(path.name for path in headers - actual_exports)
        orphaned = sorted(path.name for path in actual_exports - headers)
        raise PackageBuildError(f"Staged package sidecars do not match headers (missing={missing}, orphaned={orphaned}).")
    return tuple(sorted(files, key=lambda path: path.relative_to(content_root).as_posix().casefold()))


# Copy content tree
def _copy_content_tree(source_root: Path, mounted_root: Path, files: tuple[Path, ...]) -> dict[str, int]:
    hardlink_files = 0
    copy_fallback_files = 0
    copy_fallback_bytes = 0
    for source in files:
        relative = source.relative_to(source_root)
        target = mounted_root / relative
        if not _is_within(target, mounted_root):
            raise PackageBuildError("Resolved mount path escaped CodeVein2/Content.")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Retoc consumes this tree as read-only input. Same-volume hard links
            # avoid copying hundreds of MiB; the temporary tree is removed after
            # packaging, and no patcher is given these paths.
            os.link(source, target)
            if not os.path.samefile(source, target):
                raise PackageBuildError(f"Retoc input hard link failed identity validation: {relative.as_posix()}.")
            hardlink_files += 1
        except OSError:
            target.unlink(missing_ok=True)
            shutil.copyfile(source, target)
            if target.stat().st_size != source.stat().st_size:
                raise PackageBuildError(f"Mounted asset copy has an unexpected size: {relative.as_posix()}.")
            copy_fallback_files += 1
            copy_fallback_bytes += target.stat().st_size
    return {
        "hardlink_files": hardlink_files,
        "copy_fallback_files": copy_fallback_files,
        "copy_fallback_bytes": copy_fallback_bytes,
    }


# Resolve listed package paths
def _listed_package_paths(output: str) -> tuple[str, ...]:
    prefix = "../../../CodeVein2/Content/"
    paths: list[str] = []
    for line in output.splitlines():
        if prefix not in line:
            continue
        raw_path = line.split(prefix, 1)[1].split()[0].strip('"')
        if Path(raw_path).suffix.casefold() in {".uasset", ".umap"}:
            paths.append(prefix + raw_path.replace("\\", "/"))
    return tuple(sorted(paths))


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
