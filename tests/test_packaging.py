from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from cv2randomizer.packaging.retoc import PackageBuildError, RetocPackager, RETOC_VERSION, _copy_content_tree


class FakeRetocRunner:
    # Initialize this object
    def __init__(
        self,
        *,
        listed_paths: tuple[str, ...] | None = None,
        version: str = RETOC_VERSION,
        compression_methods: str = "[]",
    ) -> None:
        self.listed_paths = listed_paths
        self.version = version
        self.compression_methods = compression_methods
        self.commands: list[list[str]] = []
        self.mount_root: Path | None = None
        self.mounted_paths: set[str] = set()
        self.executable_directories: list[tuple[str, ...]] = []
        self.process_paths: list[str] = []
        self.working_directories: list[Path] = []
        self.source_content: Path | None = None
        self.mounted_hardlinks: set[str] = set()

    # Call the wrapped operation
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        executable_directory = Path(command[0]).parent
        self.executable_directories.append(tuple(sorted(path.name for path in executable_directory.iterdir())))
        self.working_directories.append(Path(kwargs["cwd"]))
        environment = kwargs.get("env")
        self.process_paths.append(environment["PATH"] if isinstance(environment, dict) else "")
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, self.version + "\n", "")
        if command[1] == "to-zen":
            self.mount_root = Path(command[-2])
            content = self.mount_root / "CodeVein2" / "Content"
            self.mounted_paths = {
                path.relative_to(content).as_posix()
                for path in content.rglob("*") if path.is_file()
            }
            if self.source_content is not None:
                self.mounted_hardlinks = {
                    path.relative_to(content).as_posix()
                    for path in content.rglob("*") if path.is_file()
                    and path.samefile(self.source_content / path.relative_to(content))
                }
            output_utoc = Path(command[-1])
            output_utoc.write_bytes(b"verified utoc bytes")
            output_utoc.with_suffix(".ucas").write_bytes(b"verified ucas bytes")
            output_utoc.with_suffix(".pak").write_bytes(b"verified pak bytes")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "info":
            return subprocess.CompletedProcess(
                command, 0,
                f"Container info: CV2Randomizer_P\n  compression_methods: {self.compression_methods}\n",
                "",
            )
        if command[1] == "list":
            if self.listed_paths is not None:
                paths = self.listed_paths
            else:
                content = self.mount_root / "CodeVein2" / "Content"
                paths = tuple(
                    "../../../CodeVein2/Content/" + path.relative_to(content).as_posix()
                    for path in content.rglob("*")
                    if path.is_file() and path.suffix.casefold() in {".uasset", ".umap"}
                )
            return subprocess.CompletedProcess(command, 0, "\n".join("container " + path for path in paths), "")
        if command[1] == "verify":
            return subprocess.CompletedProcess(command, 0, "verified\n", "")
        raise AssertionError(f"Unexpected retoc command: {command!r}")


class RetocPackagerTests(unittest.TestCase):
    # Read content
    def _content(self, root: Path) -> Path:
        content = root / "staging" / "Content"
        shop = content / "Blueprint" / "GameSystem" / "Shop"
        generated_map = content / "Map" / "Generated"
        shop.mkdir(parents=True)
        generated_map.mkdir(parents=True)
        (shop / "DT_ShopLineup.uasset").write_bytes(b"shop header")
        (shop / "DT_ShopLineup.uexp").write_bytes(b"shop exports")
        (generated_map / "Map001.umap").write_bytes(b"map header")
        (generated_map / "Map001.uexp").write_bytes(b"map exports")
        return content

    # Check build check all three containers
    def test_builds_and_checks_all_three_containers_and_mount_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = self._content(root)
            runner = FakeRetocRunner()
            runner.source_content = content
            packager = RetocPackager(executable=root / "retoc.exe", runner=runner)
            (root / "retoc.exe").write_bytes(b"test helper")

            result = packager.package(
                content,
                work_root=root,
                output_directory=root / "packaged",
                mod_name="CV2Randomizer",
            )

            self.assertEqual(result.package_name, "CV2Randomizer_P")
            self.assertEqual({path.suffix for path in result.files}, {".pak", ".ucas", ".utoc"})
            self.assertTrue(all(path.is_file() and path.stat().st_size for path in result.files))
            self.assertEqual(
                set(result.package_paths),
                {
                    "../../../CodeVein2/Content/Blueprint/GameSystem/Shop/DT_ShopLineup.uasset",
                    "../../../CodeVein2/Content/Map/Generated/Map001.umap",
                },
            )
            self.assertIn("Blueprint/GameSystem/Shop/DT_ShopLineup.uasset", runner.mounted_paths)
            self.assertEqual(runner.mounted_hardlinks, runner.mounted_paths)
            self.assertEqual([command[1] for command in runner.commands], ["--version", "to-zen", "info", "list", "verify"])
            self.assertTrue(all(command[0] != str(root / "retoc.exe") for command in runner.commands))
            self.assertEqual(runner.executable_directories[0], ("retoc.exe",))
            self.assertEqual(len(set(runner.process_paths)), 1)
            self.assertEqual(len(set(runner.working_directories)), 1)
            self.assertEqual(runner.working_directories[0].parent, root)

    # Check refuses promote container info reports
    def test_refuses_to_promote_a_container_if_info_reports_compression_methods(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = self._content(root)
            helper = root / "retoc.exe"
            helper.write_bytes(b"test helper")
            output = root / "packaged"
            runner = FakeRetocRunner(compression_methods='["Oodle"]')

            with self.assertRaisesRegex(PackageBuildError, "uncompressed container"):
                RetocPackager(executable=helper, runner=runner).package(
                    content, work_root=root, output_directory=output,
                )

            self.assertEqual(list(output.glob("*")), [])
            self.assertEqual([command[1] for command in runner.commands], ["--version", "to-zen", "info"])

    # Check reject missing sidecar before running
    def test_rejects_missing_sidecar_before_running_retoc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = self._content(root)
            (content / "Map" / "Generated" / "Map001.uexp").unlink()
            helper = root / "retoc.exe"
            helper.write_bytes(b"test helper")
            runner = FakeRetocRunner()
            with self.assertRaisesRegex(PackageBuildError, "sidecars do not match"):
                RetocPackager(executable=helper, runner=runner).package(
                    content, work_root=root, output_directory=root / "packaged",
                )
            self.assertFalse(any(command[1] == "to-zen" for command in runner.commands))
            self.assertEqual(list((root / "packaged").glob("*")), [])

    # Check packages explicit changed file registry
    def test_packages_the_explicit_changed_file_registry_without_copying_unregistered_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = self._content(root)
            unregistered = content / "Research" / "not-a-mod-output.txt"
            unregistered.parent.mkdir()
            unregistered.write_text("local diagnostic only", encoding="utf-8")
            registered = tuple(sorted(
                (content / "Map" / "Generated" / "Map001.umap",
                 content / "Map" / "Generated" / "Map001.uexp"),
                key=lambda path: path.name,
            ))
            helper = root / "retoc.exe"
            helper.write_bytes(b"test helper")
            runner = FakeRetocRunner()

            result = RetocPackager(executable=helper, runner=runner).package(
                content,
                work_root=root,
                output_directory=root / "packaged",
                input_files=registered,
            )

            self.assertEqual(len(result.package_paths), 1)
            self.assertEqual(
                runner.mounted_paths,
                {"Map/Generated/Map001.umap", "Map/Generated/Map001.uexp"},
            )

    # Check explicit registry must contain each
    def test_explicit_registry_must_contain_each_export_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = self._content(root)
            helper = root / "retoc.exe"
            helper.write_bytes(b"test helper")
            runner = FakeRetocRunner()
            with self.assertRaisesRegex(PackageBuildError, "sidecars do not match"):
                RetocPackager(executable=helper, runner=runner).package(
                    content,
                    work_root=root,
                    output_directory=root / "packaged",
                    input_files=(content / "Map" / "Generated" / "Map001.umap",),
                )
            self.assertFalse(any(command[1] == "to-zen" for command in runner.commands))

    # Check reject wrong container inventory commits
    def test_rejects_wrong_container_inventory_and_commits_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = self._content(root)
            helper = root / "retoc.exe"
            helper.write_bytes(b"test helper")
            runner = FakeRetocRunner(listed_paths=("../../../CodeVein2/Content/Extra.uasset",))
            with self.assertRaisesRegex(PackageBuildError, "inventory mismatch"):
                RetocPackager(executable=helper, runner=runner).package(
                    content, work_root=root, output_directory=root / "packaged",
                )
            self.assertEqual(list((root / "packaged").glob("*")), [])

    # Check retoc mount copy fall back
    def test_retoc_mount_copy_falls_back_when_hard_links_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = self._content(root)
            files = tuple(sorted(path for path in content.rglob("*") if path.is_file()))
            mount = root / "temporary" / "CodeVein2" / "Content"
            with patch("cv2randomizer.packaging.retoc.os.link", side_effect=OSError("hard links disabled")):
                metrics = _copy_content_tree(content, mount, files)

            self.assertEqual(metrics["hardlink_files"], 0)
            self.assertEqual(metrics["copy_fallback_files"], len(files))
            self.assertEqual(metrics["copy_fallback_bytes"], sum(path.stat().st_size for path in files))
            for source in files:
                target = mount / source.relative_to(content)
                self.assertEqual(source.read_bytes(), target.read_bytes())
                self.assertFalse(source.samefile(target))

    # Check refuses overwrite existing owned package
    def test_refuses_to_overwrite_an_existing_owned_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = self._content(root)
            helper = root / "retoc.exe"
            helper.write_bytes(b"test helper")
            output = root / "packaged"
            output.mkdir()
            existing = output / "CV2Randomizer_P.pak"
            existing.write_bytes(b"keep existing")
            runner = FakeRetocRunner()
            with self.assertRaisesRegex(PackageBuildError, "Refusing to overwrite"):
                RetocPackager(executable=helper, runner=runner).package(
                    content, work_root=root, output_directory=output,
                )
            self.assertEqual(existing.read_bytes(), b"keep existing")


if __name__ == "__main__":
    unittest.main()
