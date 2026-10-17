from __future__ import annotations

import unittest

from cv2randomizer.game.version_compatibility import is_game_data_layout_mismatch


class GameDataLayoutCompatibilityTests(unittest.TestCase):
    # Check only known schema failures are skippable
    def test_only_explicit_uasset_data_rejections_are_version_skips(self) -> None:
        self.assertTrue(is_game_data_layout_mismatch(
            RuntimeError("UAssetAPI failed: System.IO.InvalidDataException: StoryFlag schema changed")
        ))
        wrapped = RuntimeError("Blood Code helper rejected its source file")
        wrapped.__cause__ = ValueError("source fingerprint changed for /Game/Quest/Reward")
        self.assertTrue(is_game_data_layout_mismatch(wrapped))
        for message in (
            "Direct reward source fingerprint differs for this build",
            "Quest export 40 is QSTask_GoalLocation_0, not the expected preserved task QSTask_GainItem_0",
            "Target export parsed as RawExport; required Blueprint schemas could not be loaded",
            "Catalog material class does not match the source actor class",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_game_data_layout_mismatch(RuntimeError(message)))
        self.assertFalse(is_game_data_layout_mismatch(RuntimeError("helper timed out")))
        self.assertFalse(is_game_data_layout_mismatch(RuntimeError("file is being used by another process")))


if __name__ == "__main__":
    unittest.main()
