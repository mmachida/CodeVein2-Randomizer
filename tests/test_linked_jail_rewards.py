from __future__ import annotations

import unittest
from types import SimpleNamespace

from cv2randomizer.randomizer.linked_jail_rewards import (
    JAIL_SHOP_ROUTES,
    OGRE_ARTICLE,
    STINGER_ARTICLE,
    STINGER_SHOP_TABLE_PACKAGE,
    STINGER_WORLD_EXPORT,
    STINGER_WORLD_PACKAGE,
    build_ogre_shop_mirror_assignment,
    build_stinger_shop_mirror_assignment,
    exclude_linked_jail_shop_routes,
    find_ogre_shop_product,
    find_stinger_shop_product,
    stinger_route_unavailable_reason,
    is_ogre_shop_slot,
    is_stinger_shop_slot,
    validate_jail_shop_routes,
)
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.randomizer.shops import ShopRandomizationPlan
from cv2randomizer.randomizer.source_occurrences import planned_reward_source_occurrences


class LinkedJailRewardTests(unittest.TestCase):
    # Find shop product
    def _shop_product(self, *, index: int = 20, article: str = STINGER_ARTICLE):
        return SimpleNamespace(
            family="NORMAL",
            shop_row="Shop_SC_Normal1",
            product_index=index,
            article_package_path=article,
            article_path=article,
            article_path_number=0,
            item_object=article.rsplit("/", 1)[-1],
            item_object_number=0,
            price=100,
            uses_stock=True,
            stock=1,
        )

    # Read ogre shop product
    def _ogre_shop_product(self, *, index: int = 29, article: str = OGRE_ARTICLE):
        return SimpleNamespace(
            family="NORMAL",
            shop_row="Shop_Jadwiga_Weapon",
            product_index=index,
            article_package_path=article,
            article_path=article,
            article_path_number=0,
            item_object=article.rsplit("/", 1)[-1],
            item_object_number=0,
            price=100,
            uses_stock=True,
            stock=1,
        )

    # Build world plan
    def _world_plan(self, assigned_article: str):
        reward = SimpleNamespace(
            article_path=assigned_article,
            content_class="BASE",
            count=1,
            notification=True,
            soft_object_path_identity=None,
        )
        destination = SimpleNamespace(
            package_path=STINGER_WORLD_PACKAGE,
            export_index=STINGER_WORLD_EXPORT,
            mirror_of_package_path=None,
        )
        candidate = SimpleNamespace(
            candidate_id="stinger-world-source",
            source_occurrence_ids=("world-stinger-occurrence",),
            source_location=None,
            rewards=(reward,),
        )
        assignment = SimpleNamespace(
            destination=destination,
            source_candidate=candidate,
            assigned_rewards=(reward,),
        )
        return SimpleNamespace(assignments=(assignment,))

    # Check audited josee shop slot is
    def test_only_audited_josee_shop_slot_is_linked(self) -> None:
        product = self._shop_product()
        ogre = self._ogre_shop_product()
        ordinary = self._shop_product(index=19, article="/Game/Blueprint/Article/Item/DA_Ordinary")
        self.assertTrue(is_stinger_shop_slot(product))
        self.assertIs(find_stinger_shop_product((product,)), product)
        self.assertFalse(is_stinger_shop_slot(self._shop_product(index=19)))
        self.assertTrue(is_ogre_shop_slot(ogre))
        self.assertIs(find_ogre_shop_product((ogre,)), ogre)
        self.assertFalse(is_ogre_shop_slot(self._ogre_shop_product(index=28)))
        self.assertEqual(exclude_linked_jail_shop_routes((product, ogre, ordinary)), (ordinary,))
        self.assertIsNone(find_stinger_shop_product((self._shop_product(index=19),)))

    # Check missing stinger route content disables
    def test_missing_stinger_route_content_only_disables_that_pickup(self) -> None:
        product = self._shop_product()
        ready_inventory = PackageInventory.create((STINGER_ARTICLE, STINGER_SHOP_TABLE_PACKAGE))
        self.assertIsNone(stinger_route_unavailable_reason(product, ready_inventory))
        self.assertEqual(
            stinger_route_unavailable_reason(product, PackageInventory.create((STINGER_SHOP_TABLE_PACKAGE,))),
            "the Stinger Article is absent",
        )
        self.assertEqual(
            stinger_route_unavailable_reason(product, PackageInventory.create((STINGER_ARTICLE,))),
            "the base Shop table is absent",
        )
        self.assertEqual(
            stinger_route_unavailable_reason(None, ready_inventory),
            "the audited shop alternate is not catalogued",
        )

    # Check linked shop route mirrors final
    def test_linked_shop_route_mirrors_final_outpost_assignment(self) -> None:
        product = self._shop_product()
        assigned = "/Game/Blueprint/Article/Jail/DA_Jail_Bat"
        linked = build_stinger_shop_mirror_assignment(self._world_plan(assigned), product)

        self.assertIsNotNone(linked)
        self.assertTrue(linked.article_only)
        self.assertEqual(linked.destination, product)
        self.assertEqual(linked.source.canonical_path, assigned)

    # Check vanilla stinger result does not
    def test_vanilla_stinger_result_does_not_create_a_second_assignment(self) -> None:
        linked = build_stinger_shop_mirror_assignment(
            self._world_plan(STINGER_ARTICLE), self._shop_product(),
        )
        self.assertIsNone(linked)

    # Check ogre shop row mirrors randomized
    def test_ogre_shop_row_mirrors_the_randomized_starting_jail(self) -> None:
        product = self._ogre_shop_product()
        assigned = "/Game/Blueprint/Article/Jail/DA_Jail_Hounds"
        linked = build_ogre_shop_mirror_assignment(assigned, product)

        self.assertIsNotNone(linked)
        self.assertTrue(linked.article_only)
        self.assertEqual(linked.destination, product)
        self.assertEqual(linked.source.canonical_path, assigned)
        self.assertEqual(linked.source.origin_kind, "LINKED_INITIAL_INVENTORY")
        self.assertEqual(linked.source.content_class, "BASE")

        dlc_linked = build_ogre_shop_mirror_assignment(
            "/Game/Blueprint/Article/Jail/DLC/DA_Jail_Stinger_Pathos", product,
        )
        self.assertEqual(dlc_linked.source.content_class, "DLC")

    # Check vanilla starting ogre does not
    def test_vanilla_starting_ogre_does_not_create_a_second_assignment(self) -> None:
        self.assertIsNone(build_ogre_shop_mirror_assignment(OGRE_ARTICLE, self._ogre_shop_product()))

    # Check ogre shop mirror is not
    def test_ogre_shop_mirror_is_not_counted_as_a_second_source(self) -> None:
        product = self._ogre_shop_product()
        linked = build_ogre_shop_mirror_assignment(
            "/Game/Blueprint/Article/Jail/DA_Jail_Hounds", product,
        )
        shop_plan = ShopRandomizationPlan((), (), (), (), linked_assignments=(linked,))

        occurrences = planned_reward_source_occurrences(self._world_plan(STINGER_ARTICLE), shop_plan)
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].article_path, STINGER_ARTICLE)

    # Check ogre mirror reject unreviewed destination
    def test_ogre_mirror_rejects_unreviewed_destination_or_jail(self) -> None:
        with self.assertRaisesRegex(ValueError, "not the audited Jadwiga shop row"):
            build_ogre_shop_mirror_assignment(
                "/Game/Blueprint/Article/Jail/DA_Jail_Hounds",
                self._ogre_shop_product(index=30),
            )
        with self.assertRaisesRegex(ValueError, "not a canonical player Jail"):
            build_ogre_shop_mirror_assignment(
                "/Game/Blueprint/Article/Item/DA_Item_AmmoRifle",
                self._ogre_shop_product(),
            )

    # Check linked alternate is not second
    def test_linked_alternate_is_not_a_second_source_occurrence(self) -> None:
        product = self._shop_product()
        linked = build_stinger_shop_mirror_assignment(
            self._world_plan("/Game/Blueprint/Article/Jail/DA_Jail_Bat"), product,
        )
        shop_plan = ShopRandomizationPlan(
            (), (), (), (), linked_assignments=(linked,),
        )

        occurrences = planned_reward_source_occurrences(self._world_plan(STINGER_ARTICLE), shop_plan)
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].article_path, STINGER_ARTICLE)

    # Check shop slot reject different vanilla
    def test_shop_slot_rejects_a_different_vanilla_article(self) -> None:
        with self.assertRaisesRegex(ValueError, "no longer contains DA_Jail_Stinger"):
            find_stinger_shop_product((self._shop_product(article="/Game/Blueprint/Article/Jail/DA_Jail_Stinger_Pathos"),))

    # Check current unique jail shop routes
    def test_current_unique_jail_shop_routes_are_explicitly_classified(self) -> None:
        ogre = SimpleNamespace(
            family="NORMAL",
            shop_row="Shop_Jadwiga_Weapon",
            product_index=29,
            original_product_path=OGRE_ARTICLE,
            uses_stock=True,
            stock=1,
        )
        routes = validate_jail_shop_routes((ogre, self._shop_product()))
        self.assertEqual(
            tuple(route.route_kind for route, _product in routes),
            ("linked_initial_inventory", "linked_alternate"),
        )
        self.assertEqual(len(JAIL_SHOP_ROUTES), 2)

    # Check unreviewed jail shop copy fail
    def test_unreviewed_jail_shop_copy_fails_closed(self) -> None:
        rogue = self._shop_product(
            index=21,
            article="/Game/Blueprint/Article/Jail/DA_Jail_Hounds",
        )
        with self.assertRaisesRegex(ValueError, "Unreviewed Shop route for unique Jail"):
            validate_jail_shop_routes((rogue,))

    # Check unique jail shop route cannot
    def test_unique_jail_shop_route_cannot_move_to_another_slot(self) -> None:
        rogue = SimpleNamespace(
            family="NORMAL",
            shop_row="Shop_Jadwiga_Weapon",
            product_index=30,
            original_product_path=OGRE_ARTICLE,
            uses_stock=True,
            stock=1,
        )
        with self.assertRaisesRegex(ValueError, "moved from audited slot"):
            validate_jail_shop_routes((rogue,))

    # Check ogre shop slot reject wrong
    def test_ogre_shop_slot_rejects_wrong_article_and_moved_slot(self) -> None:
        with self.assertRaisesRegex(ValueError, "no longer contains DA_Jail_Ogre"):
            find_ogre_shop_product((self._ogre_shop_product(article="/Game/Blueprint/Article/Jail/DA_Jail_Hounds"),))
        with self.assertRaisesRegex(ValueError, "moved from audited slot"):
            validate_jail_shop_routes((self._ogre_shop_product(index=30),))


if __name__ == "__main__":
    unittest.main()
