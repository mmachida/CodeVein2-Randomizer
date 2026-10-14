# Unique reward mapping audit

Date: 2026-10-08  
Scope: current World Loot, Shop/Trader, Blood Code, Booster, Bequeathed Forma, Defensive Gadget, weapon, and Jail catalogs; linked-route handling; and unique Article identity multiplicity.

## How the mapping avoids copies

Randomization is based on physical source occurrences, not just Article names. Every active source occurrence is assigned once; World/Shop mixing verifies that the source occurrence set and Article multiset are conserved. Initial Inventory and Extractor transfers exchange one source for one returned source. Map packages marked with `mirror_of_package_path` are serialized mirrors of a canonical location, so they receive the same output but do not add another source or spoiler row.

The Stinger has one additional alternate route: the Sunken City pickup and Josee's finite shop row share the same one-time reward flag. The shop row is excluded from the independent Shop pool and follows the final Article assigned to the canonical pickup. Its DLC map mirror follows that same assignment. The DLC `DA_Jail_Stinger_Pathos` is a separate Article identity.

The audit found that this special-case logic had not been backed by a general fail-closed check for the other unique Jail source maps. `JailCatalog.validate_world_sources()` now requires exactly one canonical World Loot occurrence at the audited slot for each world-sourced Jail, and zero for the starting Ogre. It also verifies that each Jail map mirror points to that exact source and preserves the Article, Count, and notification fields. A second unlinked map copy now stops catalog validation before Generate can randomize it.

Generate also audits finite Jail Shop rows. Each row must match an explicitly reviewed article and product slot; an added Jail row, duplicate row, moved row, or changed finite-stock shape fails closed. Josee's Stinger row mirrors the canonical World reward. Jadwiga's Ogre row mirrors the Initial Inventory Jail: `DA_Jail_Ogre` declares `StoryFlag = Gain_Jail_Ogre`, and the shop product references that same Article. Both rows are excluded from independent Shop/World source pools; Jadwiga's row follows the randomized starting Jail Article when Initial Inventory changes it. This prevents either alternate route from being counted as another unique Jail occurrence.

## Current Jail source inventory

| Jail identity | Canonical World source | World map mirrors | finite Shop route | handling |
|---|---:|---:|---|---|
| Bat, Hounds, Ivy, Reaper | 1 each | 1 each | none | one source, one mirror |
| Stinger | 1 | 1 | `NORMAL / Shop_SC_Normal1 / 20` | linked alternate; excluded from Shop pool |
| Bat Pathos, Hounds Pathos, Ivy Pathos, Ogre Pathos, Reaper Pathos, Stinger Pathos | 1 each | 1 each | none | one source, one mirror; each Pathos Article is distinct |
| Ogre | none; starting inventory | none | `NORMAL / Shop_Jadwiga_Weapon / 29` | linked starting-Jail mirror; excluded from Shop pool |

The scan covered 5,983 World Loot catalog entries and 855 Shop product slots. All 11 World-sourced Jail identities have one canonical occurrence and one declared map mirror. No other Jail identity appears in the Shop catalog. The exact paths and slots are verified from the runtime catalogs, not inferred from display names.

## Ogre caveat

The catalog contains `DA_Jail_Ogre` in `DA_InitialInventory_Player` and in a finite Jadwiga Shop row (price 100, stock 1, no evaluation formula). Parsing the installed Article asset found `StoryFlag = Gain_Jail_Ogre`; the shop product points to that same Article identity. The randomizer therefore treats the shop row as a linked mirror of the starting Jail, not an independent source. If Initial Inventory is randomized, Jadwiga's Article follows the assigned starting Jail so both paths retain the same unique-unlock identity. The game-side shop repeat-suppression behavior has not been directly tested; the asset evidence supports the shared Article/StoryFlag mapping, while an in-game check remains useful to confirm how the UI handles an already-owned Jail.

The same extraction pass found distinct Article StoryFlags for all 12 Jail identities: base `Gain_Jail_Bat`, `Gain_Jail_Hounds`, `Gain_Jail_Ivy`, `Gain_Jail_Ogre`, `Gain_Jail_Reaper`, `Gain_Jail_Stinger`; Pathos `GainJailBatPathos`, `GainJailHoundsPathos`, `GainJailIvyPathos`, `GainJailOgrePathos`, `GainJailReaperPathos`, `GainJailStingerPathos`. Thus base Stinger and Stinger Pathos are separate identities, as are all other base/Pathos pairs.

## Other unique pools reviewed

- **Blood Codes:** `DA_BloodCode_LouDisp_02` appears in a DLC `GainItem` export and Lou Disp past-scenario exports. These are alternate routes for the same reward identity, with the `_Yadorigi` export mirroring the scenario route. The catalog now represents one logical destination, patches the quest plus both scenario exports with one assignment, and emits one spoiler assignment. The 79-slot pool has 79 unique Article identities; the catalog loader and planner reject duplicate source identities.
- **Bequeathed Formae:** all 10 current identities have one canonical source registration. `DA_Bow` has one declared map mirror. Absolute Executioner (`DA_ExecutionSword`) has two Lyle quest grant paths but one `logical_reward_group`; the planner makes one assignment and the serializer patches both paths to that assignment. `DA_LouAxe` has one direct quest grant; the other seven identities each have one catalogued finite Shop row. No Forma Article is independently registered in both the World and Shop pools. The alternative Lyle quest paths are treated as one logical source; their mutual-exclusion behavior has not been separately confirmed in gameplay.
- **Weapons:** the current catalogs contain 34 canonical World Loot weapon occurrences and 85 weapon Shop/Trader product slots, all with distinct Article paths across those two pools. Two of the shop slots have zero stock and stay vanilla; the other 83 are finite positive-stock sources. No Article path is repeated within the weapon World sources or Shop/Trader products, and none overlaps between the two systems. The NPC Trader rows are already part of the unified Shop catalog and were not double-counted as another source.
- **Boosters:** the 102 mastery reward rows each have a distinct reward identity bundle (Article plus StoryFlag). The current source catalogs also contain 43 distinct Extractor recipe outputs and 10 distinct physical World Loot Booster sources. These three sets contain 155 Article identities total and have no exact Article-path overlap. The three Iris Booster rewards are already logical World Loot destinations; mirrored exports 39/40 of the c00070 package are serialized mirrors, not extra pool sources.
- **Defensive Gadgets:** the 17 Article identities in the gadget catalog are distinct. Ten currently catalogued World destinations each have one canonical physical occurrence; nine have a declared map mirror. Jadwiga's initial Parry grant is the same one World Loot destination, not a second NPC-pool source. The additional Lise Warp quest package still has no structurally confirmed reward field and is not randomized by that route, so it cannot currently create a cloned randomized reward.
- **Weapons/Initial Inventory and Extractor transfers:** Initial Inventory and Extractor use source-occurrence exchanges: the selected physical source is removed and the displaced vanilla reward is returned to that source slot. Existing regression tests cover this balancing behavior. Jails are detailed above; the known Stinger and Ogre alternate routes are linked and excluded from independent source counts.

This audit does not claim that every unresearched quest, shop, or version-specific route has been proven to be a single-copy source. New unique source routes must be added to an explicit owner/link policy before they can enter the randomization pool.

## Validation

Regression tests cover a second unlinked Hounds map source, a mirror pointing to the wrong source, linked Ogre/Stinger Shop routes, the Ogre shop mirror following Initial Inventory, an unreviewed Jail Shop copy, and a moved finite Shop row. Blood Code tests validate unique source identities and patch one Lou Disp assignment into the GainItem and both scenario routes. Runtime behavior of the linked Lou Disp paths remains for gameplay confirmation; the focused catalog, planner, serializer, inventory, configuration, and pipeline checks pass.
