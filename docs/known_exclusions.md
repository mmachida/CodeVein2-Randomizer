# Known Randomizer Exclusions

This file records content that is currently kept out of randomization. It documents scope; it does not add or change randomizer behavior. See [rules.md](rules.md) for the underlying shop-stock and protection rules.

## Shop inventory kept vanilla

- **Infinite-stock shop entries:** kept vanilla and excluded as both sources and destinations. The randomizer does not change their stock or convert them to finite stock.
- **Luxury shop:** the `LUXURY` family row `Shop_MM_Special1` in `/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_Luxury` sells repeatable Luxury items and stays outside the pool. The current inventory report lists 35 entries as `UNSUPPORTED` with `UNKNOWN_SHOP_STOCK_SEMANTICS`.
- **Pre-order weapons:** the two entries `DA_SingleSword_JutoMasamune_DLC` and `DA_DualSword_Antique_DLC` in `NORMAL / Shop_Jadwiga_Weapon` stay outside the shop pool. Their current inventory status is `UNSUPPORTED` (`UNKNOWN_SHOP_STOCK_SEMANTICS`). There is no active option that removes or edits them. This note applies to those shop entries; it does not establish a global exclusion for every other physical occurrence of a similarly named weapon.
- **DLC repeatable supplies:** row `Shop_DLC_Normal1` in `/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_DLC` has seven repeatable entries that stay outside the pool:
  - Ammunition: `DA_Item_AmmoGrenade`, `DA_Item_AmmoRifle`, `DA_Item_AmmoShotgun`.
  - Upgrade materials: `DA_Item_DLC_MatLevelUpJailB_01`, `DA_Item_DLC_MatLevelUpSAGadgetB_01`, `DA_Item_DLC_MatLevelUpWeaponC_01`, `DA_Item_DLC_MatLevelUpWeaponD_01`.

The current inventory report marks those seven DLC entries `UNSUPPORTED` with `UNKNOWN_SHOP_STOCK_SEMANTICS`.

## Key and quest items

Exact Key and quest/progression identities in [`important_item_exclusions.json`](../data/reward_pools/current/important_item_exclusions.json) remain protected from randomization. The list is still being audited and is not yet a complete inventory of every Key or quest item. Do not infer exclusions from item names alone; add newly confirmed identities to the authoritative list as they are reviewed. In particular, `DA_Item_Important_` is not a blanket protection rule: recipe Articles with that prefix remain eligible wherever their locations are supported by a randomizer pool.

- **Metagen / Glutton’s Eyes:** Lise’s point exchange provides the Rune Blade at `/Game/Blueprint/Article/Weapon/RuneBlade/DA_RuneBlade_OnihimeRuneBlade_Lise`. A targeted inspection of the installed Josee quest packages found that `/Game/Quest/MainEpisode_Josee/QST_MainEpisode_Josee_e20520` awards two distinct Articles: `DA_RuneBlade_OnihimeRuneBlade_1` (the weapon) and `/Game/Blueprint/Article/Item/DA_Item_Important_Josee_RuneBlade` (the story item). That quest package contains an `EnemyDefeat` event and the Sunken City boss actor `BP_BA_01_0101GiantNoLegs_Fist_Elite_N01_SC_01_Boss_C`, matching the reported encounter. The story Article is now an exact quest exclusion; it is outside the usual `/Item/Important/` folder, so the policy also accepts explicitly named `DA_Item_Important_*` Articles. The Lise shop weapon remains a separate weapon identity and stays eligible. The earlier check of `QST_MainEpisode_Josee_e20100_Boss` was simply the wrong Josee quest stage. The audit also records `/Game/Blueprint/Article/Item/Important/DA_Item_Important_GluttonyMask` for Lyle’s Gluttony Mask quest, while `DA_Item_EnMatGluttonyMask_*` are distinct enemy-drop crafting materials.

- **Lise’s Hair Ornament:** the exact Article `/Game/Blueprint/Article/Item/DA_Important_LiseHairOrnament_Name` is granted by `QST_MainEpisode_Josee_GainLiseHairOrnament` and also appears as a ground pickup. It is now protected by its exact quest identity so the pickup cannot bypass the quest protection. This does not create a general `DA_Important_` name rule.

- **Zenon’s passports:** `DA_Important_ZenonPassport` and `DA_Important_ZenonPassport_Upgraded` are direct main-story quest grants. Their names do not use the `DA_Item_Important_` prefix, but they remain protected because they are story progression items.

- **Alternate Bequeathed reward routes:** `DA_ExecutionSword` is registered in logical reward group `BEQUEATHED_EXECUTIONER`. The canonical randomized World Loot slot is `bequeathed_executioner_lyle_quest`; the grant in `QST_MainEpisode_Lyle_e00560` is the alternate route for the same logical item. Both the route row and the direct-grant audit row are excluded as mirrors in the inventory report, not counted as extra reward candidates.

- **Important-folder icon candidates:** the UI icon folder is mixed content, not a quest classification. Exact quest/progression Articles confirmed by the current policy include Music Box, Camilo Mask A/B, Bridge/Motorcycle/Time Travel spells, Spice 01–03, Engraved Weapon 01–03, and Rift Book 01–04. The enemy quest audit separately confirms Lyle’s Envy/Gluttony/Lust/Sloth/Wrath masks and Iris’s Vampire Factors I–III, among other quest rewards. Reports A/B/C remain included pending classification. Structurally unsupported branch locations for protected Articles remain excluded from randomization. Do not apply one rule to everything merely under `Item/Important`.

## Inventory report

The current report is [`randomizer_item_inventory.csv`](../workspace/randomizer_inventory_summary/randomizer_item_inventory.csv). Shop rows described above are outside the active pool; check `status` and `exclusion_reason` there for the current catalog classification. Quest-grant audit rows marked `UNSUPPORTED` are not automatically protected: they have no registered destination patch yet. Check other rows for the same exact Article to see whether another supported occurrence is already randomized. Hot Spring tasks now have registered World Loot destinations: all 19 recipe reward slots across 10 physical Hot Springs (including one DLC Hot Spring) are included as complete task bundles. Recipe Articles are not protected merely because their names start with `DA_Item_Important_`.
