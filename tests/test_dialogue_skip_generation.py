from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.dialogue_skip_generation import (
    SUBTITLE_WIDGET_PACKAGE,
    DialogueSkipError,
    DialogueSkipResult,
    DialogueSkipSerializer,
    apply_tap_to_skip_dialogue,
)
from cv2randomizer.generation.pipeline import _changed_package_registry, _expected_output_files


class DialogueSkipGenerationTests(unittest.TestCase):
    # Skip an absent optional widget
    def test_optional_extraction_skips_absent_widget(self) -> None:
        class MissingExtractor:
            def extract_assets(self, _game_root, packages, *, optional_virtual_paths, **_kwargs):
                self.requested = tuple(packages)
                self.optional = tuple(optional_virtual_paths)
                return ()

        extractor = MissingExtractor()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_root = root / "generation"
            work_root.mkdir()
            result = apply_tap_to_skip_dialogue(
                game_root=root / "game",
                work_root=work_root,
                output_content_root=work_root / "staging" / "Content",
                mappings_path=root / "CV2.usmap",
                game_version="old-build",
                data_revision="revision",
                extractor=extractor,
                serializer=object(),
            )

        self.assertFalse(result.applied)
        self.assertIn("absent", result.reason or "")
        self.assertEqual(extractor.requested, (SUBTITLE_WIDGET_PACKAGE,))
        self.assertEqual(extractor.optional, (SUBTITLE_WIDGET_PACKAGE,))

    # Patch the subtitle hold callback
    def test_apply_patches_and_stages_only_subtitle_widget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            work_root = root / "generation"
            work_root.mkdir()

            class Extractor:
                def extract_assets(self, _game_root, packages, *, optional_virtual_paths, **_kwargs):
                    self.requested = tuple(packages)
                    self.optional = tuple(optional_virtual_paths)
                    source = root / "cache" / "WDT_Subtitles.uasset"
                    source.parent.mkdir(parents=True)
                    source.write_bytes(b"source header")
                    source.with_suffix(".uexp").write_bytes(b"source exports")
                    return (ExtractedAsset(SUBTITLE_WIDGET_PACKAGE, (source, source.with_suffix(".uexp"))),)

            class Serializer:
                def patch_subtitle_widget(self, *, output_file, input_file, **_kwargs):
                    self.input_file = input_file
                    output_file.parent.mkdir(parents=True)
                    output_file.write_bytes(b"patched header")
                    sidecar = output_file.with_suffix(".uexp")
                    sidecar.write_bytes(b"patched exports")
                    return DialogueSkipResult((output_file, sidecar), True)

            extractor = Extractor()
            serializer = Serializer()
            content_root = work_root / "staging" / "Content"
            result = apply_tap_to_skip_dialogue(
                game_root=root / "game",
                work_root=work_root,
                output_content_root=content_root,
                mappings_path=root / "CV2.usmap",
                game_version="2.0.3",
                data_revision="revision",
                extractor=extractor,
                serializer=serializer,
            )
            self.assertTrue(result.applied)
            self.assertEqual(extractor.requested, (SUBTITLE_WIDGET_PACKAGE,))
            self.assertEqual(extractor.optional, (SUBTITLE_WIDGET_PACKAGE,))
            self.assertEqual(serializer.input_file.name, "WDT_Subtitles.uasset")
            self.assertIn("dialogue-skip", str(serializer.input_file))
            self.assertEqual(len(result.output_files), 2)
            self.assertTrue(all(path.is_file() for path in result.output_files))
            self.assertIn("WDT_Subtitles", str(result.output_files[0]))

    # Clean failed widget patches
    def test_failed_widget_patch_removes_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            work_root = root / "generation"
            work_root.mkdir()
            content_root = work_root / "staging" / "Content"

            class Extractor:
                def extract_assets(self, _game_root, _packages, **_kwargs):
                    source = root / "cache" / "WDT_Subtitles.uasset"
                    source.parent.mkdir(parents=True)
                    source.write_bytes(b"source")
                    return (ExtractedAsset(SUBTITLE_WIDGET_PACKAGE, (source,)),)

            class FailingSerializer:
                def patch_subtitle_widget(self, *, output_file, **_kwargs):
                    output_file.parent.mkdir(parents=True)
                    output_file.write_bytes(b"partial")
                    output_file.with_suffix(".uexp").write_bytes(b"partial")
                    raise DialogueSkipError("validation failed")

            with self.assertRaises(DialogueSkipError):
                apply_tap_to_skip_dialogue(
                    game_root=root / "game",
                    work_root=work_root,
                    output_content_root=content_root,
                    mappings_path=root / "CV2.usmap",
                    game_version="2.0.3",
                    data_revision="revision",
                    extractor=Extractor(),
                    serializer=FailingSerializer(),
                )

        self.assertFalse(tuple(content_root.rglob("WDT_Subtitles.*")) if content_root.exists() else ())

    # Validate helper output identity
    def test_serializer_requires_reopened_guarded_progress_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            helper = root / "helper.csproj"
            helper.touch()
            mappings = root / "CV2.usmap"
            mappings.touch()
            source = root / "source" / "WDT_Subtitles.uasset"
            source.parent.mkdir()
            source.write_bytes(b"source")
            output = root / "staging" / "WDT_Subtitles.uasset"

            def runner(command, **_kwargs):
                self.assertIn("patch-dialogue-skip-subtitle-widget", command)
                output.parent.mkdir(parents=True)
                output.write_bytes(b"patched")
                sidecar = output.with_suffix(".uexp")
                sidecar.write_bytes(b"exports")
                response = {
                    "status": "patched",
                    "virtualPackage": SUBTITLE_WIDGET_PACKAGE,
                    "inputSource": "configured Interact hold-progress",
                    "activationPoint": "first positive SetSkipIndicatorPercent_Interact progress",
                    "skipGuard": "IsInteractSkipProhibited",
                    "outputAction": "OnForceClose -> SkipStatement",
                    "closeHandler": "unchanged",
                    "inputRecovery": "native 0.5-second debounce and zero-progress reset; prohibited progress does not latch",
                    "outputFiles": [str(output), str(sidecar)],
                    "reopenedAndValidated": True,
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

            result = DialogueSkipSerializer(helper_project=helper, runner=runner).patch_subtitle_widget(
                work_root=root,
                mappings_path=mappings,
                input_file=source,
                output_file=output,
            )

        self.assertTrue(result.applied)
        self.assertEqual(set(result.output_files), {output, output.with_suffix(".uexp")})

    # Remove incomplete serializer output
    def test_serializer_rejects_unvalidated_output_and_cleans_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            helper = root / "helper.csproj"
            helper.touch()
            mappings = root / "CV2.usmap"
            mappings.touch()
            source = root / "source" / "WDT_Subtitles.uasset"
            source.parent.mkdir()
            source.write_bytes(b"source")
            output = root / "staging" / "WDT_Subtitles.uasset"

            def runner(command, **_kwargs):
                output.parent.mkdir(parents=True)
                output.write_bytes(b"partial")
                return subprocess.CompletedProcess(command, 0, json.dumps({"status": "patched"}), "")

            serializer = DialogueSkipSerializer(helper_project=helper, runner=runner)
            with self.assertRaises(DialogueSkipError):
                serializer.patch_subtitle_widget(
                    work_root=root,
                    mappings_path=mappings,
                    input_file=source,
                    output_file=output,
                )

        self.assertFalse(output.exists())

    # Register the changed widget package
    def test_dialogue_widget_joins_miscellaneous_output_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            content = root / "Content"
            outputs = (
                content / "UI/Subtitles/WDT_Subtitles.uasset",
                content / "UI/Subtitles/WDT_Subtitles.uexp",
            )
            for path in outputs:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"validated")
            result = DialogueSkipResult(outputs, True)
            expected = _expected_output_files(None, None, None, dialogue_skip=result)
            registry = _changed_package_registry(
                content,
                world=None,
                shops=None,
                materials=None,
                enemy_drops=None,
                blood_codes=None,
                boosters=None,
                defensive_gadgets=None,
                bequeathed_formae=None,
                direct_grants=(),
                extractor=None,
                initial_inventory=None,
                dialogue_skip=result,
            )

        self.assertEqual(set(expected), set(outputs))
        self.assertEqual({entry["sources"][0] for entry in registry}, {"miscellaneous"})
        self.assertEqual({entry["virtual_package"] for entry in registry}, {SUBTITLE_WIDGET_PACKAGE})


if __name__ == "__main__":
    unittest.main()
