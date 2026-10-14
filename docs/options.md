# Randomizer option contract

This document defines the behavior of the user-controlled checkboxes on the
Item Randomizer tab. `Randomize World Loot` is not a user option: ordinary World
Loot is the always-on baseline. A setting's default below is its value for a
new profile.

| UI option | Config field | Default | ON | OFF | Dependencies and pool boundaries |
|---|---|---:|---|---|---|
| Randomize Initial Inventory | `randomize_initial_inventory` | Off | Replaces the starting Weapon, Defensive Gadget, and Jail in `DA_InitialInventory_Player`. | Those three slots stay vanilla. | Uses three category-specific pools and reservation rules. Lou_01 Blood Code, BodyLight, and BasicRecovery stay vanilla in either state. |
| Randomize Blood Codes | `randomize_blood_codes` | Off | Permutes the 80 confirmed Blood Code reward locations among Blood Codes. | Blood Code grants stay vanilla and create no Blood Code assignments. | Blood Code rewards stay in a Blood Code-only pool. Lou_01 is outside the pool. |
| Randomize Boosters | `randomize_boosters` | Off | Permutes the 102 Blood Code Mastery Booster reward rows. | The Mastery table stays vanilla. | This option does not add Mastery assignments to World Loot. When Booster Crafting is ON, Mastery Booster identities remain eligible crafting candidates under the separate canonical-pool rule; that does not patch the Mastery table. |
| Randomize Booster Extractor | `randomize_extractor_recipes` | Off | Shuffles up to 43 supported Extractor recipe outputs, preserving recipe costs, flags, and other row data. | Extractor recipes stay vanilla and no Extractor identities are reserved. | Independent of Randomize Boosters. Its candidate pool is deduplicated by Article path and includes available Mastery, recipe, canonical Article, and active World/Shop Booster identities. Unallocated identities join World Loot only while this option is ON. |
| Randomize Shops and NPC Traders | `randomize_shops` | On | Shuffles eligible finite-stock Shop and NPC Trader slots. | Shop and Trader slots stay vanilla and are absent from Shop/World mixing. | Infinite, uncertain, protected, and Magmell Tool Shop slots stay vanilla and cannot be sources or destinations. Prices, stock, unlock conditions, and slot metadata stay with the destination. Affinity thresholds change only if the separate NPC Trader affinity option is enabled. NPC Traders are controlled by this same field. |
| Mix Shop rewards with World Pool | `mix_shops_with_world` | Off | Allows eligible Shop/Trader Article identities and flexible World rewards to cross between those destinations. | Shop and World pools remain separate; Shop and Trader pools follow their Shop-side rules. | Effective only while Shops is ON. If Shops is OFF, the model clamps effective mixing to false, even if the UI checkbox remains visually checked for the current session. The effective false value is what settings save. |
| Unlock all NPC Trader Shop Tiers | `unlock_npc_trader_shops` | Off | Makes every affinity-gated tier in available NPC Trader tables available from the start by lowering its verified affinity threshold to zero. | Original NPC affinity requirements remain unchanged. | In Miscellaneous; independent of reward randomization and Shop/World mixing. Current base-game and Liv trader tables use affinity tiers 8 through 72. This patches only `TRADE` / `TRADE_DLC` Buddy rows; the Magmell Tool Shop is untouched. |
| Randomize Enemy Drops | `randomize_enemy_drops` | Off | Shuffles enemy drop bundles, including rows shared by regular enemies and bosses, and enables confirmed one-time enemy Article locations in the always-on World Loot pool. | Enemy Drop tables and one-time enemy locations stay vanilla. | Shared rows affect both their regular-enemy and boss/miniboss encounters. Unresolved rows and protected/key/progression rewards remain unchanged. The audited one-time group includes 15 boss/miniboss quest grants, 24 FieldEnemy grants, and five Harmony Moon rewards. |
| Randomize Healing Items | `randomize_healing` | On | Adds 46 Golden Nectars and 12 Golden Blood Beads physical locations to the shared World Loot pool. | Those 58 physical locations stay vanilla and are excluded as both World Loot sources and destinations. | This controls locations, not global Article identities: an independently occurring item may still be used by another active system. |
| Randomize Renewable Ingredients | `randomize_materials` | Off | Globally shuffles validated respawning pickup types. Every pickup of a type gets the same replacement, including mirrored map copies. | Renewable pickups stay vanilla. | In Ingredients; separate from World Loot. Extract validates each class hierarchy and reset behavior; unsupported classes stay vanilla. |
| Unlock 6 Booster Slots | `unlock_all_booster_slots` | Off | Starts new games with Booster slots 5 and 6 unlocked by setting their StoryFlag defaults. | Booster slots keep their vanilla unlock progression. | In Miscellaneous. Both flags have `IsInitToKeepValue=True`, so existing saves keep their stored values. Uses the optional cached `DT_StoryFlagDefine_GameSystem` source. |
| Unlock Motorcycle at Start | `motorcycle_unlocked` | Off | Adds the Motorcycle Forma to new-game inventory, enables its StoryFlag, and removes the late quest gate for the motorcycle icon/action. | Motorcycle access stays gated by the normal story progression. | In Miscellaneous. Existing saves are unchanged; can be combined with Initial Inventory randomization. |
| Unlock Second Weapon Slot | `second_weapon_unlocked` | Off | Starts new games with the second weapon slot unlocked by setting `UnlockEquipSlot_Weapon2` true. | The slot stays locked until its normal Jail +11 unlock. | In Miscellaneous. This is the same persistent StoryFlag used by the Jail +11 upgrade; existing saves retain their stored value and the shared flag table is served from the Extract cache. |
| Cheaper Booster Extractor (only 1x of each material) | `cheaper_booster_extractor` | Off | Sets each required material quantity in available Booster Extractor recipes to 1. | Recipe material quantities stay vanilla. | In Miscellaneous; Haze costs and other recipe data stay unchanged. Generate reads the recipe tables from the prepared cache. |
| Unrestricted Blood Code Mastery | `low_blood_code_mastery_exp` | Off | Uses Lou 1's original mastery EXP requirement for every installed Blood Code and removes enemy-level restrictions. | Blood Code mastery requirements stay vanilla. | In Miscellaneous; applies to existing and new saves. Generate reads installed Blood Code Articles from the prepared cache. |

## Always-on and locked behavior

- Ordinary World Loot is randomized whenever the app generates. The hidden
  `randomize_world_loot` model field is forced on by settings migration and by
  the UI; only isolated internal tests can turn it off directly.
- The direct Bequeathed Forma quest-reward permutation is always enabled. Its
  two logical rewards (three physical grant packages) have no separate
  checkbox.
- The Warp Defensive Gadget NPC grant follows the always-on World Loot
  baseline.
- The Protection controls remain checked and disabled. Keys, selected quest
  items, scripted pickups, and unsafe/unparsed packages remain protected
  regardless of the other options. Renewable pickups leave the World Loot
  pool only when the separate Ingredients option is enabled.
- Booster Crafting reserves assigned Booster identities before the final World
  Loot plan. When both Booster Crafting and Enemy Drops are ON, the Lyle
  Harmony Moon quest slot stays vanilla if its Booster identity is reserved by
  a recipe, avoiding a duplicate source occurrence. The other 43 enemy-gated
  World destinations remain eligible. With Booster Crafting OFF, all 44
  curated enemy-gated destinations are eligible when Enemy Drops is ON.
- Missing optional content is skipped based on the installed package
  inventory. A local-unverified game build can generate locally but is not
  thereby an officially supported version.

## Evidence and remaining runtime limits

The planner and serializer tests cover the documented membership and metadata
rules. A local-unverified full Generate has produced and Retoc-verified the
audited Healing output, but gameplay of all 58 healing locations remains
unconfirmed. Previous gameplay checks confirmed the core World Loot and Shop
flows, several Healing swaps, and individual Booster/Blood Code features; those
checks do not prove every catalogued location or every game version.

## Reproducibility identity

Every successful generation records the randomizer algorithm version,
effective configuration fingerprint, game/content fingerprint, and canonical
assignment manifest SHA-256 in `assignment_manifest.json`, which is retained
alongside the app output after installation. Compare those values when
comparing seeds between users; matching seed text alone is not enough when
game/content fingerprints or effective settings differ. Paths, usernames,
timestamps, and cache roots do not affect these fingerprints. Container bytes
can vary with Retoc/container metadata; the assignment manifest is the
authoritative check for identical logical randomization. The installed
`~mods/Spoiler.txt` has a compact header with the seed, local creation date,
total assigned-slot and unchanged counts, and app version, followed by the
changed item replacements. It is not duplicated in the app output folder.
