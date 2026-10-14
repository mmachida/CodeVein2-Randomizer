from __future__ import annotations

import unittest
from types import SimpleNamespace as NS

from cv2randomizer.generation.pipeline import _world_loot_candidate_fname_values
from cv2randomizer.game.content_availability import PackageInventory


# List reward
def _reward(path: str) -> NS:
    return NS(article_path=path)


# Build a test location record
def _location(
    path: str,
    *,
    package: str = "/Game/Map/Cell_A",
    source_file: str = "Cell_A.umap",
    protected: bool = False,
    protection_reasons: tuple[str, ...] = (),
    location_type: str = "NORMAL_WORLD_LOOT",
    destination_kind: str = "ARTICLE_BULK",
) -> NS:
    return NS(
        package_path=package,
        source_file=source_file,
        protected=protected,
        protection_reasons=protection_reasons,
        location_type=location_type,
        destination_kind=destination_kind,
        rewards=(_reward(path),),
    )


class WorldLootCandidateNameTests(unittest.TestCase):
    # Check candidate universe include legal cross
    def test_candidate_universe_includes_legal_cross_pool_rewards_and_excludes_protected_content(self) -> None:
        catalogs = NS(
            unsafe_packages=("/Game/Map/UnsafeCell",),
            important_item_exclusions=NS(
                key_identities=("/Game/Blueprint/Article/Item/DA_Key",),
                quest_flag_identities=("/Game/Blueprint/Article/Item/DA_QuestFlag",),
            ),
            world_loot=(
                _location("/Game/Blueprint/Article/Item/DA_WorldReward_1"),
                _location("/Game/Blueprint/Article/Item/DA_Protected", protected=True),
                _location(
                    "/Game/Blueprint/Article/Item/DA_ReasonProtected",
                    protection_reasons=("SCRIPTED",),
                ),
                _location(
                    "/Game/Blueprint/Article/Item/DA_Renewable",
                    location_type="RENEWABLE_INGREDIENT",
                ),
                _location(
                    "/Game/Blueprint/Article/Item/DA_Unsafe",
                    package="/Game/Map/UnsafeCell",
                ),
                _location(
                    "/Game/Blueprint/Article/Item/DA_Unsupported", destination_kind="RAW_EXPORT",
                ),
                _location(
                    "/Game/Blueprint/Article/Item/DA_DirectQuestReward",
                    destination_kind="QUEST_ARTICLE_GRANT",
                ),
                _location(
                    "/Game/Blueprint/Article/Item/DA_DirectRawReward",
                    destination_kind="RAW_ARTICLE_GRANT",
                ),
                _location("/Game/Blueprint/Article/Item/DA_Key"),
                _location("/Game/Blueprint/Article/Item/DA_QuestFlag"),
                _location("/Game/Blueprint/Article/BloodCode/DA_BloodCode_Noah_01"),
            ),
            shops=(
                NS(original_product_path="/Game/Blueprint/Article/Item/DA_FiniteShop", protected=False,
                   uses_stock=True, stock=1),
                NS(original_product_path="/Game/Blueprint/Article/Item/DA_InfiniteShop", protected=False,
                   uses_stock=False, stock=None),
                NS(original_product_path="/Game/Blueprint/Article/Item/DA_ProtectedShop", protected=True,
                   uses_stock=True, stock=1),
            ),
        )
        field_enemies = NS(locations=(
            NS(protected=False, protection_reasons=(), rewards=(_reward("/Game/Blueprint/Article/Item/DA_BossReward"),)),
            NS(protected=True, protection_reasons=("KEY",), rewards=(_reward("/Game/Blueprint/Article/Item/DA_BossKey"),)),
        ))
        canonical = NS(world_reward_items=(NS(asset_path="/Game/Blueprint/Article/Item/DA_Canonical"),))
        gadgets = NS(items=(
            NS(world_reward_eligible=True, asset_path="/Game/Blueprint/Article/Gadget/Defence/DA_Gadget"),
            NS(world_reward_eligible=False, asset_path="/Game/Blueprint/Article/Gadget/Defence/DA_ProtectedGadget"),
        ))
        jails = NS(items=(NS(world_reward_eligible=True, asset_path="/Game/Blueprint/Article/Jail/DA_Jail"),))
        formae = NS(items=(NS(world_reward_eligible=True, asset_path="/Game/Blueprint/Article/Gadget/DA_Forma"),))
        extractor = NS(slots=(NS(original_article_path="/Game/Blueprint/Article/Item/DA_RecipeOutput"),))

        values = set(_world_loot_candidate_fname_values(
            catalogs,
            field_enemy_catalog=field_enemies,
            canonical_article_catalog=canonical,
            extractor_catalog=extractor,
            defensive_gadget_catalog=gadgets,
            jail_catalog=jails,
            bequeathed_formae_catalog=formae,
        ))

        for included in (
            "/Game/Blueprint/Article/Item/DA_WorldReward_1",
            "/Game/Blueprint/Article/Item/DA_WorldReward",
            "DA_WorldReward_1",
            "DA_WorldReward",
            "/Game/Blueprint/Article/Item/DA_FiniteShop",
            "/Game/Blueprint/Article/Item/DA_BossReward",
            "/Game/Blueprint/Article/Item/DA_Canonical",
            "/Game/Blueprint/Article/Gadget/Defence/DA_Gadget",
            "/Game/Blueprint/Article/Jail/DA_Jail",
            "/Game/Blueprint/Article/Gadget/DA_Forma",
            "/Game/Blueprint/Article/Item/DA_RecipeOutput",
            "/Game/Blueprint/Article/Item/DA_DirectQuestReward",
            "/Game/Blueprint/Article/Item/DA_DirectRawReward",
        ):
            self.assertIn(included, values)
        for excluded in (
            "DA_Protected",
            "DA_ReasonProtected",
            "DA_Renewable",
            "DA_Unsafe",
            "DA_Unsupported",
            "DA_Key",
            "DA_QuestFlag",
            "DA_BloodCode_Noah_01",
            "DA_InfiniteShop",
            "DA_ProtectedShop",
            "DA_BossKey",
            "DA_ProtectedGadget",
        ):
            self.assertNotIn(excluded, values)

    # Check finite shop candidates keep existing
    def test_finite_shop_candidates_keep_existing_names_and_unique_numbered_spelling(self) -> None:
        base_path = "/Game/Blueprint/Article/Item/DA_NumberedFiniteShop"
        catalogs = NS(
            unsafe_packages=(),
            important_item_exclusions=None,
            world_loot=(),
            shops=(
                NS(original_product_path="/Game/Blueprint/Article/Item/DA_AmbiguousFiniteShop",
                   protected=False, uses_stock=True, stock=1),
                NS(original_product_path=base_path, protected=False, uses_stock=True, stock=1),
            ),
        )
        inventory = PackageInventory.create((base_path + "_1",))

        values = set(_world_loot_candidate_fname_values(catalogs, installed_inventory=inventory))

        # The planner already treats finite products as legal candidates. Keep
        # their exact names even if their base package is absent/ambiguous, and
        # include a uniquely resolved installed spelling for FName suffix forms.
        self.assertIn("/Game/Blueprint/Article/Item/DA_AmbiguousFiniteShop", values)
        self.assertIn(base_path, values)
        self.assertIn(base_path + "_1", values)
        self.assertIn("DA_NumberedFiniteShop_1", values)


if __name__ == "__main__":
    unittest.main()
