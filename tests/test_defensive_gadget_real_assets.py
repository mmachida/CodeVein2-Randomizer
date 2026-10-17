from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.game.defensive_gadget_generation import DefensiveGadgetSerializer
from cv2randomizer.core.workspace import CacheManager


ROOT = Path(__file__).resolve().parents[1]
_QUEST_PACKAGE = Path(
    "Quest/CharacterEpisode/CharacterEpisode_Lise/QST_CharacterEpisode_Lise_c02070/"
    "QST_CharacterEpisode_Lise_c02070.uasset"
)
_WORKSPACE_SOURCE = ROOT / "workspace" / "cache" / "defensive-gadget-research" / "Content" / _QUEST_PACKAGE
_USER_CACHE_SOURCES = sorted(CacheManager().root.glob(f"*/assets/{_QUEST_PACKAGE.as_posix()}"))
SOURCE = _WORKSPACE_SOURCE if _WORKSPACE_SOURCE.is_file() else next(
    iter(_USER_CACHE_SOURCES), _WORKSPACE_SOURCE,
)
MAPPINGS = ROOT / "workspace" / "research_assets" / "CV2.usmap"


@unittest.skipUnless(SOURCE.is_file() and MAPPINGS.is_file(), "local Warp research asset and CV2.usmap are not available")
class DefensiveGadgetRealAssetTests(unittest.TestCase):
    # Check warp grant reopens base DLC
    def test_warp_grant_reopens_with_base_and_dlc_reward_identities(self) -> None:
        source_hashes = {
            path.suffix: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (SOURCE, SOURCE.with_suffix(".uexp"))
        }
        catalog = DefensiveGadgetCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        slot = catalog.grants[0]
        targets = (
            catalog.resolve("DA_DefGadget_Shield_3"),
            catalog.resolve("DA_DefGadget_ZenonGreatShield"),
        )
        self.assertTrue(all(target is not None for target in targets))
        package = slot.package_path
        with tempfile.TemporaryDirectory(prefix="cv2-defensive-gadget-real-asset-") as temporary:
            work_root = Path(temporary).resolve()
            input_header = work_root / "source" / SOURCE.name
            input_header.parent.mkdir(parents=True)
            input_header.write_bytes(SOURCE.read_bytes())
            input_header.with_suffix(".uexp").write_bytes(SOURCE.with_suffix(".uexp").read_bytes())
            serializer = DefensiveGadgetSerializer()
            snapshot = serializer.inspect_packages(
                work_root=work_root,
                mappings_path=MAPPINGS,
                package_files={package: input_header},
            )
            self.assertEqual(len(snapshot), 1)
            if (
                snapshot[0].export_index != slot.export_index
                or snapshot[0].export_name != slot.export_name
                or snapshot[0].reward_package != slot.original_asset_path
                or snapshot[0].reward_asset != slot.original_asset_path.rsplit("/", 1)[-1]
            ):
                self.skipTest("local cached package belongs to a game build that differs from the current catalog")
            self.assertEqual(snapshot[0].export_index, slot.export_index)
            self.assertEqual(snapshot[0].export_name, slot.export_name)
            self.assertEqual(snapshot[0].reward_package, slot.original_asset_path)
            self.assertEqual(snapshot[0].reward_asset, slot.original_asset_path.rsplit("/", 1)[-1])
            for index, target in enumerate(targets):
                assert target is not None
                output_header = work_root / "staging" / f"{index}-{SOURCE.name}"
                patch = {
                    "exportIndex": slot.export_index,
                    "exportName": slot.export_name,
                    "bulkIndex": slot.bulk_index,
                    "rewardIndex": slot.reward_index,
                    "expectedPackage": slot.original_asset_path,
                    "expectedAsset": slot.original_asset_path.rsplit("/", 1)[-1],
                    "expectedCount": slot.count,
                    "expectedNotification": slot.notification,
                    "assignedPackage": target.asset_path,
                    "assignedAsset": target.object,
                }
                result = serializer.patch_packages(
                    work_root=work_root,
                    mappings_path=MAPPINGS,
                    packages={package: (input_header, output_header, (patch,))},
                )[0]
                self.assertTrue(result["reopenedAndValidated"])
                self.assertEqual(result["patchedSlots"], 1)
                self.assertTrue(output_header.is_file())
                self.assertTrue(output_header.with_suffix(".uexp").is_file())
                reopened = serializer.inspect_packages(
                    work_root=work_root,
                    mappings_path=MAPPINGS,
                    package_files={package: output_header},
                )
                self.assertEqual(len(reopened), 1)
                self.assertEqual((reopened[0].reward_package, reopened[0].reward_asset), (target.asset_path, target.object))
                self.assertEqual((reopened[0].count, reopened[0].notification), (slot.count, slot.notification))
        self.assertEqual({path.suffix: hashlib.sha256(path.read_bytes()).hexdigest() for path in (SOURCE, SOURCE.with_suffix(".uexp"))}, source_hashes)


if __name__ == "__main__":
    unittest.main()
