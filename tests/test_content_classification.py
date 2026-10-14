from __future__ import annotations

import unittest
from pathlib import Path

from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.content_classification import (
    build_explicit_article_class_index,
    classify_package_content,
    classify_shop_where,
)


ROOT = Path(__file__).resolve().parents[1]


class ContentClassificationTests(unittest.TestCase):
    # Check package container classification use exact
    def test_package_container_classification_uses_exact_ownership(self) -> None:
        inventory = PackageInventory.create(
            ("/Game/Map/A",),
            ("pakchunk0-Windows.utoc",),
            (("/Game/Map/A", "pakchunk0-Windows.utoc"),),
        )
        unresolved = classify_package_content("/Game/Map/A", inventory)
        self.assertEqual(unresolved.content_class, "UNKNOWN")
        self.assertEqual(unresolved.source_containers, ("pakchunk0-Windows.utoc",))
        self.assertEqual(unresolved.audit_status, "EXHAUSTIVE_UNRESOLVED")

        classified = classify_package_content(
            "/Game/Map/A",
            inventory,
            container_classes={"pakchunk0-Windows.utoc": "BASE"},
        )
        self.assertEqual(classified.content_class, "BASE")
        self.assertEqual(classified.classification_source, "PACKAGE_CONTAINER")

    # Check exact package override precedes shared
    def test_exact_package_override_precedes_shared_container_provenance(self) -> None:
        inventory = PackageInventory.create(
            ("/Game/Quest/ExactDlcQuest",),
            ("pakchunk0-Windows.utoc",),
            (("/Game/Quest/ExactDlcQuest", "pakchunk0-Windows.utoc"),),
        )
        result = classify_package_content(
            "/Game/Quest/ExactDlcQuest",
            inventory,
            package_overrides={"/game/quest/exactdlcquest": ("DLC", "reviewed FieldEnemy catalog")},
            container_classes={"pakchunk0-Windows.utoc": "BASE"},
        )
        self.assertEqual(result.content_class, "DLC")
        self.assertEqual(result.classification_source, "VERSION_OVERRIDE")

    # Check article classes require exact identity
    def test_article_classes_require_exact_identity_from_reviewed_catalogs(self) -> None:
        index = build_explicit_article_class_index(ROOT / "data")
        self.assertFalse(index.conflicts)
        self.assertEqual(
            index.get("/Game/Blueprint/Article/Gadget/StandAlone/DA_IaiKatana").content_class,
            "BASE",
        )
        self.assertEqual(
            index.get("/Game/Blueprint/Article/BloodCode/DA_BloodCode_BossZenon").content_class,
            "BASE",
        )
        self.assertEqual(
            index.get("/Game/Blueprint/Article/BloodCode/DA_BloodCode_Noah_01").content_class,
            "BASE",
        )
        self.assertEqual(
            index.get("/Game/Blueprint/Article/BloodCode/DA_BloodCode_GuardianHeart").content_class,
            "BASE",
        )
        self.assertEqual(len(index.entries), 223)
        self.assertIsNone(index.get("/Game/Blueprint/Article/Item/Support/DA_Fake_DLC"))

    # Check shop where use reviewed table
    def test_shop_where_uses_only_the_reviewed_table_family(self) -> None:
        self.assertEqual(classify_shop_where("NORMAL").content_class, "BASE")
        self.assertEqual(classify_shop_where("TRADE_DLC").content_class, "DLC")
        self.assertEqual(classify_shop_where("UNREVIEWED").content_class, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
