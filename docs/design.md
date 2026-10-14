# Code Vein 2 Randomizer — Design Document

## 1. Purpose

Code Vein 2 Randomizer is a Windows desktop application written primarily in Python.

The app generates deterministic Code Vein 2 randomizer mods from a user-provided seed. The user chooses which systems to randomize, the app reads the user's own installed game assets, applies safe deterministic changes, builds a mod override, and installs or outputs the generated mod.

The user should not need to manually open FModel, UAssetGUI, a hex editor, or the Code Vein 2 Modding Framework.

This document describes the current architecture and behavior, alongside requirements that remain future work. Blood Codes, mastery Boosters, Enemy Drops, World Loot, and Shops have each had their supported reward change confirmed in gameplay. The Bequeathed Forma catalog validates the one World source and seven Shop sources against their existing pools. Battle Axe: Idris's Conceit is a direct quest destination in always-on World Loot. Absolute Executioner is one World Loot logical reward with two alternative Lyle quest grant packages; both packages receive the same assignment so either route grants the same randomized item. Renewable ingredients have a separate optional, cache-backed randomization setting and remain excluded from World Loot; generated-package gameplay validation is pending. Golden Nectars and Golden Blood Beads have a dedicated `Randomize Healing Items` option, enabled by default: the current prepared cache contains 116 serialized map/export rows (92 Nectar and 24 Bead) across 116 maps, representing 58 unique physical pickups (46 Nectar and 12 Bead) mirrored between MainWorld and DlcWorld. Extract prepares their per-location Blueprint/CDO structures while preserving each vanilla reward; Generate uses rebuilt binary descriptors for changed rewards and preserves the vanilla locations when Healing is disabled. Two deterministic all-options Generates changed 110 mirrored Healing rows and retained six self-maps; complete-package gameplay validation remains pending. World Loot is always randomized in the app; the former master checkbox is removed. The extracted-data cache is reused across app updates when the installed game archives are unchanged; stale app-derived indexes are ignored and Generate uses its structural fallback. Defensive Gadgets are included in World Loot and Warp is an optional direct gadget grant. Jadwiga's three direct rewards, the DLC e90360 Gift Heal Point Up reward, five Harmony Moon rewards, three Iris Character Episode rewards, and all 10 Hot Spring quest tasks are generic World Loot Article destinations. The Hot Springs retain 19 serialized reward slots across 10 tasks: nine base-game springs have two slots each and the DLC spring has one. Each base-game pair can draw two independent Articles from the singleton World Loot source pool, including weapons and other item types. Recipe Articles are treated as unique unlocks: repeated Forest01–04 entries for Deadly Venom and Slow enter the pool once each, and their extra serialized slots receive ordinary non-recipe World Loot items. The DLC recipe participates once in the singleton pool and its destination is filtered out when its quest package is unavailable. All 10 Hot Spring `QSTask_GainItem_0` tasks have `bNoMultipleGain=True` in the game data, which suppresses rewards the player already owns. Generate now sets that flag to `False` on randomized Hot Spring tasks; a controlled MagMell test confirmed that already-owned, below-cap materials are then granted. Iris's direct swaps have been manually gameplay-tested; generated-package gameplay remains pending. Harmony Moon patches only `QSTask_GainItem_0`'s second Article path; its important first reward, multiplayer reward export, quest logic, and all other fields are snapshot-validated and preserved. The Harmony packages pass real-asset staged round trips, but generated-package gameplay remains pending. Compatible content from available BASE, DLC, AFTER_ENDING, or UNKNOWN sources participates automatically within its feature pool; Shop/World mixing remains a separate explicit option. A local-unverified build does not establish official game-version support.

---

## 2. Product goals

### Primary goals

1. Randomize open-world loot.
2. Randomize renewable gathering ingredients in a separate, compatible pickup pool.
3. Randomize shop inventories.
4. Randomize catalogued Blood Code reward slots.
5. Randomize confirmed mastery Booster rewards.
6. Randomize only confirmed normal-enemy drop rows.
7. Add Defensive Gadgets and player Jails to World Loot; randomize structurally confirmed direct gadget NPC grants.
8. Randomize Booster Extractor crafting rewards from the canonical Booster identity pool.
9. Include Battle Axe: Idris's Conceit and one logical Absolute Executioner reward in always-on World Loot; mirror the Executioner assignment to both alternative Lyle quest routes.
10. Keep story-critical, key, scripted, and otherwise protected content vanilla by default.
11. Produce deterministic results from a seed.
12. Generate a loadable Code Vein 2 mod automatically.
13. Never modify original game archives in place.
14. Make regeneration/removal of a seed simple.

### Non-goals for the initial version

- Enemy randomization.
- Boss randomization.
- General quest reward randomization beyond the explicitly catalogued Blood Code and Bequeathed Forma grants.
- Key-item logic randomization.
- Progression logic solving.
- Network/multiplayer behavior changes.
- Automatic update/release system.
- Public distribution packaging/installer work.

These can be future features but must not complicate V1.

---

## 3. Confirmed game behavior

The following structures were verified experimentally against the game.

### 3.1 Ground pickups

Placed ground pickups are stored inside generated world map packages.

Typical location:

`/Game/Map/MainWorld/MainWorld/_Generated_/HASH`

A placed pickup actor contains:

`ArticleBulkInfo -> Articles[] -> ArticleDataPath, Count, bNotification`

Example:

`/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight_1`

The location actor also contains a `UniqueSaveID` and placement information.

For ordinary world loot, the reward can be changed while preserving the actor/location/save identity.

The proven reward storage forms are: Most placed actors store `ArticleBulkInfo` directly in the map export. Remaining pickup Blueprints may inherit their default reward from a Blueprint CDO and are represented by `data/catalogs/current/world_loot_cdo_catalog.csv`; those ordinary destinations still patch the shared class default. A serialized prototype for Golden Seed and Golden Blood Bead locations clones the source Blueprint package per pickup, changes the clone CDO `ArticleDataPath`, and redirects the map actor's `ClassIndex` and `TemplateIndex`. Those references survive staged and final-container reopening, but they still require runtime confirmation. In the latest trace, the Randomizer was installed and an ordinary pickup was observed, while the Nectar and Bead map packages were absent from that container. The trace therefore did not test those two redirects. The current catalog/cache includes both maps; the next generated container must include them before their gameplay result can be interpreted. The shared source CDO and original Blueprint remain read-only.

The earlier local audit scanned 660 cached World Partition map packages and found 2,680 physical pickup actors: 1,736 direct map reward rows, 37 writable shared CDO groups covering 863 actors, 31 protected rows, one planner-locked row, and 81 RawExport actors in 8 groups whose per-instance protection or override could not be inspected. That audit expanded 825 renewable ingredient nodes and 38 healing locations. A later complete scan parsed all 35,074 generated MainWorld maps and found 58 physical healing pickups (46 Nectars and 12 Blood Beads); 18 were missing from the earlier audit and are now catalogued. The 2026-10-09 prepared-cache audit records 116 mirrored location/export rows (92 Nectar and 24 Bead) in 116 maps for those same 58 physical pickups. The earlier 2,680-row audit remains a scoped historical snapshot; do not describe it as a complete account of every installed map. The renewable set spans 35 Blueprint classes, 35 Articles, and 100 generated map packages. Those 81 opaque groups remain recorded in `data/catalogs/current/world_loot_unsupported_cdo_sources.csv` only to make their reward Articles available; they are not planner destinations and their shared CDOs are not edited. The audit found all 468 distinct physical reward Article references in both the installed IoStore and the fresh cache, without fuzzy suffix resolution. If an owner, class, or parent Blueprint is absent on another installation, that location is omitted from its eligible pool; an installed dependency extraction failure still fails safely.

Renewable nodes are classified by physical location and the real Blueprint ancestry, not by Article name. The class must belong to the `BP_PickupMaterial_*` hierarchy and have audited effective `bResetOnYadorigiRest = true`; the planner locks each physical node with `RENEWABLE_INGREDIENT_DISABLED`. Extract rechecks the installed class ancestry and stores the reader's reset observation separately from the catalogued physical audit. If the reader exposes the inherited/default property, an explicit `false` conflicts and fails validation; if the reader omits it, Extract may use the audited catalog value and records `catalogued_physical_audit` as its source. The current local cache directly observed `true` for 21 renewable classes and used that audited fallback for 14; all 35 classes are represented, with no observed conflicting value. Classification is location-based, so a one-time pickup containing the same Article remains eligible. Golden Seeds and Golden Blood Beads use two separately catalogued material-family classes and are healing locations, not renewable locations; each physical row retains its map/export identity, CDO source, Article FName, Count, and notification metadata.

Golden Nectars and Golden Blood Beads join the regular World Loot pool when `Randomize Healing Items` is enabled. The current catalog and READY cache contain 116 mirrored location/export rows across 116 maps (92 Nectar, 24 Bead) for 58 distinct physical pickups (46 Nectar and 12 Bead); each physical pickup has one deterministic assignment and an isolated Blueprint/CDO clone template. Turning the option off omits these physical pickups as both destinations and sources, preserving their vanilla rewards. Extract materializes the per-location structure, retains the vanilla ArticleDataPath, serializes/reopens the map and clone, and rebuilds descriptors against the prepared bytes. Generate verifies those descriptors and patches only the fixed-width reward FName in changed clones while staging the paired map; the READY templates stay immutable. Two all-options Generates changed 110 mirrored rows (55 physical pickups), retained six mirrored self-map rows (three physical pickups), binary-patched 110 clones and 110 Healing maps, and passed Retoc verification with 1,504 package paths. Two generic Treasure Box instance-override packages used the structural fallback; no changed Nectar/Bead pickup needed that fallback. These are structural and packaging checks only: in-game confirmation is still needed for prepared vanilla and randomized Nectar and Bead rewards. World Loot is a required baseline and has no user-facing master toggle. Current prepared-cache revision is 13; cache index format, descriptors, and preparation identity must be rebuilt together when their serialized layout changes.

In the earlier full Generate, 601 map packages contained catalogued instance rewards; 594 were staged because they contained changed eligible rewards, and 7 contained protected rewards only. The full healing scan added 18 physical instances after that build. Reward `Count` and notification fields stay attached to the destination (including the x2 Magmell pickup). Flag-associated physical pickups remain protected except for an explicit allowlist of 17 ordinary Article rewards, represented by 38 current MainWorld/DlcWorld map rows. Those assignments change only `ArticleBulkInfo.ArticleDataPath`; actor flags, save identity, Count, and notification remain at the destination. This includes `DA_Item_ExpA_09/10` on `BP_PickupItem_WithFlagChange_C`. The six mirrored `DA_Report_A/B/C` pickups use an exact Article-only scenario allowlist; only their reward path changes, while each scenario reference, save identity, Count, and notification stays with the destination. Exact Key/Pathos rewards and unsafe packages remain locked, as do other scenario pickups that lack an exact safe-patch validation.

The Magmell regression cases are ordinary `BP_it00_001_A_0_C` instance rewards, not CDO defaults. The importer normalizes this concrete pickup class to `GROUND_PICKUP`, allowing its three previously unresolved catalog rows to enter the ordinary World Loot planner while retaining their destination counts. Three deterministic dry-runs assigned all three; the full Generate patched, reopened, packaged, and documented them in `Spoiler.txt`. Prepared-cache coverage is now one descriptor per World Loot actor/direct grant, including individual renewable and healing locations. Cache class profiles bind the renewable/healing categories to the validated class ancestry and effective reset behavior. Generate checks the complete descriptor set against the active catalog before planning. The complete 2,680-actor CSV/JSON audit is kept under the ignored local `workspace/` directory.


#### Full installed-game pickup audit (2026-10-07)

The fresh installed IoStore sweep inventoried 216,218 packages, including 67,235 `.umap` packages (67,171 World Partition generated cells). Structural parsing covered 1,858 candidate-bearing map packages and yielded 7,741 pickup-like exports. These candidates match all 5,954 catalogued physical slots: 3,585 normal World Loot, 2,253 renewable nodes, and 116 Healing locations. Compared with the prior 660-map snapshot, the catalog recovered 1,849 normal slots: 1,622 eligible locations and 227 protected locations. The final normal pool has 3,322 eligible locations; each has a cached prepared descriptor and a seed assignment.

Classification is location-based. One-time pickups containing a material, ingredient, or upgrade Article remain eligible when the actor is outside the audited reset-on-rest `BP_PickupMaterial_*` families. The 58 physical Golden Nectar and Blood Bead pickups remain owned by `Healing` and enter the shared World Loot pool only when that option is enabled; they are represented by 116 mirrored map records. Heavy History Book is a normal `BP_it00_001_A_0_C` instance pickup with a valid `UniqueSaveID`; its previous `REVIEW / keyword:story` label had no structural progression evidence. The importer now ignores that keyword-only reason, so the book is no longer excluded by an Article-specific exception.

Three DLC treasure-box actors inherit `DA_Item_AmmoRifle` from `BP_it00_002_A_0`'s CDO instead of carrying a local `ArticleBulkInfo`. The validated generic per-instance override clones the source Blueprint/CDO reward for each actor, then redirects both `ClassIndex` and `TemplateIndex`. The source CDO is export index 3 (one-based UAssetAPI index); the source Blueprint remains unchanged. Count and notification stay attached to the destination slot. The corrected catalog supports ordinary ground and treasure-box destinations through this same patch strategy.

The audit records 1,263 exports that failed structural parsing as unsafe/unparsed, plus 254 component false positives, 239 rewardless map-feature actors, 25 rewardless base pickup actors, three rewardless `BP_it00_001_A_0` actors, and three actors from a user test map. All 1,787 non-catalog candidates have an explicit exclusion reason, so there are no unexplained differences among scanned candidate rows. Five exceptionally large map packages were skipped above the 512 MiB research-parser limit; their contents remain explicitly unparsed and are not claimed as verified. Ninety-six cached descriptors remain unprepared because their map packages are marked unsafe; these destinations stay protected.

The corrected source registry (`catalogued-sources-v19-full-world-ground-pickup-audit`), descriptor revision 10, and preparation revision 5 were validated by a fresh Extract. A full local-unverified Generate with seed `heavy-history-audit-20261007` completed with installation disabled. Heavy History Book and the three inherited CDO chests received non-self assignments; the 1,324-package Retoc container reopened and verified with `compression_methods: []`. The same seed reproduced the exact assignment-manifest hash, and two further plan-only seeds changed Heavy History Book and four distinct newly recovered locations. A follow-up Generate kept that same manifest hash. Spoiler output now lists only changed assignments, with mirrored map variants collapsed to their canonical logical destination. Gameplay validation of this final generated container remains pending; no game installation files were changed.

### 3.2 Treasure chests

Treasure chests use the same reward representation:

`ArticleBulkInfo -> Articles[] -> ArticleDataPath, Count, bNotification`

The chest actor and `UniqueSaveID` remain unchanged while the reward changes.

A real test was completed successfully:

- Original chest reward: `DA_Bayonet_Antique_1` (Ancient Hunter's Bayonet)
- Patched reward: `DA_Item_ChemicalLight_1`
- The generated mod was loaded by the game successfully.
- The chest gave the replacement item.

This validates the full world-loot patch -> package -> load path.

### 3.3 Renewable ingredients

The `Randomize Renewable Ingredients` option uses the full audited renewable-location set from `world_loot.json`, currently 2,253 map/export records across 49 structural resource classes and 47 reward Articles. It creates a separate Ingredients pool; these rows remain locked out of ordinary World Loot. The randomizer maps each original reward Article to one replacement Article globally, so every pickup of a given type changes consistently, including shared Articles owned by different Blueprints and mirrored map rows. Generate stages each affected reward-owner Blueprint and changes only the validated `ArticleBulkInfo.Articles[0].ArticleDataPath` on its shared CDO. It leaves placed map actors, their `ClassIndex`/`TemplateIndex`, save identities, and map `.umap`/`.uexp` payloads unchanged. This replaces the unsafe implementation that changed placed actor class/template references and caused the game's `Bad export index` save-load crash. Extract writes the validated class profiles to READY cache metadata. Generate reuses those profiles and stages only needed Blueprint dependencies from the active cache; incomplete older profile metadata falls back to validation through the cache extractor. A real cached Blueprint CDO round trip passed helper validation on a temporary copy; generated-package gameplay/load confirmation is still pending.

Renewable materials use a different structure.

Observed class hierarchy:

PickupableActor (native)
-> PickupMaterial (native)
-> BP_PickupMaterial_Base_C
-> optional material-specific Blueprint such as BP_PickupMaterial_ItemIngredient_C
-> resource-specific BP such as BP_it23_001_A_1_C
-> placed instance in generated map

Some resource classes inherit directly from BP_PickupMaterial_Base_C; others use a material subclass. The parser resolves the actual parent chain rather than assuming one fixed chain.

The resource-specific Blueprint class contains its reward in its class default object:

ArticleBulkInfo -> Articles[] -> ArticleDataPath

Example:

BP_it23_001_A_1_C -> DA_Item_IngredientHoney_1

BP_PickupMaterial_ItemIngredient_C has:

bResetOnYadorigiRest = true

The parser reads each resource Blueprint class default and walks its generated Blueprint ancestors to resolve the effective value. V1 only shuffles classes in the material hierarchy whose effective reset value is explicitly true; classes with absent or unknown values and non-material parents remain unchanged. This keeps reset behavior matched across the pool without inferring defaults.

The intended randomization shuffles the Article rewards stored in renewable pickup CDOs: every pickup of ingredient type A yields the same assigned item everywhere, while the placed actor, its class, save identity, transforms, and instance data remain unchanged. The currently shipped class-and-template map patch is unsafe and must not be used as evidence of runtime compatibility. The dedicated CDO serializer must validate each source reward and preserve unrelated package data before this option is used again.

Alternative MainWorld/DlcWorld map copies share one `UniqueSaveID`. The planner shuffles each canonical ingredient type once and applies that same assigned reward to every available location and mirror. Mirrors do not create extra ingredient types or separate spoiler rows. A type with an unvalidated location stays vanilla as a whole. Blueprint ancestry and reset behavior are validated through the prepared Extract cache; Generate stages the reward-owner Blueprints from that cache.

The spoiler lists one row per ingredient type and states how many ground pickups use that mapping. The total count still reflects affected pickup locations. The inventory report builds this same full runtime Ingredients pool rather than the smaller legacy `materials.json` subset, and marks rows with the separate `randomize_materials` option and `INGREDIENTS` pool.

### 3.4 Shops

The shop manager uses:

`BP_ShopManager -> CDT_ShopLineup`

The composite table aggregates:

- `DT_ShopLineup`
- `DT_ShopLineup_Trade`
- `DT_ShopLineup_Luxury`
- `DT_ShopLineup_DLC`
- `DT_ShopLineup_Trade_DLC`

All use native row struct:

`/Script/Extensions.ShopLineupTableRow`

A product entry contains:

- `ArticleDataPath`
- `Price`
- `bIsUseStock`
- `Stock`

In every Shop randomization mode, only the Article identity moves. The destination keeps its `Price`, `bIsUseStock`, `Stock`, unlock/evaluation condition, row, and slot position. A vanilla infinite slot is completely excluded as both a source and destination, so its item and all metadata remain unchanged.

Only a slot whose raw payload explicitly proves `bIsUseStock = True` and a positive `Stock` quantity is eligible. A zero-masked `bIsUseStock` resolves to its Unreal schema default `False`, so it is locked as `INFINITE_SHOP_STOCK` even if the catalog has a stale `True` value. When the logical flag is `True`, zero-masked, missing, conflicting, or nonpositive stock values are locked as `UNKNOWN_SHOP_STOCK_SEMANTICS`; a zero-masked `Stock` decodes to 0 but does not prove finite quantity or a sentinel meaning. These rules apply identically to Shop-only, NPC Trader, and Shop/World pools. Logical defaults are kept separate from serialized bytes: no stock fields are materialized or rewritten.

The Tool Shop remains excluded from sources and destinations. Its repeatable material purchases keep their original infinite stock.

Pre-order weapons are currently left untouched. They are neither removed nor stock-edited by the randomizer; their shop rows follow the same eligibility and preservation rules as other vanilla shop products.

The renewable Magmell Tool Shop is the explicit `NORMAL / Shop_Jadwiga_Item` row in `DT_ShopLineup` (56 product slots across 40 Card/evaluation groups). Its catalog entries use `protected=true` and `protection_reason=MAGMELL_TOOL_SHOP`, which excludes the entire row as both sources and destinations in Shop-only and Shop/World planning. The separate `NORMAL / Shop_Jadwiga_Weapon` row is not protected by the Tool Shop rule; its finite-stock slots remain eligible, while infinite or uncertain stock slots follow the shared stock rule. Card Article rewards are not protected by the Tool Shop rule. The generation manifest lists slot-lock reasons and slot indices for audit.

Shop/World Article mixing preserves each Soft Object Path FName as its literal NameMap value plus its explicit numeric suffix. Catalog World Article paths are literal identities with Number `0`; suffix characters such as `_07` are never parsed into a Number. Shop sources retain their independently decoded package/object Numbers, and shop patches resolve NameMap indices from the destination table before writing those Numbers. When a Shop identity moves to World, the same two FNames pass through the world serializer. Reopen validation checks those explicit identities on generated assignments as well as the visible package path. A controlled test confirmed that rewriting `_07` as base-name + Number `8` produced `_7` and crashed, while preserving the literal `_07` with Number `0` opened successfully.

### 3.5 Enemy Drops

Enemy rewards come from three DataTables under `/Game/Blueprint/Article/DropItem/`:

- `DT_DropItemData`
- `DT_DropItemData_AfterEnding`
- `DT_DropItemData_DLC`

Each `DropItemInfos[]` entry contains a `Weight` and an `ArticleBulkInfo`. The whole `ArticleBulkInfo.Articles[]` array is the movable reward bundle, so article paths, `Count`, and `bNotification` move together. `Weight`, `DropRate`, `DropDataID`, row identity, and table identity stay at the destination. Enemy Blueprints and their drop-link fields are not patched.

The version-bound classification catalog contains 233 rows: 189 `NORMAL_ONLY`, 12 `SHARED_BOSS_NORMAL`, and 32 `UNRESOLVED`. The `Randomize Enemy Drops` checkbox controls both this pool and the audited one-time enemy rewards described in §3.10. When off, no Enemy Drop table is patched and the one-time enemy locations are removed from the World Loot source and destination pool. When on, `NORMAL_ONLY` and `SHARED_BOSS_NORMAL` reward bundles move within the Enemy Drop pool. A shared row changes the drop for both its respawning-enemy and boss/miniboss owners; confirmed one-time quest rewards remain in World Loot and protected rewards remain locked. Generate checks each table package independently and plans only those present in the installed inventory: BASE (130 rows / 762 eligible slots), AFTER_ENDING (3 / 27), and DLC (68 / 522). All active compatible tables share the eligible bundle pool. Unresolved rows remain unchanged. Python and the helper require BASE plus a canonical subset of available optional source tables; valid source sets are BASE, BASE+AFTER_ENDING, BASE+DLC, and all three. The patch request contains only destination tables with changed assignments, while `sourceTables` contains the complete active source set.

Article property FNames are rebound recursively for cross-table copies. Some valid source Article properties have a `PropertyTypeName` whose optional `Nodes` list is absent; the helper preserves that absent metadata and continues rebinding actual FName values. Both sides validate row/slot identities, reward fingerprints, table availability, and required `.uasset/.uexp` pairs. The C# helper reports full exception context and reopens each output to verify that reward bundles moved while `Weight`, `DropRate`, `DropDataID`, protected rows, and other destination data stayed intact. A real direct-helper replay and a full three-table Generate passed; the new generated cross-table behavior still needs gameplay validation.

### 3.6 Blood Code grants and mastery Boosters

Blood Codes and Booster mastery rewards are separate randomization categories. The Blood Code catalog contains 79 logical reward locations: 35 direct `GainItem` rewards, 30 SmallScenario reward events (alternate Yadorigi assets are patched with their same logical event), and 14 primary companion `BuddySettings.BloodCode` references. There are no standalone proficiency-reference reward locations. The installed inventory contains 81 `DA_BloodCode_*` DataAssets; `DA_BloodCode_Buddy` is Debug-only and Lou_01 remains vanilla by current user decision. Distribution is BASE 56, AFTER_ENDING 13, DLC 10. Every eligible Blood Code Article appears once in the pool. `DA_BloodCode_LouDisp_02` has a direct DLC quest grant plus a Lou Disp scenario route and its Yadorigi mirror; these physical routes are linked to one logical slot and receive the same assignment.

Noah_01 and Guardian's Heart are owned by the primary `/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/noah/DA_BuddySettings_Noah` and `/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/GuardianHert/DA_BuddySettings_GuardianHert` assets. Their reward is the exact `BloodCodeArticleData` object import in `BuddySettings.BloodCode`, the same structure used by the gameplay-confirmed Iris unlock. Each is mirrored as metadata in `BloodCodeArticleDatas[1]` or `[2]` on `QSTask_BloodCodeAddProficiency_0` in both `/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10210` and `/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10250`. Those task arrays track proficiency for the associated Blood Code; they are not the player-facing reward owner. The pool counts Noah_01 and Guardian's Heart once each, patches the BuddySettings object import, and synchronizes the same assignment into both proficiency references. This preserves the proficiency association while changing only the Blood Code identity.

Direct `GainItem` quest grants patch only the selected `ArticleBulkInfo.Articles[]` path, preserving task logic, `Count`, and `bNotification`. SmallScenario locations patch only the reward CDO's `ArticleDataPath`; Yadorigi alternates stay synchronized. The BuddySettings patcher verifies the `ObjectProperty` schema, import class/path/FName numbers, and unique owner dependency, changes only the import reference, reopens the package, and checks that raw export payloads and opaque sidecars are unchanged. Companion requirements remain attached to the original settings asset.

Lavinia's tier-one Blood Code is one logical pool location, but its reward identity is serialized in three physical places: `DA_BuddySettings_Lavinia.BloodCode` and the `ArticleBulkInfo.Articles[0].ArticleDataPath` fields on `BP_SmallScenario_Lavinia_Present_c08010_B` and `BP_SmallScenario_Lavinia_Present_c08030_B`. The catalog ties both MagMell scenario CDOs to the existing Lavinia slot rather than adding duplicate candidates. The raw SmallScenario patcher validates each scenario's actual parent class (`BP_SmallScenarioGainBoosterBase_C` or `BP_SmallScenarioBase_C`) and changes only the serialized ArticleDataPath FName pair, preserving `Count`, notification, and the remaining scenario export bytes.

The true New Game Lou_01 ownership source remains unresolved, and its investigation is parked by user decision. The field `/Game/Blueprint/Article/DA_InitialInventory_Player`, export 1, `BloodCodes[0].0.ArticlePath`, was tested as a candidate, but a fresh New Game with that path changed produced both vanilla Lou_01 and the randomized replacement. This proves that field is an additive grant path rather than the true starting ownership initializer. For the current design, Lou_01 stays vanilla and outside the Blood Code pool; when Blood Code randomization is enabled, all other confirmed obtainable Blood Codes are shuffled among the 79 catalogued logical reward locations. Debug-only `DA_BloodCode_Buddy` is not a player reward. Catalog and installed Article paths use exact package/object FNames with Number 0, not suffix-based aliases.

The Lou Disp quest and the Lou Disp past-scenario `ArticleBulkInfo.Articles[0]` route are one logical `DA_BloodCode_LouDisp_02` reward, not two copies. The scenario's primary and Yadorigi exports, plus the DLC GainItem export, are all patched from one assignment. The catalog has one source identity and emits one spoiler assignment for this reward.

The Beginning references to Lou_01 in `QST_MainEpisode_Beginning_e10210` and `e10250` are proficiency/task metadata. Lou BuddySettings assets also contain references but are not confirmed startup owners. Inspected player, game mode, game state, controller, and default settings exports did not expose a serialized current/owned starting Blood Code field. A fresh-save or runtime-state comparison is still needed to prove where Lou_01 first enters player ownership.

Known reward owners include Noah_01 and Guardian's Heart in their primary BuddySettings assets (with linked proficiency metadata in the mirrored beginning quests); Noah_02 in `BP_SmallScenario_Noah_Present_a20030_01` and its Yadorigi alternate; Dullahan/Franz in `QST_MainEpisode_Beginning_e10380`'s `QSTask_GainItem`; other first-tier companion unlocks in their primary BuddySettings; and installed DLC Harmony mission `GainItem` tasks. NoahAnother is an alternate-path BuddySettings asset with the same Noah_01 import but no serialized incoming asset references in the extracted package set; it is not the canonical player-facing owner and is left unchanged. Each destination remains independently listed in spoilers with its canonical owner/export location. An earlier catalog/cache revision incorrectly treated the beginning proficiency arrays as the Noah and Guardian rewards and omitted their actual BuddySettings packages; the corrected catalog and source registry require those owners before Generate.

Mastery Booster rewards live in `/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/DT_GainBoosterSmallScenario`. Its 102 rows contain a mastery requirement and Blood Code path as well as a Booster StoryFlag, Booster `ArticleDataPath`, `Count`, and `bNotification`. The movable reward identity is the StoryFlag and Booster `ArticleDataPath` together. The row name/index, mastery owner/rank, Blood Code path, `Count`, `bNotification`, and all other destination fields stay unchanged. Its content distribution is BASE 57, DLC 18, and AFTER_ENDING 27.

The mastery table and every Blood Code quest/article pair are checked against the installed package inventory. Every required BASE Blood Code source package must be present in the Extract cache; a missing BASE source invalidates the cache instead of silently shrinking the pool. Optional DLC and After Ending locations are included automatically when their package and Article sources are installed. Present classes labeled `UNKNOWN`, `DLC`, or `AFTER_ENDING` are not blocked by those labels. Compatible sources share their feature pool automatically; no content-class mixing option is exposed or persisted.

The user confirmed both features in gameplay. A direct `GainItem` Blood Code grant was modified and observed in-game. For Boosters, the `BloodCodeMastery.Lou` Rank01 slot that normally grants `Booster_MND_1` was changed to `Booster_WIL_1`; after speaking with Lavinia, the game granted WIL. Separately, the 102-row Booster table and a two-row MND/WIL swap were serialized, reopened, and checked for preservation of the other 100 rows. The automatic cross-class pool behavior has package-level automated checks; generated gameplay for that behavior remains unreported.

### 3.6.1 New-game Booster slot unlock

The optional `Miscellaneous > Unlock 6 Booster Slots` setting changes only `DefaultValueBool` for `UnlockEquipSlot_Booster5` and `UnlockEquipSlot_Booster6` in `/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/DT_StoryFlagDefine_GameSystem`. Both flags are Boolean, default to `False`, and have `IsInitToKeepValue=True`. Holly's first request, `/Game/Quest/CharacterEpisode/CharacterEpisode_Holly/QST_CharacterEpisode_Holly_c05020` export 79, sets Booster5 to `True`; her final request, `QST_CharacterEpisode_Holly_c05120` export 83, sets Booster6 to `True`. The option sets these same initial values so a new game begins with six slots. `Miscellaneous > Unlock Second Weapon Slot` changes only the `DefaultValueBool` of `UnlockEquipSlot_Weapon2` in that same DataTable. The row is Boolean, defaults to `False`, and also has `IsInitToKeepValue=True`; it is the same flag raised by upgrading a Jail to +11. These slot options share one validated table patch and change only the requested defaults. Because the flags retain saved values, existing profiles are not promised a retroactive unlock. Quest packages remain unchanged. Extract includes this optional DataTable in the reduced cache when it exists in the installed game, so Generate reads the READY cache directly. If an older cache lacks it, Generate uses the extractor's persistent content-addressed cache, so subsequent runs reuse the package.

The Boosters group contains mastery reward randomization and Booster crafting recipe randomization. Miscellaneous contains `Unlock 6 Booster Slots`, `Unlock all NPC Trader Shop Tiers`, `Unlock Motorcycle at Start`, and `Unlock Second Weapon Slot`. `Unlock all NPC Trader Shop Tiers` makes affinity-gated NPC Trader tiers available from the start without changing reward identities; the current maximum affinity requirement is 72. `Miscellaneous > Unlock Motorcycle at Start` adds one `DA_Item_Important_SpellForMotorcycle` entry to `DA_InitialInventory_Player.Items`, enables the `BloodMagic_BikeCreator` default in `DT_StoryFlagDefine_Beginning`, and changes `BP_Player.MountControlComponent.AvailableStoryFlagCondition.EvaluationFormula` to that flag. The last change removes the later quest-progress gate that otherwise keeps the motorcycle icon and summon action unavailable in a new game. Extract registers the flag table, player Blueprint, and Motorcycle Forma Article as optional sources in the persistent cache; Generate reuses those validated cached packages for its overrides. Existing caches can add them through `Update Prepared Data`, which reuses the existing raw cache and extracts only any newly required sources rather than repeating the full game extraction. Quest acceptance and boss-completion flags remain unchanged; the patch changes only that availability formula and leaves the component's other fields intact. Existing saves are unaffected, all other initial items are preserved, and the option can be combined with Initial Inventory randomization.

### 3.7 Defensive Gadgets

The canonical reward pool contains 17 gadgets: 16 BASE and one additional (`DA_DefGadget_ZenonGreatShield`). Five shortened asset names are normalized as aliases to their `_1` canonical items. The runtime World Loot catalog currently contains ten eligible physical Gadget reward occurrences; each retains its own package/export/reward-index identity and its destination `Count` and `bNotification`. Gadget metadata classifies those sources but cannot add a new copy. The source Article multiset is asserted against final World assignments.

World Loot also controls the structurally confirmed Lise/Warp grant at `/Game/Quest/CharacterEpisode/CharacterEpisode_Lise/QST_CharacterEpisode_Lise_c02070`, export `QSTask_GainItem_0`, property `ArticleBulkInfo.Articles[0].ArticleDataPath`. Only that reward path moves. Source `.uasset/.uexp` fingerprints are checked before patching, and the staged copy is reopened and compared so unrelated export fields remain unchanged. The NPC planner keeps its own RNG domain, but is enabled and disabled with World Loot. Jadwiga Servant's direct Lightning Barrage, Parry, and Booster Dex rewards are generic World Loot destinations.

The Defensive Gadget NPC grant accepts canonical Article paths from both `/Game/Blueprint/Article/Gadget/Defence/` and `/Game/Blueprint/Article/Gadget/DLC/`. It selects one actual eligible source occurrence from the active World/Shop pool, moves that occurrence to the NPC grant, and returns the NPC grant's original Article to the exact source slot. The transfer consumes one occurrence; it never leaves the selected Article active at its old location as well.

### 3.8 Player Jails

The metadata-only `data/jails/current/jails.json` catalog contains 12 canonical player Jails: six BASE and six Pathos/DLC. Companion assets named `DA_Buddy_Jail_*` and the UI label `Death` are excluded; the canonical item is `DA_Jail_Reaper`. The catalog classifies rewards, but does not create source occurrences. All 11 confirmed World Loot Jail sources, including base-game Hounds, are movable; Ogre also appears in Initial Inventory. The Sunken City Stinger pickup has an exact source policy: its Article reward can move while its flag behavior and all other actor fields remain intact, and its DLC map mirror receives the same assignment. Josee's finite Stinger shop row is an alternate route for that same unique base Stinger, not an independent source or destination: when World Loot is enabled, the shop row mirrors the final Outpost Article assignment; otherwise it remains vanilla and is excluded from Shop shuffling. If the pickup is active but the base Shop DataTable is missing from the installed package inventory, Generate stops rather than emit an inconsistent Stinger assignment. The distinct `DA_Jail_Stinger_Pathos` remains its own DLC identity and source. Runtime validation cross-checks researched World Loot sources against `world_loot.json`, including package/export identity, reward path, payload, save ID, protection metadata, and the Stinger's reward-only mode. A separate Article allowlist covers 17 ordinary flag-associated rewards and does not weaken this source-specific Jail rule.

World Loot validates each unique Jail Article against exactly one canonical physical source (zero for the starting Ogre) before planning. An additional unlinked map occurrence is rejected instead of being treated as another copy; a map mirror is accepted only when it points to the audited source export and carries the same Article, Count, and notification payload. Josee's alternate Stinger shop row is removed from source/destination pools and is not shown as a second spoiler assignment; its serialized Article follows the canonical Outpost result. Jadwiga's finite `DA_Jail_Ogre` product at `Shop_Jadwiga_Weapon[29]` is also removed from independent pools: its Article declares the same `Gain_Jail_Ogre` StoryFlag as the starting Jail, so it is treated as a linked mirror. If Initial Inventory changes the starting Jail, Jadwiga's Article follows that assignment and does not count as another source or spoiler assignment. A missing or protected source stays in its owning system or vanilla location; it is never synthesized as an extra World Loot reward. The planner asserts unique source occurrence IDs and exact source-to-assignment Article counts. Spoiler rows remain under `WORLD_LOOT` and label the Jail candidate category. Jail DataAssets are not staged or packaged; only changed destination map packages and linked shop tables enter the existing pipeline.

Jails are included whenever World Loot is enabled, with no separate feature checkbox. Only eligible physical sources enter its pool; absent Pathos packages are skipped while available BASE sources continue. Initial Inventory selects an owned Jail source occurrence from the already-planned World/Shop inventory, moves that Article to the starting slot, and returns the original starting Jail to the source slot. The Jadwiga Ogre row is excluded from that source inventory and follows the final starting Jail assignment as a linked mirror. Jail pickup/chest behavior has structural and deterministic coverage but still needs an in-game collection test.

### 3.9 FieldEnemy direct rewards

`data/field_enemy/current/` contributes 24 checksum-bound singleton quest-reward destinations, all classified DLC, plus a separate list of 10 protected key/progression grants. The destinations are WHERE slots only: their Article identities join the existing whole-payload World Loot pool, so duplicate material rewards remain separate occurrences and are not collapsed into canonical candidates. The Zenon Great Shield location contributes one WHERE slot while the existing canonical Defensive Gadget catalog continues to own its single WHAT identity. No additional UI option is needed.

FieldEnemy is an optional WHERE group. Generate includes each destination whose Quest source package exists and skips only unavailable locations. The ten protected records never enter the location list. Their Article paths are matched exactly against the global Key exclusion policy; the four identities not previously listed were added to `data/reward_pools/current/important_item_exclusions.json`. No name or keyword heuristic is used.

Changed destinations use the existing IoStore extraction and structured `QSTask_GainItem` patch path. Before inspection, the extracted original `.uasset` and `.uexp` must match any catalogued SHA-256 fingerprints. The patch targets only the registered `ArticleBulkInfo.Articles[rewardIndex].ArticleDataPath` entries, plus the explicitly audited Hot Spring `bNoMultipleGain` flag; task/export identity, reward count, notification, bundle structure, and all other quest data stay at the destination and are checked by serializer reopen validation. The Hot Spring flag is cleared even for self-assigned reward bundles so repeatable items still grant when already owned. For paired Hot Springs, two ordinary singleton World Loot sources are combined at the destination. Each unique recipe reward is assigned once to a singleton destination; duplicate recipe-unlock occurrences are replaced by ordinary singleton World Loot items, preserving the serialized slot count. Only changed quest packages and Hot Spring packages requiring the repeat-gain flag change enter staging. The catalog's reported in-game test covers one base-game FieldEnemy grant; the full generated Hot Spring/DLC pool still needs gameplay validation.

### 3.10 One-time enemy rewards
Respawning/farmable `DropItem` bundles stay in the separate normal-enemy Enemy Drops pool. Confirmed one-time enemy rewards are World Loot destinations and sources only while `Randomize Enemy Drops` is enabled, so they exchange with ordinary World Loot and, when the separate Shop/World option is enabled, eligible Shop rewards. With Enemy Drops off, these reward locations are omitted as both sources and destinations, leaving their quest assets vanilla while ordinary World Loot remains active. Classification is per reward slot, based on serialized encounter/quest completion and persistence evidence; enemy-name matching is not used. A reward attached to a boss is not automatically eligible: key, quest, progression, Blood Code, story-flag, and other non-Article rewards keep their own protected systems.

The audited enemy-gated World Loot group has 44 locations: 15 direct boss/miniboss Article grants in `world_loot.json` (13 dungeon grants and two DLC main-episode grants), 24 eligible FieldEnemy quest destinations, and five Harmony Moon enemy-defeat rewards. All 44 are included only when Enemy Drops is enabled; otherwise none can contribute a source reward or receive a World Loot assignment. The two DLC grants are reached from enemy-defeat event graphs in `QST_MainEpisode_DLC_e90153` and `e90193`; they preserve each destination's original trigger, count, notification, and quest metadata while allowing only the Article path to move.

The research scanned all 1,088 installed Quest packages for Article GainItem tasks, then parsed 76 packages with EnemyDefeat event graphs connected to GainItem nodes. Of 56 reachable concrete Article reward slots, 18 were already in World Loot, 22 key/quest/progression slots remain protected, eight Blood Code rewards stay in the Blood Code pool, six repeatable multiplayer rewards stay out, and two new ordinary DLC grants were added; three reachable tasks had no concrete Article. The 13 dungeon rewards, 24 FieldEnemy eligible rewards, 10 FieldEnemy protected rewards, and the known Josee main-episode boss quest were separately checked. This audit does not claim respawn semantics for 32 shared/unresolved generic `DropItem` rows; those rows remain unchanged.

The primary regression is Alexis the Heartless Bodyguard in East Bay Underground Ruins. Its visible reward is Adrenaline Formae; the UI texture `T_UI_SGadget_StrikeRiser` identifies its icon, while the serialized Article is `/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportAttack/DA_GB_CV19_AtkUpFixed`. The actual owner is `/Game/Quest/Dungeon/MM/QST_QuestDungeon_MM_Medium08_Boss`, export `QSTask_GainItem_0` (index 28), reached from the enemy-defeat completion path. It was missing from the former World Loot location catalog, so the first divergence was before planning. The assigned Article, exact FNames, staged quest serialization, spoiler entry, and Retoc container were validated. Gameplay confirmation of this generated build remains pending.

### 3.10 Bequeathed Formae

The metadata-only catalog `data/bFormae/current/bequeathed_formae.json` lists the 10 player Bequeathed Formae (8 BASE, 2 DLC) and their source eligibility. Its one World source (`DA_Bow`) and seven Shop sources are cross-checked against the current World Loot and Shop catalogs before generation; the source identities remain part of those active inventories and may cross between World and Shops only when the explicit Shop/World option is on. The Forma metadata does not inject duplicate candidates.

Battle Axe: Idris's Conceit is awarded by the beginning MagMell quest `/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10365`, export `QSTask_GainItem_1`. It is represented as a regular direct Article destination in the always-on World Loot catalog, so its reward enters the shared open-world reward permutation instead of a two-item Forma-only shuffle. The World Loot quest serializer changes only the Article path and preserves the quest slot's count, notification flag, row/array structure, and unrelated export data; the staged package is reopened and validated before packaging. Gameplay testing confirmed the quest awards its randomized item. Absolute Executioner is another single logical World Loot destination with two alternative reward routes in `/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00382` and `/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00560`. The original reward is inspected as a Gadget/Bequeathed Forma, but the assigned reward comes from the broader World Loot pool and can be any Article. The alternate `QSTask_GainItem` export therefore uses the generic World Loot quest Article serializer, not the Gadget-only Bequeathed Forma patch operation. Both routes receive the same Article and preserve a single obtainable item regardless of which route the player completes. Bequeathed Forma DataAssets are never staged.

### 3.11 Booster Extractor

The version-bound metadata catalog describes exactly two recipe DataTables: `DT_BoosterRecipe` (36 BASE rows) and `DT_BoosterRecipe_DLC` (7 DLC rows). Together they contribute 43 recipes to `CDT_BoosterSyntheticRecipe`, which is a runtime composition and is never patched. Extract obtains original packages through `IoStoreAssetExtractor` and includes these two small tables in its reduced cache. Normal Generate uses `CachedAssetExtractor` to reuse those originals; except for the canonical `workspace/research_assets/CV2.usmap` runtime mapping, the rest of `workspace/research_assets/` is local research/test data.

Reward randomization changes only `CreateArticleDataPath`. `Cheaper Booster Extractor (only 1x of each material)` sets every listed `RawMaterials[].UseNum` to 1 and preserves the Haze cost, recipe structure, and ingredients. It uses the same two recipe tables already included in the reduced Extract cache; Generate patches cached copies in staging and reopens them for validation. The game was tested with one unit of each ingredient and successfully crafted while charging Haze. Zero-quantity requirements and material refunds are not part of this feature. The option also works when reward randomization is off. Extractor destinations accept Booster Articles only. In-game testing confirmed that Booster-to-Booster swaps display the correct name, icon, category, traits, description, materials, and recipe UI and craft successfully; generic Articles can craft but show incompatible Extractor information.

### 3.12 Initial Inventory

The optional `Randomize Initial Inventory` control changes exactly three `ArticlePath` values in `/Game/Blueprint/Article/DA_InitialInventory_Player`: `Weapons[0].0.ArticlePath` (starting Weapon), `DefenceGadgets[0].0.ArticlePath` (starting Defensive Gadget), and `Jails[0].0.ArticlePath` (starting Jail). Their validated vanilla identities are `DA_SingleSword_Future_Lesser_1`, `DA_DefGadget_LesserShield_1`, and `DA_Jail_Ogre`. The duplicated BodyLight references, BasicRecovery, and `BloodCodes[0].0.ArticlePath` remain vanilla. Gameplay demonstrated that changing the BloodCodes field adds a second Blood Code; it is not treated as the true initial ownership source.

The starting Weapon pool is assembled from available, safe, unprotected singleton Weapon rewards already represented by the World Loot and Shop catalogs; it does not keep a second hand-authored Weapon list. Defensive Gadgets come from `data/reward_pools/current/defensive_gadgets.json`, and player Jails come from `data/jails/current/jails.json`. Every candidate Article package is checked against the installed package inventory. Numbered package aliases are resolved by the shared `PackageInventory`; the selected source is still a concrete location occurrence, so repeated Articles retain their actual source count. Optional missing gadget/Jail packages, including Pathos content, are omitted without disabling available BASE candidates.

Planning order is explicit: World Loot and Shops establish their active source occurrences first, including the global Shop/World mix when enabled. Initial Inventory then chooses one owned Weapon, one Defensive Gadget, and one Jail occurrence from independent `initial_inventory.weapon`, `initial_inventory.defensive_gadget`, and `initial_inventory.jail` RNG domains. The planner transfers each selected Article into the starting slot and moves that slot's original Article back to the selected occurrence's former location. Later Defensive Gadget NPC grants use the same owner-aware transfer rule. No Article path is removed globally when it has multiple independent physical occurrences.

The original DataAsset is acquired when Initial Inventory is enabled, through the existing `IoStoreAssetExtractor`, materialized under the generation workspace, fingerprinted, and structurally inspected by `tools/uassetapi-worldloot`. The helper targets only the three starting gear properties, fails closed if source identities or layout differ, serializes only to staging, reopens the package, and verifies all preserved entries—including the vanilla Lou_01 ArticlePath. The manifest records the source package/fingerprint, selected canonical paths, candidate counts, catalog revision, RNG domains, reservation count, and validation status. The spoiler CSV includes one row per starting gear slot; there is no initial Blood Code assignment entry. Except for the canonical `workspace/research_assets/CV2.usmap` runtime mapping, `workspace/research_assets/` holds local research/test material.

The 102 Booster mastery rewards stay in their own pool because each row couples its Article path to a StoryFlag and mastery requirement. The 43 Extractor recipe outputs are shuffled with the Booster Article occurrences present in the active World Loot plan; mastery rewards and synthetic canonical identities never enter this pool. Every assigned World Booster occurrence is exchanged with one unused vanilla recipe output at that same physical source occurrence. Remaining World Boosters stay in World Loot. This preserves the combined World-plus-recipe Article multiset one-for-one, including direct NPC and quest Booster grants such as Dex and Iris. The planner rejects duplicate physical occurrence IDs and duplicate Article identities, and allocation uses the isolated `cv2-extractor-booster-recipes-v1` RNG domain.

Each recipe table is resolved independently from the installed inventory. The 36 BASE recipes continue when the 7-row additional table is absent; with both tables present the maximum pool is 43. Booster reward candidates also require their own Article packages. The generation manifest records available/missing/invalid source packages, recipe counts, candidate allocation/remainder counts, modified tables, and reopen validation; the spoiler shows recipe identity and assigned Booster without serialization internals.

---

## 4. Randomization pools

### 4.1 Automatic availability and compatible pools

There is no global Content Mixing setting. Within each randomizer system, all available content that is structurally compatible participates automatically, regardless of `BASE`, `DLC`, `AFTER_ENDING`, or `UNKNOWN` catalog labels. Content from unrelated systems stays separate unless the explicit `Mix Shop rewards with World Loot` control joins the compatible Article identities across those two systems.

There is no `Include DLC` option and the app does not inspect Steam ownership, license, or entitlement. At Extract/Generate time it uses installed package inventory or the validated cache to resolve each catalogued source as `AVAILABLE`, `MISSING`, or `INVALID`. Only sources that exist and pass relevant structural checks reach their planner. A missing optional package is skipped; a required missing package or invalid core data fails safely. Availability does not depend on content class.

Availability filtering precedes protections and shuffling. World Loot WHERE packages, each Shop table and product Article, and each active feature's source package are filtered by inventory presence. Missing catalogued items or source packages are skipped individually where the feature supports independent slots; linked unique rewards such as Jail identities are kept only when both the Article and physical source package exist. Renewable Ingredients use their validated cache-backed Blueprint profiles and CDO reward owners. FieldEnemy slots are filtered independently by installed Quest package. `UNKNOWN` is metadata, not an exclusion. Existing safety checks for protected, important, unsafe/unparsed, flag-changing, and invalidly parsed content remain in force.

`tools/import_catalogs.py` carries reviewed labels from optional `where_content_class`, `what_content_class`, and material class columns into runtime JSON. It writes `UNKNOWN` when source CSVs omit those columns, and rejects unsupported values or conflicting WHERE labels within one grouped World slot. Rebuilding a catalog therefore preserves explicit research without deriving a class from an asset path or map name.

The revision-bound classification audit currently reports World Loot WHERE as 6 BASE, 1 DLC, 5 AFTER_ENDING, and 1,736 UNKNOWN; its 1,751 reward occurrences are 19 BASE, 6 DLC, and 1,726 UNKNOWN. Materials remain unresolved for all 734 rows across WHERE, resource Blueprint, reward class, and Article class. Shops have authoritative WHERE counts of 801 BASE and 54 DLC; WHAT is 7 BASE, 2 DLC, and 846 UNKNOWN. FieldEnemy has 24 DLC WHERE locations; its original WHAT references are 1 DLC and 23 UNKNOWN, but WHAT does not gate those destinations.

The audit output in `data/content_classification/current/classification_audit.json` lists every unresolved classification occurrence and retains its research status (`EXHAUSTIVE_UNRESOLVED` or `NOT_IN_INSTALLED_INVENTORY`). Those labels are not runtime availability states and do not become BASE by default. The prior installed-build audit put all six known additional-content markers in `pakchunk0-Windows.utoc`, alongside ordinary game content; container ownership cannot establish store entitlement or classify individual packages. The version-signature directory still contains no tested game definition; package presence does not mark an executable officially supported.

### 4.2 Materials pool

Contents:
- renewable gathering materials only

Behavior:
- map each eligible reward Article to one other eligible ingredient Article globally
- keep every pickup of the same original Article consistent, including shared CDO owners and mirrored locations
- edit only the reward-owner Blueprint CDO's audited Article path; do not change placed actor classes or map exports
- preserve the location
- preserve the renewable/reset behavior
- preserve any location-specific save identity
- reward/resource identity is randomized
- independently filter location WHERE, resource-class WHAT, and Article reward WHAT before Blueprint/map extraction
- filter each row by source package presence; UNKNOWN labels alone do not block it, while missing packages and failed reset-safe/parent validation skip or invalidate the affected row

### 4.3 World Loot pool

Contents:
- white ground pickups
- purple ground pickups
- yellow/legendary ground pickups
- treasure chest rewards
- canonical Defensive Gadget reward payloads
- canonical player Jail reward payloads
- the three canonical Iris Character Episode Booster rewards, reusable at compatible World Loot locations
- Jadwiga Servant's direct Lightning Barrage, Booster Dex, and Parry rewards as generic Article destinations
- the DLC e90360 Gift Heal Point Up `QSTask_GainItem` reward as a structured Article destination
- the five Harmony Moon memory quest Booster rewards as structured Article destinations in the generic pool
- canonical Booster identities left after Extractor allocation, but only when Extractor randomization is enabled

Defensive Gadgets, Jails, Iris rewards, and (when Extractor is enabled) its unallocated Booster identities are classified against the World Loot pool. Metadata identifies and validates reward types; it does not create source occurrences. The pipeline filters metadata by installed package presence, then plans from movable physical source occurrences. Article identity may repeat at different sources; stable occurrence IDs, not Article names, establish uniqueness. Unowned or protected candidates are skipped and never added as synthetic World rewards. Protected source locations remain vanilla. Available compatible physical sources join the pool automatically.

Three Magmell Jadwiga NPC grants are singleton destinations in this generic pool: Servant1's Lightning Barrage reward (`jadwiga_servant_lightning_barrage_reward`), Servant2's Booster Dex reward (`jadwiga_servant_booster_dex_reward`), and Servant3's initial Parry reward (`jadwiga_servant_initial_parry_reward`). They join the planner's one-reward-Article group, preserving direct-slot arity; multi-reward bundles remain bundled and are not flattened. Each explicit slot is bound to its package, CDO, export index, vanilla reward/NameMap identity, 55-byte layout, and source `.uasset/.uexp` SHA-256 fingerprints. Servant1's vanilla FName Numbers are already 0/0; Servant2 and Servant3 start at 2/2. All three can receive any canonical singleton Article reward from World Loot and are not category-restricted. The shared helper writes the complete ArticleDataPath and object name literally into the existing NameMap entries, sets both output FName Number values to zero, reopens each staged package, and verifies all other exports/imports and unrelated RawExport bytes. It reuses existing NameMap references and adds no imports; the offsets are not generalized to other RawExport NPC grants. `Gain_BlastBolt`, `Gain_Booster_Dex`, `Gain_DefGadget_Parry`, scenario flow, dialogue, and all other logic stay at their original locations. The three assignments use stable spoiler/manifest identities and the shared extraction, staging, and Retoc package path.

The DLC e90360 Gift Heal Point Up grant is a fourth generic direct Article destination. Its source is `/Game/Quest/DLC/QST_MainEpisode_DLC_e90360`, export `QSTask_GainItem_1` (export index 83), in `ArticleBulkInfo.Articles[0]`. The structured serializer changes only that reward's `ArticleDataPath`; it preserves the row name, mastery/story requirements, Blood Code path, `Count`, `bNotification`, and all other target-grant fields. The catalog binds the source reward and its vanilla FName representation for safe source validation, while generated assignments store canonical package/object strings literally with FName Numbers zero. The spoiler and generated manifest report canonical reward paths and destination identity, not FName Number internals. This destination joins the ordinary singleton World Loot pool and uses the same extraction, staging, and Retoc path. Automated real-asset reopen validation passes; confirming the transferred reward in gameplay remains pending.

The five Harmony Moon memory rewards (`Holly`, `Josee`, `Lyle`, `Noah`, and `Zenon`) are additional generic singleton Article destinations. Each package uses export 39 `QSTask_GainItem_0`, `ArticleBulkInfo.Articles[1]`, and an `Articles` array of exactly two entries. The serializer changes only the reward `ArticleDataPath`, writes the canonical package/object names literally with FName Numbers zero, and keeps Count=1 and bNotification=true. The important/story Article at index 0, quest/mastery logic, multiplayer reward task at export 40 (`QSTask_GainMultiplayReward_0`), and all unrelated exports remain unchanged; strict snapshots include the complete target export with only the selected ArticleDataPath masked, plus other raw-export bytes. They draw from the ordinary one-Article World Loot pool, so a slot can receive non-Booster items. The five packages pass real-asset round-trip tests and are included in the normal staging/manifest/spoiler flow. Confirming rewards in gameplay remains pending.

Iris's three confirmed generic Booster rewards are direct World Loot source occurrences: the c00010 initial sample and c00070 rewards 1 and 2. The c00070 pair is represented by exactly two logical destinations (`iris_reward_1/2`); each planned reward is written to the matching Article index in both mirrored exports 39 and 40, preserving the same pair without creating four assignments. The structured serializer validates export and task names, array counts, vanilla Article paths, and source FName values, changes only the selected `ArticleDataPath`, reopens the staged package, and strictly snapshots unrelated exports and fields. Their physical sources can move to another compatible location such as a treasure chest; the metadata catalog does not create additional Iris Booster copies. Focused real-asset round trips, planner tests, and the user's gameplay confirmation validate this behavior.

Eligible physical source occurrences from these systems share the World Loot pool when their owning option is active. Direct quest/NPC rewards can be destinations in that pool, but catalog entries do not add copies; when a source occurrence is transferred to an external slot, the slot's former Article is returned to that exact source.

World Loot map patching preserves the source export bytes. UAssetAPI parses each registered reward and prepares the output header/NameMap, but the final `.uexp` starts from the original sidecar and copies back only the fixed-width `ArticleDataPath`, `Count`, and `bNotification` byte ranges for requested reward rows. The ExportMap serial sizes and offsets are aligned to that preserved raw layout. Existing NameMap entries and imports must remain unchanged; additions are limited to assigned Article FNames. The helper reopens the result and verifies exact assigned FName literal/Number identities, reward values, target-export snapshots, and byte-for-byte equality of every non-target export. Ambiguous field locations, changed target bytes outside those fields, variable-width targets, or invalid offsets fail closed before packaging.

The visual rarity of a ground pickup is not required to match the new reward in V1 unless later analysis proves a simple safe way to update it.

A chest reward may become a former ground reward and vice versa.

### 4.4 Enemy Drop pools

Available BASE, AFTER_ENDING, and DLC DataTables are checked independently; an absent optional table is skipped. All `NORMAL_ONLY` and `SHARED_BOSS_NORMAL` rows in active compatible tables share the eligible reward-bundle pool automatically. A shared row is used by both respawning enemies and boss/miniboss encounters, so its randomized reward applies to both. `UNRESOLVED` rows remain vanilla. `Weight`, `DropRate`, `DropDataID`, row/table identities, and all other destination fields stay fixed. No enemy Blueprint is extracted or patched. Cross-table FNames are rebound to the destination DataTable's NameMap and outputs are reopened; gameplay behavior still requires validation after a generated mod is installed.

### 4.5 Shop pools

`Randomize Shops` enables every available supported family: NORMAL, LUXURY, TRADE, DLC, and TRADE_DLC, including rows beginning `Character.Type.Buddy.` for NPC Traders. Buddy rows are part of Shops and have no separate checkbox or mode. No manual DLC switch is required.

Shop families are selected by semantic category:

- NORMAL: `DT_ShopLineup`, with optional counterpart `DT_ShopLineup_DLC`
- LUXURY: `DT_ShopLineup_Luxury`
- TRADE: `DT_ShopLineup_Trade`, with optional counterpart `DT_ShopLineup_Trade_DLC`

Each Shop family is resolved by the existence of its DataTable package. An absent optional family is recorded and skipped; other present families continue. Product class labels do not gate a table or a safe product slot. Runtime Article availability uses the parsed `ArticleDataPath` FName literal together with its serialized Number; it checks the resulting logical identity against the installed package inventory and cache. A literal base name with several numbered packages is ambiguous and remains unavailable until an exact FName identity resolves it. The available Shop pool moves Article identity only. `Price`, `bIsUseStock`, `Stock`, unlock conditions, row, and slot position stay at the destination. Only proven finite slots join a pool; infinite and uncertain slots remain vanilla and cannot be sources or destinations. A missing `TRADE_DLC` table only removes Liv; available base Traders continue.

The current catalog regression inventory is 12 base companions / 392 product slots plus Liv / 31 DLC slots when `TRADE_DLC` is installed, for a 13 / 423 maximum. Runtime rows and slots are always discovered from the loaded catalog and parsed tables; these counts are test expectations only.

The optional `Unlock all NPC Trader Shop Tiers` setting is independent of reward randomization. When enabled, it patches only the `EvaluationFormula` FString in affinity-gated groups of `Character.Type.Buddy.*` rows in `TRADE` and `TRADE_DLC`, changing the verified `Friendship.<Buddy>.Present + Friendship.<Buddy>.Adventure + Friendship.<Buddy>.Episode >= N` threshold to zero without changing the serialized width. Current source tables contain all nine thresholds from 8 through 72 for each of the 12 base companions and Liv. Unknown affinity formulas fail closed. Any appended episode/story clauses remain byte-identical. When disabled, all original formula bytes remain unchanged. Regular shops, including the protected Magmell Tool Shop, are never part of this operation.

In Shop-only mode, each randomized product moves its `ArticleDataPath` and item-object FName. `Price`, `bIsUseStock`, `Stock`, unlock conditions, row, and slot position are destination-local and byte-preserved. NPC Trader Only follows the same rule: destination `Price`, stock fields, NPC/row, product index, and replenish group stay fixed. Affinity formulas stay byte-preserved unless `Unlock all NPC Trader Shop Tiers` is enabled; that option changes only the verified Buddy-row affinity threshold. Only proven finite slots participate. The dedicated RNG domain is `npc_traders.only`. When Initial Inventory or an NPC grant takes an item from a Shop/World source, the original destination Article is returned to that exact source slot.

In Shop/World mode, only `ArticleDataPath` moves across a Shop/World boundary. Shop `Price`, `bIsUseStock`, `Stock`, unlock/evaluation condition, row, and slot remain at the Shop destination. Infinite and uncertain Shop slots are absent from both the shared source pool and Shop destinations. World Count, notification, actor, and location fields remain at their World destination. Shop Important slots participate unless their exact Article identity is listed in the Key or quest/flag exclusions, or another structural safety rule locks the slot. Initial Inventory and NPC direct-grant transfers consume one source occurrence and return the former destination Article to that source slot.

The optional `Mix Shop rewards with World Loot` control is available only while both World Loot and Shops are enabled. It includes proven finite slots from every available Shop family and NPC Trader in the shared Article pool. Across a Shop/World assignment, only `ArticleDataPath` moves; Shop price, stock, unlock/evaluation, NPC/shop identity, row, slot, and product index remain at the destination, except for the explicit NPC Trader unlock option described above. Infinite and uncertain Shop slots stay vanilla and are absent from the shared pool. World Count, notification, actor, and location fields remain at the World destination. Extractor, Initial Inventory, and NPC reservations claim a particular source occurrence; when a reserved reward is transferred, the original destination Article is returned to that source. No reservation clones an Article or leaves its original occurrence active twice.

Shop-only mode intentionally allows unusual outcomes, such as:
- a powerful weapon at a cheap slot price
- an upgrade material at a cheap slot price
- infinite and uncertain Shop stock slots remain vanilla and are excluded as sources and destinations

### 4.6 Blood Code pools

Blood Code grants shuffle only with other available Blood Code grants. Each source Quest and referenced Blood Code package is checked before extraction; missing pairs are omitted while the remaining grants continue. All available BASE, DLC, and AFTER_ENDING grants share one compatible pool. The shuffle is a permutation, so the active grant multiset is preserved and self-maps are allowed.

### 4.7 Booster pools

Booster mastery rewards use their own compatible pool: all available BASE, DLC, and AFTER_ENDING rows participate automatically. The StoryFlag and Booster `ArticleDataPath` always move together as one reward identity. Booster Articles absent from the inventory are omitted from the candidate pool. Self-maps are allowed; the system does not reroll them.

### 4.8 Defensive Gadget NPC grants

Only structurally confirmed gadget-specific direct quest/NPC reward slots participate. The current catalog contains one confirmed BASE grant (Warp/Lise). Gadget identities and direct-grant packages are filtered independently by package availability. This planner has a separate seed domain but follows the World Loot master toggle. Self-maps are permitted. Jadwiga's Parry and Booster Dex rewards are World Loot destinations supplied from the generic canonical Article reward pool.

### 4.9 Bequeathed Forma quest locations

Battle Axe: Idris's Conceit is a World Loot destination, so it follows the always-on shared World Loot pool. Absolute Executioner is one logical reward location with two alternative Lyle quest grant packages; its World Loot assignment is mirrored into both packages so either quest route gives the same randomized item. It is counted once in the source/destination multiset and spoiler. The Forma source catalog validates the existing World and Shop source entries without injecting duplicate candidates. No Bequeathed Forma checkbox is required.

### 4.10 Extractor recipe pools

The two recipe tables contain 36 BASE and 7 DLC destinations. Their source pool is the canonical Booster identity inventory assembled from the existing version-bound Booster catalogs and filtered by installed Article packages. A full `/Game/Blueprint/Article/Booster/...` path is the deduplication key. The planner selects one distinct reward per active destination from all available compatible classes; it fails closed if the active pool is too small. A missing table or row reward Article is omitted before planning; if only the BASE table is available, the 36 BASE recipes continue, with a maximum of 43 when both tables are present.

After Extractor allocation, all unused canonical identities return to World Loot's compatible singleton Article pool. Assigned identities are reserved there so they cannot appear twice in the same generated inventory. If Extractor is disabled, it contributes no extra Booster overlay and existing World Loot behavior is retained.

### 4.11 Locked content

Important is not a blanket exclusion. `data/reward_pools/current/important_item_exclusions.json` is authoritative for exact Key Article identities and explicitly postponed quest/flag Important identities. Each excluded identity is removed from candidate inventory and its vanilla World or Shop source location is kept unchanged. No name or path heuristic is used to infer Keys. Other structural protections still apply, including unsafe packages, unresolved location types, scripted/small-scenario actors, and unsupported multi-reward bundles where a safe pool-preserving patch is unavailable.

When an excluded Important Article appears in a multi-reward World location, the whole location remains vanilla. Shop slots with an excluded identity remain vanilla. Eligible Important rewards can otherwise participate in the ordinary Article pool and Shop/World mixing.

---

## 5. Seed behavior

### Deterministic contract

For the same randomizer app version, `RANDOMIZER_ALGORITHM_VERSION`, game/content
fingerprint, effective options, and normalized seed, the canonical assignment
manifest and Spoiler assignment sections must be byte-identical. The spoiler
header also contains an informational local creation date and per-category
assignment/self-map counts; those fields do not affect planning or its hashes.
Install paths, cache roots, output paths, usernames, timestamps, worker
completion order, and filesystem enumeration order are never inputs to
planning. Different content fingerprints (including different installed
package availability or catalog bytes) are not expected to produce identical
assignments.

Planning uses `StableRandom`, an application-owned SplitMix64 generator. A
SHA-256 digest of the fixed `cv2-randomizer-prng` prefix, algorithm version,
and explicitly supplied seed/domain material initializes its 64-bit state.
Bounded draws use rejection sampling; shuffle uses descending Fisher-Yates;
sample uses partial Fisher-Yates. These algorithms and their test vectors are
part of the compatibility contract. Randomizer planners must not use
`random.Random`, a module-global RNG, Python `hash()`, or runtime/library RNG
behavior. An intentional change to the generator, seed normalization, or
assignment semantics requires an algorithm-version increment; UI-only changes
do not.

Each feature or intentionally shared pool derives an isolated, named stream
from the normalized master seed. One feature therefore cannot consume or shift
an unrelated feature's stream. Pools that intentionally share rewards (such as
Shop/World when enabled) use a distinct stream for the effective combined
configuration. Before every shuffle, sample, choice, or pairing operation,
destinations and candidates are ordered by canonical internal identity.

Canonical sorting is locale-independent: normalize Unicode to NFC, compare
case-folded identifiers, and use the normalized original spelling as a stable
tie-break. Unreal package paths normalize backslashes to forward slashes before
comparison. Display/localized names and absolute filesystem paths are not pool
identities. Sets and maps may be used for membership and deduplication, but
order-sensitive processing must sort their contents first. Concurrent
discovery may complete in any order; its results are canonicalized before
planning.

Feature-specific safety and pool boundaries remain unchanged: Blood Codes stay
in their own pool, and Shops, NPC Traders, Enemy Drops, Boosters, Extractor,
Healing, Initial Inventory, one-time enemy rewards, and Defensive Gadget grants
retain their documented eligibility and metadata rules. Package availability
is resolved before pool construction; catalog class metadata is not an
availability gate.

### Seed input

Allow:
- numeric seed
- text seed
- blank seed -> generate random seed

Normalize the seed to a stable internal integer/hash.

Display and save the exact seed used.

### Seed manifest

Every generation creates a canonical `assignment_manifest.json` outside the
packaged mod, containing the seed, app and algorithm versions, effective
options, game build, content/config fingerprints, and canonically ordered
assignments. Its `assignment_manifest_sha256` is the SHA-256 of the canonical
JSON document. Successful Randomize retains it alongside `Spoiler.txt`
in the app output directory; non-install generation leaves it in that run's
output workspace. It contains no user paths or timestamp.

The ordinary generation manifest may additionally include operational details
that are not reproducibility inputs, such as a UTC timestamp, staged paths,
package names, validation status, and install status. The deterministic
assignment manifest is the comparison artifact between users.

Every generation manifest includes:

- randomizer name and version
- data/catalog revision and detected game version
- seed and selected options
- UTC generation timestamp
- per-pool assignment, locked, skipped, and error counts
- staged output paths and package filenames
- package validation and install status; when installed, the relative install directory and owned filenames
- optional spoiler-log filename

The `Spoiler.txt` begins with the seed, local creation date, total assignment
counts, and the number of assignments that retain their vanilla identity. It
then reports the app version and whether NPC Trader affinity tiers and all six
Booster slots were unlocked, followed by the game build, algorithm version,
config fingerprint, content fingerprint, and assignment-manifest hash. Sections and
changed assignment lines use canonical stable ordering. Each line is
`destination: vanilla item -> assigned item`, so searching an item name shows
both the location it left and the location where it appears in this seed.
The bundled `data/spoiler_item_names.json` provides verified English Article
names and is loaded once per process; Generate performs only local in-memory
lookups and never extracts assets or uses the network to write the spoiler.
Unknown Articles fall back to a readable asset label until an exact name is
verified and added to the catalog. Curated quest/shop destinations use their
friendly labels; unnamed map pickups use a compact stable world-cell/export
reference.
Unchanged assignments are omitted from the location list, and mirrored map
variants produce one line for their canonical logical destination. If the same
Article has multiple independent vanilla source occurrences, each occurrence
remains distinct and is represented separately; this preserves the physical
source multiset rather than deduplicating by Article name.

Spoiler lines map every changed logical destination to its exact assigned payload. Shop rows keep the destination slot identity. Blood Code entries identify the destination grant and assigned Blood Code; Booster entries identify the destination mastery row and assigned StoryFlag, with the mastery owner/rank noted; Defensive Gadget rows identify the confirmed NPC grant and canonical gadget identity, with pending grants explicitly listed. World Loot rows label canonical gadget, Jail, and Booster candidates. Manifest counts include assignments, changes/self-maps, pool boundaries, patched packages/rows, reopened-output status, per-category candidate injection/deduplication, shared overlay displacement, alias normalization, protected Jail source identities, and pending grant status.

---

## 6. Game version support

Randomization is asset/version sensitive.

The app must identify the installed game version before patching.

Maintain version definitions such as:

`data/versions/<version_id>.json`

Each version definition may contain:
- executable hash/version
- supported catalog revision
- required asset paths
- known package/container information
- expected signatures for critical shop tables
- expected parser data

An exact definition match is the only way a build becomes officially supported. A valid installation with an unrecognized executable may be used in the desktop app's local-validation workflow, but it remains `local-unverified` and is never added to `data/versions` automatically. Its manifest records `version_validation: local-unverified`. The reusable extracted cache is bound to installed game archive contents; app/catalog/parser revisions do not create a new copy of the extracted source assets. Local generation requires a completed cache. The user must manually validate gameplay before the build is added to the official support list. Package parsing and patching still fail closed on structural mismatches; absent catalogued source packages are skipped.

Catalogued item, map, Shop, and quest packages are optional unless the version definition explicitly lists them as core requirements. Extract records absent optional packages for that installation; Generate filters individual rewards, locations, and alternate routes against the installed inventory. Missing DLC or version-specific assets therefore do not invalidate an otherwise valid cache or block unrelated randomization. Re-extract only when the installed archive fingerprint changes, required core data is missing, or cache integrity/format checks fail.

Known executable MD5 values may provide a friendly game-version label in Diagnostic and beside the Extract Game Data status. A ready local-unverified cache displays `Game Data: Ready (local validation, vX.X.X)` when the executable matches a known display hash, or `Game Data: Ready (local validation)` when it does not. A ready officially supported cache displays the detected version. This label is display-only: it does not make an unverified build officially supported, change the `local-unverified` identity, alter the cache key, or change extraction/generation behavior. Unknown MD5 values continue to display `Local Validation (unverified)`.

Never silently use offsets/signatures from another version or treat a local-unverified build as officially tested.

A successful parse, serialization, or local-unverified package build is not a game-version signature. Blood Code and Booster catalogs are fingerprint-bound development data; do not register an official supported version until its executable/catalog signature is explicitly tested. Local-unverified generation is permitted only through the desktop app after extracting and validating the current install into its own hash-bound cache; it is a manual-validation workflow, not official support. Local-only package tests remain separate from the supported-version list and from the game installation.

---

## 7. Asset acquisition

The app must not distribute original copyrighted game assets.

The desktop flow separates extraction from seed generation:

1. On startup or game-path change, a background detector checks the installation and executable. Exact signature matches use their official version definition; valid unmatched builds receive a `local-unverified-<executable SHA-256>` identity for local validation only. Both paths inspect the same persistent vanilla cache and reuse it when the installed archive fingerprint still matches.
2. `Extract Game Data` inspects the installed IoStore inventory and uses the existing `IoStoreAssetExtractor` to collect known sources from the central `_known_source_packages` registry. Official builds also require their version-defined core packages; local-unverified builds treat catalogued packages as optional and skip packages absent from that install.
3. Recursive generated-Blueprint dependencies discovered during extraction are prefetched into that same cache. Extraction also writes a compact `world_loot_dependency_index` containing map/export identities and the Blueprint parent package closure. Generate consumes that index to stage World Loot maps and required Blueprints without repeating static map-class and parent discovery. When Renewable Ingredients is enabled, Generate reuses validated class profiles and stages only the reward-owner Blueprint packages needed for CDO edits; it does not stage ingredient maps.
4. A new cache is built under an isolated rebuild directory, hashed and validated, then atomically promoted to the single `extracted/READY` folder. Re-extract replaces the current cache only after validation; a failed extraction keeps the previous valid cache. A legacy content-addressed cache is adopted when its archive fingerprint and package manifests validate, then redundant recognized cache entries are removed.
Retoc may need Oodle to read the installed game's compressed IoStore archives. The resolver checks the detected executable directory and known game-install locations first, then the app-local `tools/oo2core_9_win64.dll` fallback. It validates and exposes the selected directory only to the Retoc child process. The Windows release builder includes the locally supplied DLL under `_internal/tools` and records its hash; the source copy remains excluded from source control. If no copy exists, the error reports every checked path. The pinned `to-zen` output remains uncompressed and does not use Oodle for packaging.
Extract worker policy: expensive work uses `MemoryBudgetScheduler`, which uses a parallel-worker reserve of the greater of 1 GiB or 15% of physical RAM, capped at 3 GiB, and admits additional jobs based on current available RAM, active reservations, package size, and observed per-package peaks. RAM never blocks the only worker: if the estimate/reserve cannot support another concurrent process, the scheduler runs one job at a time and does not reject it on a memory timeout. The worker ceiling rounds nominal 8/16/24/32 GB machines to at most 1/2/3/4 workers; available RAM may lower that count but never below one. These policies are unit-tested with simulated memory, while a full Extract on low-memory hardware remains unverified; a true Windows commit exhaustion can still make the helper itself fail. A bounded post-single-pass sample of 96 maps measured 27.94 s at 2 workers and 21.66 s at 4, with zero fallback packages; requesting 6 was physically capped at 4 and took 22.63 s. Peak sampled process-tree working set was 2.15 GiB at 2 workers and 2.71 GiB at 4; minimum system-available RAM remained 14.72 GiB at 4. The 4-worker ceiling is therefore enabled while dynamic reservation remains the controlling parallelism guard. Map-class inspection, Blueprint-parent resolution, and preparation are sent to short-lived Release helper processes in bounded package batches, with a 2 GiB .NET heap limit. Each helper gets a byte-verified private copy of `CV2.usmap`, because UAssetAPI opens a shared mapping path exclusively on Windows. This releases object graphs at process exit instead of retaining parsed maps across the Extract scan. Process and system memory samples, helper heap/working set, active jobs, phase, elapsed time, and minimum available RAM are written to the app cache diagnostics; the main UI receives only normal phase progress.
5. `cache_info.json` records the original install/version metadata, archive fingerprint, source sets, package inventory, and cached file hashes/sizes. The archive fingerprint is the reuse boundary; app-derived indexes are used only while their catalog/helper/preparation revisions match. Stale indexes are ignored so Generate can use its structural fallback.
6. Every Randomize request requires the validated cache entry. It checks the cache identity, derives availability from its package inventory, creates a per-seed workspace, and materializes only packages selected by enabled features into that workspace.
7. Deterministic plans and reservations are built from those clean vanilla copies; patching, staged-output validation, Retoc packaging, and optional installation use the existing services.
8. Every successful non-dry-run generation writes a player-facing `packaged/Spoiler.txt`. Randomize copies it to `Paks/~mods/Spoiler.txt` with hash-tracked ownership alongside the three containers and verifies the installed files. It does not retain a spoiler in the app output root; recognized old app-generated output copies are removed, while unknown files are left alone. Recognized app-owned generation workspaces are then cleaned. Removal only deletes the installed spoiler if its hash still matches app ownership. Failed runs retain their failure log and never touch the READY cache.

Do not extract the entire game. The source registry includes all currently supported catalogued inputs, not every package in the installation. Required core package absence fails extraction. Missing optional packages are recorded and skipped; Generate filters affected rewards or destinations against the installed package inventory so one absent item does not block unrelated content. Content-class labels remain metadata and do not substitute for package availability.

The persistent cache lives under the app-owned `CacheManager` directory (`platformdirs.user_cache_dir(APP_NAME) / "extracted" / "READY"`), not in `workspace/research_assets/` or an OS temporary folder; it is one replaceable folder. It is immutable input: `CachedAssetExtractor` has no fallback to extracting from the game during Generate, and copies selected files into an isolated workspace. Catalog Article paths may encode Unreal FName `Number` values separately from the literal path; cache lookup resolves a base path only when the installed inventory has one unique numbered variant. For World Loot, Extract builds a deterministic legal destination-name universe from eligible World rewards, finite Shop products, confirmed FieldEnemy rewards, canonical World Articles, eligible Gadgets/Jails/Formae, and Extractor outputs. It omits Blood Codes, renewable materials, unavailable Articles, protected or unsafe locations, and infinite Shop stock. The helper sorts package/object FName literals (including the required numbered-name forms), appends missing values to supported World Partition map NameMaps, keeps vanilla reward fields and raw `.uexp` bytes unchanged, reopens the result, and verifies export/import identity, NameMap append order, and every vanilla reward. Validated templates and required sidecars live under `prepared-assets`; each versioned binary descriptor is tied to their hashes and rebuilt offsets/indices. A package that cannot pass preparation remains eligible only for the existing structural fallback. Generate uses a cached prepared package when available, then validates package hashes, exact original bytes, FName identity, field layout, and non-overlapping edits before writing fixed-width ArticleDataPath replacements. It never structurally opens an eligible prepared package on this fast path. A missing NameMap name or any failed check selects the safe structural path. Option and seed changes do not invalidate prepared data. App-derived revisions do not invalidate raw extracted assets; stale indexes are ignored. Changed game archive contents, missing current mandatory core packages, or failed cache integrity checks require re-extraction. Structural reopen success does not by itself establish runtime safety.

At startup the detector rechecks the installed build and cache. A `READY` official cache unlocks the randomizer. A `READY` local-unverified cache also unlocks local generation but keeps the explicit unverified status visible; generation requires that cache and the manifest records its validation status. `NOT_EXTRACTED`, `OUTDATED`, and `INVALID` keep generation disabled while game-path/mappings selection, extraction (when the installation is structurally valid), and Clear Cache remain accessible. Re-extraction builds and validates a replacement before swapping it in; Clear Cache removes only the app-owned extracted-data directory and disables generation until extraction succeeds again.

Each official version definition must name at least one mandatory core package in `required_asset_paths`; official extraction/generation fails if any core package is missing. Local-unverified cache builds have no version-specific mandatory core list, so they require at least one catalogued source package to be present and validate every extracted package before promotion. Optional or feature-specific sources remain skippable. `data/content_availability/current.json` and classification reports remain research evidence only; runtime does not use their markers to detect DLC ownership or entitlement.

### Required categories

World loot:
- generated map packages containing supported loot locations

Materials:
- implementation and metadata remain in the repository, but the parked feature is not planned or staged during normal Generate
- generated map packages and/or resource-specific Blueprint packages required by the chosen patch strategy

Shops:
- each selected Shop DataTable whose package exists
- each table is independent; missing optional tables are recorded and skipped

Enemy Drops:
- each of BASE, AFTER_ENDING, and `DT_DropItemData_DLC` whose package exists

Blood Code grants:
- only quest packages listed by the enabled, fingerprint-checked grant catalogs

Mastery Boosters:
- `/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/DT_GainBoosterSmallScenario`

Extractor / Booster Crafting:
- `/Game/Blueprint/GameSystem/ItemSynthetic/Booster/DT_BoosterRecipe`
- `/Game/Blueprint/GameSystem/ItemSynthetic/Booster/DT_BoosterRecipe_DLC` when its package exists

---

## 8. Asset patching architecture

Core UI code must never directly patch assets.

Use service modules.

Enemy Drops, Blood Codes, Boosters, Extractor recipes, Defensive Gadget NPC grants, and mixed-family Shops use the shared IoStore extraction/cache, isolated generation workspace, UAssetAPI helper where needed, staged-output validation, and Retoc packaging path. The Idris's Conceit quest destination uses the same World Loot direct-Article extraction and patch service as other World Loot quest rewards. The explicit Extract action acquires vanilla sources from the user's installed game through `IoStoreAssetExtractor` and stores them in the persistent app cache; normal Generate reads that cache through `CachedAssetExtractor`. The canonical `workspace/research_assets/CV2.usmap` mapping is a required runtime/build resource and must be present in source checkouts; `.gitignore` preserves this path while the rest of `workspace/` holds ignored local research and generated files. Catalogs are metadata, not bundled game packages. Patch services use per-generation copies, verify catalogued source hashes and identities, reopen serialized results, and abort generation on mismatches or partial failures. The Bequeathed Forma source identity is checked against its metadata and current authoritative World/Shop catalogs. The helper requires the canonical mapping. The extraction cache includes all known sources; selected feature packages are materialized into each generation workspace before they are passed to patch services. Extractor and Enemy Drop tables are selected from cached package inventory, so missing optional tables are omitted independently. Extractor changes `CreateArticleDataPath` only when reward randomization is enabled. The cheaper Extractor option changes each recipe ingredient quantity to one while preserving the ingredient list and Haze cost; it reuses the cached Extractor DataTables and writes validated results only to staging. Booster inspection gives UAssetAPI the cached source file directly, while patches are written only to staging. For mixed Shops, UAssetAPI adds missing path names to the destination package header; the existing Python parser and fixed-width `.uexp` patcher encode the `ArticleDataPath` against that destination name map and reparse the result.

For Blood Codes, the shared helper locates catalogued `GainItem` exports and changes only the target reward path. Battle Axe: Idris's Conceit is modeled as a normal World Loot destination at the beginning MagMell quest's `QSTask_GainItem_1`; it uses the generic World Loot quest-Article serializer so its reward joins the same source multiset as other open-world rewards. Absolute Executioner is one logical destination; its randomized Article is also written to the alternate Lyle quest grant so only one reward is obtained along either route. For mastery Boosters, the helper changes the Booster flag token in `EvaluationFormula` together with the Booster `ArticleDataPath`. For Extractor recipes, it changes only the output path when reward randomization is enabled, checking each source row and preserving all other fields after serialization. For Defensive Gadgets, it locates the exact catalogued ArticleBulkInfo entry and changes only its `ArticleDataPath`; all parsed exports are snapshotted and compared after reopening. These operations preserve unrelated fields and validate the reopened exports/rows before packaging.

The current Generate flow is: validate the game and submitted ready-cache entry; obtain package inventory from cache (without Retoc listing or source extraction); enforce mandatory core packages; load relevant metadata catalogs and resolve known packages as `AVAILABLE` or `MISSING`; filter unavailable packages; normalize the seed; create a workspace; copy selected vanilla packages from cache; build deterministic plans and reservations; write a canonical `assignment_manifest.json` with config/content fingerprints and its SHA-256; stop after manifest/Spoiler/optional CSV creation for dry run, or patch staging copies; validate staged inventory; build and verify the `.pak`, `.utoc`, and `.ucas` set with Retoc; write `Spoiler.txt` and the operational manifest; install for the desktop Generate action; verify installed hashes; retain the assignment manifest under the output root; and remove app-owned generation workspaces. Internal engine callers can request non-install output. Renewable Ingredients, when enabled, patch their shared reward-owner CDO packages; placed material map actors are not rewritten. World Loot edits are grouped by destination package. A changed Healing per-instance edit and a World Loot map edit in the same package stay together on the structural path. Other prepared or raw-binary-candidate maps use the cached fixed-width path after complete hash, expected-byte, layout, FName, and overlap validation; the package is copied once and all fields are patched together. Missing names, unprepared packages, unsupported layouts, or validation mismatches go to the existing UAssetAPI structural serializer. Preparation failures are recorded per package and never weaken validation. The structural helper uses bounded batches, runs one helper at a time under low-memory conditions, and adds workers only when available RAM permits; it keeps a 2 GiB managed-heap limit, package reopen checks, and per-package timing/memory diagnostics. Core, staging, patch, and package-integrity failures remain fatal, and partial/invalid output is never installed. `Spoiler.txt` starts with the normalized seed, local creation date, total assignment/unchanged counts, and app version; it lists only changed player-facing assignments by feature. Healing is reported separately while it remains in the shared World Loot pool. The optional `spoiler_log.csv` is diagnostic and is removed after successful installation.

The implemented modules are organized as follows:

- `src/cv2randomizer/ui/` owns controls and dispatches generation to a worker.
- `src/cv2randomizer/core/models.py` stores feature options; `core/seed.py` normalizes seed input.
- `src/cv2randomizer/generation/pipeline.py` orchestrates catalogs, extraction, plans, patching, validation, manifest/spoiler output, and packaging.
- `src/cv2randomizer/game/asset_extraction.py` uses the existing IoStore extractor/cache. Feature catalogs and preparation/patch services live in `game/*_catalog.py` and `game/*_generation.py`.
- `src/cv2randomizer/game/jail_catalog.py` validates canonical player Jail metadata against authoritative World Loot source locations; `src/cv2randomizer/randomizer/jails.py` classifies those physical occurrences for the World Loot pool.
- `src/cv2randomizer/game/article_reward_catalog.py` validates reusable canonical Iris Article reward metadata; `src/cv2randomizer/randomizer/article_rewards.py` helps classify matching physical World source occurrences.
- `src/cv2randomizer/game/defensive_gadget_catalog.py` and `defensive_gadget_generation.py` load the canonical gadget pool, extract confirmed grant packages, and validate staged patches.
- `src/cv2randomizer/game/extractor_catalog.py` and `extractor_generation.py` validate the two recipe table catalogs, coordinate extraction, and reopen staged path-only patches; `game/booster_reward_pool.py` builds the deduplicated canonical identity inventory.
- `src/cv2randomizer/randomizer/extractor.py` allocates Booster identities without replacement; an unallocated identity can join World Loot only when an eligible physical World source occurrence already contains it.
- `src/cv2randomizer/game/bequeathed_formae_catalog.py` cross-checks the canonical Forma identities against World Loot and Shop metadata; the Idris's Conceit grant is validated and patched by the shared World Loot direct-reward service.
- `src/cv2randomizer/randomizer/` contains independent deterministic planners, including `blood_codes.py`, `boosters.py`, and `bequeathed_formae.py`.
- `src/cv2randomizer/randomizer/defensive_gadgets.py` owns the independent NPC reward domain and provides metadata for matching eligible physical World source occurrences.
- `src/cv2randomizer/packaging/retoc.py` verifies and builds containers; `packaging/installer.py` owns local installation/removal.
- `tools/uassetapi-worldloot/` is the shared .NET/UAssetAPI CLI; Blood Code and Bequeathed Forma quest rewards share the `GainItem` patcher, alongside Booster and Defensive Gadget operations.

---

## 9. World loot patcher

### Input model

Each world location should have at least:

- package virtual path
- source package path
- export identifier/index
- actor type
- `UniqueSaveID` when available
- reward list
- protected reason if any

Reward:

- `ArticleDataPath`
- `Count`
- `bNotification`

### Shuffle semantics

A reward tuple travels together:

- `ArticleDataPath`
- `Count`
- `bNotification`

This prevents invalid outcomes such as inheriting a consumable quantity for a weapon.

### Protection

Skip:
- exact Key and quest/flag Important identities listed in `data/reward_pools/current/important_item_exclusions.json`
- flag-change
- small-scenario
- parse failures
- explicitly blacklisted paths

### Patching

V1 may use a silent CLI serialization helper internally if required.

The user must not need to manually open UAssetGUI.

The Python app may:
1. extract package
2. convert/deserialize using a bundled CLI/helper
3. modify the correct reward node
4. serialize back
5. validate output

Long-term, direct Python parsing may replace helpers if reliable.

### Prepared NameMap templates and World Loot binary fast path (2026-10-08)

Generate consumes the extraction-time `world_loot_dependency_index`, so it does not rediscover map classes or Blueprint ancestry for each seed. Prepared maps whose reward fields pass the binary descriptor checks use fixed-width writes and do not open UAssetAPI during Generate. Maps that require structural edits or fail a safe binary check use the existing Release UAssetAPI path, sent in deterministic batches of 32; when available RAM permits, at most four helper processes work on independent batches. Each helper gets a temporary byte-identical `CV2.usmap` copy because concurrent UAssetAPI readers can conflict on Windows' exclusive file lock. The copies and batch manifests are removed after each helper exits. Results are validated and restored to canonical package order before promotion, and progress advances as each batch completes. Any batch failure removes every staged map output. Self-map-only packages are skipped; the seed plan, assignment semantics, and package inventory are unchanged by the fast path or parallel execution.

Direct World Loot rewards are grouped by destination package. The current catalog produced 36 changed assignments in 35 packages: 33 structured quest slots across 32 packages (one package contains two slots), plus three raw CDO slots in three packages. Generate submits the structured slots in one helper invocation across 32 packages and uses one invocation per raw package, for four invocations total. The verified helper work is 70 structural package loads, 35 serializations, and 35 reopen validations. The 35 source packages contribute 70 `.uasset/.uexp` materialization copies. Quest-source fingerprints use the immutable cache manifests; the three raw slots still hash their `.uasset` and `.uexp` source files in the helper, six source-file hash reads per Generate.

Renewable Ingredient edits target Blueprint CDO packages, while World Loot edits target map and reward packages; ingredient edits never overlay or compose changes into a placed pickup map. World Loot writes its patched packages directly into final staged `Content`, and every CDO override is staged and validated as its own package.

Subsystem outputs populate an explicit changed-package registry. Final inventory validation and Retoc consume its exact sidecar list; normal Generate performs no recursive scan of staged Content. The READY cache is trusted for normal materialization after its archive/package integrity checks, and repeated Generate no longer rehashes every immutable cached package. Reuse depends on unchanged game archive contents and present current core packages; app revisions to `CV2.usmap`, catalogs, source registries, and helper/preparation logic do not invalidate raw extracted files. Stale World Loot indexes are discarded, and Generate uses its structural path.

Extract prepares the legal World Loot destination-name universe across all supported option combinations, sorts NameMap literals deterministically, and stores prepared map templates plus per-package NameMap indices and fixed-width reward descriptors in the persistent cache. This is a pool-specific universe: 1,303 Article package-path spellings / 2,606 NameMap literals, drawn from eligible World Loot/direct grants, eligible finite Shop/Trader stock when Shop/World Mix is enabled, canonical World Articles, eligible FieldEnemy rewards, Gadgets, Jails, Bequeathed Formae, and Extractor outputs. Blood Codes, protected key/quest-flag identities, renewable materials, unsafe locations, and infinite, uncertain, or protected Shop entries are excluded. The cache does not add every game Article to every World Loot map.

The current prepared-index metadata uses format 5, source registry `catalogued-sources-v22-prepared-world-namemaps`, descriptor revision 13, preparation revision 14, and binary patch version 3. The source cache itself remains a single reusable `READY` folder across app revisions. The revision-13 Extract described below is the pre-single-pass baseline. The current revision-14 Extract inspects 1,454 map packages; 1,134 World Loot maps receive NameMap preparation and 116 Healing maps receive combined Healing/NameMap preparation. Those 116 map rows plus 116 independent Healing clone templates represent 58 unique pickup identities (46 Golden Nectar and 12 Golden Blood Bead), each appearing in both `MainWorld` and `DlcWorld` with the same `unique_save_id`. World Loot planning shuffles canonical destinations once and copies each assignment to its mirror; the fixed-seed manifest confirms matching assigned Articles across all 58 pairs. If Extract cannot prepare a package or a descriptor/package identity, hash, expected byte, NameMap index, field layout, bounds, or overlap check fails, Generate uses the existing structural serializer or fails safely. A Healing map and all its clone descriptors are preflighted as one unit before any binary output is committed.

During the fixed-seed all-options Generate benchmark, 1,108/1,108 prepared World Loot maps were patched through direct fixed-width binary writes with zero missing assigned names; the 77 Healing-overlap maps used structural serialization. The two runs produced the same assignment manifest and Spoiler hashes, the same 1,510 package paths, and Retoc `verify` returned `verified`. Compared with the immediately preceding prepared-cache revision, median Generate time fell from 253.37 s to 238.46 s and median World Loot patch time fell from 156.19 s to 137.38 s. Mean sampled process-tree CPU use fell from 10.13% to 6.84% of all logical processors, and peak sampled RSS fell from 5.84 GiB to 3.36 GiB. The prepared cache grew by about 74.9 MB in total, including the persistent per-map NameMap lookup tables. These are local measurements; the cache revision intentionally changes the content fingerprint, so cross-revision full-manifest hashes are not expected to match. The World Loot assignment bundles grouped by destination package did match the preceding benchmark. This paragraph records the earlier cache revision; current Healing preparation and measurements are recorded below.

The 2026-10-09 Healing preparation pass completed a real Extract with revision 11: 116/116 serialized Healing map rows and 116/116 isolated clone templates prepared, zero preparation failures, and all 464 cached `.uasset/.uexp` files present with matching size/SHA-256. The 116 rows represent 58 unique physical pickups (46 Golden Nectar and 12 Golden Blood Bead), each mirrored between `MainWorld` and `DlcWorld` under a shared `unique_save_id`. Two warm all-options Generates used seed `6800008187299365132`, installation disabled, and the same current READY cache. Each produced the same manifest, Spoiler, content/config fingerprints, 1,504 package paths, and `retoc verify=verified`. 110 changed Healing reward records were written through 110 prepared clones; 114 maps were binary-written because four also carried non-Healing World Loot changes, and two self-mapped records needed no output write. These counts refer to serialized representations, not unique physical pickups; the saved fixed-seed manifest has 55 changed pickups and three self-maps, with matching Articles across all mirror pairs. The available pre-preparation shared helper call processed 112 maps/113 assignments in total: 110 Healing maps/assignments plus two generic Treasure Box maps/three assignments. Its Healing package-processing time was 32.61 seconds; current Healing structural fallback is zero, while the two generic Treasure Box maps still use 1.74 seconds of the shared helper. Median Generate was 96.33 seconds versus 149.21 seconds in that later available baseline, and World Loot patching was 19.73 seconds versus 73.45 seconds. The before and after content fingerprints and assignment/Spoiler hashes differ, so this is a directional performance comparison, not proof of same-output equivalence across revisions. Structural, binary-readback, and Retoc checks passed. No installation or `~mods` changes were made.

The fixed-width path is specific to prepared World Loot reward fields. It does not generalize to arbitrary RawExport, direct quest/CDO edits, or Healing's materialization/redirect operation. Parser reopening and Retoc verification do not establish runtime safety for every map. The user previously reported that a recognizable Magmell Glowing Haze pickup became an Uncanny Stone; broader gameplay coverage of the final refreshed cache remains a manual validation item. No game installation file or `~mods` content is modified during Extract or Generate.

#### Extract performance profile and remaining work (2026-10-09)

A complete installed-game Extract with revision 13 took 816.737 seconds (13m36.737s), versus 1,190.187 seconds (19m50.187s) in the available earlier profile: 373.450 seconds / 31.38% less wall time. The profile's sampled exclusive phases cover the run within 0.006 seconds. The main spans were Retoc (255.93 s), World Loot NameMap preparation (202.32 s), map-class inspection (95.40 s), Healing template preparation (76.32 s), and helper work (45.74 s). Exact stage measurements are nested in those phase spans and are published in `workspace/extract_performance_profile.md`; `workspace/extract_performance_before_after.md` explains the comparison limits.

The inventory index covered 216,218 installed package paths. Source registries made 3,232 package requests; canonical deduplication removed 365 duplicates and left 2,867 distinct paths. Retoc ran 26 batched `to-legacy` calls and committed 2,794 package outputs; the READY cache contains 2,794 inventory entries after static dependencies were resolved. The run used 240 Release helper launches in package batches, with up to three heavy workers. Prepared work serialized 1,134 World Loot NameMap maps, 116 combined Healing/NameMap maps, and 116 per-location Healing templates. Per-package records are in `workspace/extract_slowest_packages.csv`.

The revision-13 baseline did not establish that Extract opened each map only once: 1,454 maps were class-inspected, and 1,250 were reopened for NameMap or Healing preparation. The focused single-pass work below combines those operations while retaining output metadata parsing and final safety validation. Feature catalog scanners remain timed as one shared source-plan stage, not separately by subsystem.

Two same-seed all-options Generates then ran with installation disabled using the final READY cache. They matched assignment manifest, Spoiler, content fingerprint, and 1,504 package paths; both Retoc verifies passed. The World Loot binary patcher handled 1,112/1,112 eligible candidate packages with no World Loot structural fallback. Healing changed 110 mirrored rows via prepared descriptors; the two structural fallback packages were unrelated Treasure Box instance overrides. The physical Healing total remains 58 pickups (46 Nectar, 12 Bead), serialized as 116 mirrored map rows (92 Nectar, 24 Bead). The two-run median end-to-end Generate task wall was 121.031 s and median generation-pipeline time was 120.960 s. The earlier 96.33 s timing belongs to another prepared-cache fingerprint and is not a controlled same-input comparison. The Extract CPU monitor averaged 9.60% of total machine CPU (~1.54 logical cores); the earlier profile has no comparable CPU data.

#### Single-pass map discovery and preparation (2026-10-09)

During one map source-open lifetime, `PackagePreparationContext` now combines catalog-export class discovery, the 2,606 canonical destination NameMap literals, binary field descriptors, and Healing locations. The global candidate-name set is normalized and sorted once before package preparation. NameMap and Healing helpers return class results and descriptors from that same source UAsset open. The 204 maps without preparation requirements stay read-only; 1,134 NameMap maps and 116 Healing map rows are serialized into prepared assets. Healing clone templates remain separate per-location package outputs.

The baseline had 1,454 discovery opens, 1,250 additional preparation opens, and 77 Healing descriptor-only map reads. The revision-14 Extract has 1,454 initial map opens, zero preparation reloads, and zero descriptor-only rereads. Intermediate metadata parsing and final safety validation remain enabled for every prepared map (1,250 of each). Including these output checks, map structural opens fell from 5,281 to 3,954 (1,327 / 25.13% fewer); before output validation, source/discovery/preparation opens fell from 2,781 to 1,454 (47.72% fewer). The old inspection, NameMap, and Healing stages took 414.505 seconds combined; final four-worker single-pass discovery/preparation took 283.542 seconds, saving 130.963 seconds within those stages. A preceding three-worker single-pass run took 329.345 seconds.

The final real Extract completed in 670.532 seconds (11m10.532s) compared with 816.737 seconds (13m36.737s) for the revision-13 baseline, saving 146.205 seconds (17.90%). It used up to four heavy workers and 80 helper launches, down from 240. Peak child working set changed from 4.658 to 4.935 GiB; peak Python working set changed from 0.986 to 1.170 GiB and peak helper working set was about 2.024 GiB in both runs. Minimum available RAM was 11.953 GiB in the baseline and 11.523 GiB with four workers. The Extract profiler does not capture comparable CPU utilization or disk-throughput samples. Blueprint class records remained 5,949 target exports and dependency resolution remained 62 unique package paths over three rounds; package paths are deduplicated before Blueprint extraction. The profiles do not expose class-cache hit/miss or CDO-lookup counters, and no repeated Blueprint-package loads were found in helper records.

Two all-options Generates with seed `6800008187299365132`, install disabled, used the final revision-14 READY cache and took 117.635s / 119.086s. Their assignment manifests, Spoiler files, content fingerprints, all 1,504 package paths, and SHA-256 hashes for all 3,008 staged inputs match. Retoc `info` reports `compression_methods: []`, `list --path` contains 1,504 packages, and `verify` passes for both. The World Loot binary path patched 1,112/1,112 candidate packages with zero structural fallbacks. Healing's prepared path handled 110/112 instance-override map packages and 110 isolated clones; the two remaining packages are generic Treasure Box overrides without Healing descriptors and correctly retain the structural fallback. The 2,644 active source-occurrence IDs were unique and the planner's Article-multiset validation passed. A same-seed structural-fallback Generate from before the final helper-matched Extract has the same 5,879 logical assignment entries; its manifest differs only in `content_fingerprint`, and its Spoiler differs only in the corresponding manifest-hash and content-fingerprint headers. Prepared NameMaps and serialization change the package bytes but not the randomization plan. Retoc container bytes are not guaranteed bit-for-bit stable. This is a correctness/payload regression check, not a controlled Generate throughput comparison. No installation, game archive, or `~mods` files were changed.

Required profiles are in `workspace/extract_single_pass_profile.json`, `workspace/extract_single_pass_profile.md`, `workspace/extract_single_pass_before_after.md`, and `workspace/extract_single_pass_slowest_packages.csv`; the bounded worker experiment is in `workspace/extract_worker_sweep.md`. Static dependency and prepared-cache generation (321.910s) is now the largest exclusive Extract stage, followed by map discovery/preparation (283.542s) and required/optional source extraction (278.960s). Any later pass should first split the static dependency/cache stage and profile serializer/readback and source-I/O phases independently, while retaining exact-byte checks, final validation, atomic cache promotion, and memory-aware scheduling.

#### Historical isolated probes before full integration

A one-package A/B probe was prepared around an early-route actor logged by UE4SS. Review of the exact log line shows it is a `SetCanInteract` interaction-area event, which proves proximity but not collection. The map field for that actor parses as `DA_Item_AtkUpPhysical_1`; however, the user reported seeing Glowing Haze 1 in Case A. The mismatch means the physical target has not been confirmed. If the same exact pickup can be collected again from a pre-pickup save, Case B (`DA_Item_AmmoRifle`) can correlate whether that object is the patched actor; otherwise wait for a grant-level trace or a visibly identified target. Case A/B containers pass Retoc verification, but gameplay validation remains pending.

The separate Magmell `Shop_MM_Secret1[0]` diagnostic (`DA_SingleSword_IronPipe` -> `DA_SingleSword_Machete`) was installed by the user and the Machete appeared in-game. This confirms that the isolated `DT_ShopLineup` container and the row patch are applied at runtime. It does not test NameMap expansion: the Machete FNames already existed in the destination table's NameMap.

A follow-up research probe replaced that same Pipe slot with Golden Nectar, whose package and object FName literals were absent from the vanilla Shop table. The existing Shop serializer appended exactly those two entries (retaining FName Number 2), the patched table reopened, the one-package Retoc container passed verification, and the user confirmed the Nectar appeared in-game. This passes runtime validation for Shop DataTable NameMap expansion only; it does not establish safety for prepared World Partition maps or enable NameMap preparation in production.

A separate World Partition probe prepared three ordinary Ground Pickups in the same two Magmell map cells as the previously tested Nectar locations. It appended the FName literals required by three distinct test rewards, patched only fixed-width ArticleDataPath fields, and kept each Count/notification unchanged. The maps reopened and validated during NameMap preparation; afterward their RawExport fields were checked by exact-byte readback and resolving serialized FName indices against each reopened NameMap. Retoc contains exactly the two maps and passes verification. This historical isolated probe is superseded by the full cache/Generate validation above; broad gameplay coverage remains pending.

---

## 10. Renewable Ingredient reward patching

Renewable Ingredients are an optional, separate pool. The catalog records physical map locations for audit and spoiler reporting, but randomization changes the reward on each shared pickup Blueprint CDO. The planner permutes reward Articles by original ingredient type, so every occurrence of one type gets the same replacement, even when that Article is owned by multiple Blueprints.

Generate stages and validates each changed reward-owner Blueprint, registers that package's class schema with the serializer, patches only the audited `ArticleBulkInfo.Articles[0].ArticleDataPath` on its CDO, then reopens the result and verifies the reward tuple, untouched exports, and sidecars. Conflicting ownership, ambiguous CDOs, or incomplete validation fail before packaging.

Ingredient randomization never rewrites placed actor `ClassIndex` or `TemplateIndex` references, map `.umap`/`.uexp` payloads, reset behavior, interaction behavior, location, or save identity. One-time pickups containing the same Article stay in their normal pool.
- export serial sizes, offsets, and sidecar bytes

---

## 11. Shop binary patcher

This can be implemented directly in Python.

### Known structure

Shop tables are cooked DataTables whose row struct is:

`ShopLineupTableRow`

The GUI tools may show row data as Raw Data, but the binary is parseable.

The parser must:
1. parse the package name map
2. identify DataTable row boundaries
3. identify product entries
4. resolve `ArticleDataPath` FNames
5. read:
   - Price
   - bIsUseStock
   - Stock
6. associate the product with its shop row
7. validate all values before patching

### Shuffle payload

Shop-only mode moves Article identity only. It preserves destination `Price`, `bIsUseStock`, `Stock`, unlock/evaluation condition, shop row, and slot index. Only proven finite slots are eligible, and their stock bytes remain unchanged.

Shop/World mode moves only the source `ArticleDataPath`; Shop destination price, stock, unlock/evaluation, row, and slot fields remain unchanged. Infinite and uncertain slots do not enter this pool. World destination Count, notification, actor, and location fields remain unchanged as well.

### Safety

Do not patch fixed byte offsets without first verifying the package structure/signature.

Offsets discovered during development are useful tests, not the final parsing API. Shop patching never materializes or rewrites stock fields; only the Article identity can move, while all other product fields must match the source values.

The final parser should dynamically find product records and validate the expected original item before writing.

### Article-only Shop/World patch

The cross-system mode writes only the 20-byte serialized `ArticleDataPath`. It preserves the destination's `Price`, finite stock, evaluation condition, and remaining row bytes, then reparses the table and verifies the assigned path/FName numbers and unchanged fields. Shop-only and NPC Trader modes use the same destination-stock rule. The patcher rejects any assignment whose source or destination is infinite, protected, or stock-uncertain.

---

## 12. Packaging

Generated assets preserve their original virtual paths.

Example:

```text
CodeVein2/
  Content/
    Map/
      MainWorld/
        MainWorld/
          _Generated_/
            HASH.umap
            HASH.uexp
```

Shop example:

```text
CodeVein2/
  Content/
    Blueprint/
      GameSystem/
        shop/
          DT_ShopLineup.uasset
          DT_ShopLineup.uexp
```

The app must invoke a packaging CLI/helper automatically.

V1 uses the pinned `retoc_cli 0.1.5` `to-zen` command with `UE5_4`. Copy only validated staged assets under a temporary `CodeVein2/Content/` mount tree, then write `<name>_P.utoc`. Run Retoc from a temporary copy of its executable with the Windows system search path only; packaging must not look up or inject Oodle. The helper emits the matching `<name>_P.ucas` and `<name>_P.pak`. Before promoting the three files, run `retoc info`, `retoc list --path`, and `retoc verify`; require an empty compression-method list and compare every listed path against staged `.uasset/.umap` files under `../../../CodeVein2/Content/`.

Expected generated mod files:

- `<name>_P.pak`
- `<name>_P.utoc`
- `<name>_P.ucas`

The app should not require the Code Vein 2 Modding Framework GUI.

During development, the exact packaging command used by the existing framework may be reused/reproduced.

---

## 13. Local installation

Default install target:

`CodeVein2/Content/Paks/~mods/`

The app should:
- create `~mods` if absent
- use a unique predictable mod name, e.g. `CV2Randomizer_P.*`
- record the installed game path and SHA-256 hashes of the three owned containers in app state outside the game folder
- remove only files whose current hashes still match that ownership record; refuse removal of externally changed files
- reject unowned install collisions by default; Randomize may replace matching Randomizer filenames only after explicit confirmation and an app-data recovery backup
- roll back partial updates/removals
- leave vanilla archives, unrelated mods, and the `Paks/~mods` directory itself untouched
- expose the complete "Randomize" action in the UI; the engine retains reusable non-install generation support for tests and internal workflows
- expose a remove operation for the currently installed randomizer mod

The install/remove backend and UI actions are implemented. Before Randomize, the user confirms replacement of files named `CV2Randomizer_P.*` and `Spoiler.txt`; this permits adopting a prior install that predates the ownership record. Any matching existing files are backed up under app data before replacement, and unrelated mod filenames remain untouched. Calls without explicit confirmation continue to reject unowned files. In-game validation remains feature-specific: a successful package install or load does not establish that every randomized reward behaved correctly.

---

## 14. Workspace and generated files

Never patch source assets in place.

Runtime generation workspace (temporary):

```text
<app-data>/workspace/generated/<timestamp-seed-hash-id>/
  extracted/       # feature intermediates, including modified world-map inputs
  source/Content/   # source package copies materialized as needed
  staging/Content/  # validated virtual-path tree passed to Retoc
  packaged/         # generated .pak/.utoc/.ucas and temporary Spoiler.txt
  logs/
  manifest.json
  spoiler_log.csv   # optional diagnostic log
```

The persistent vanilla source cache is separate under the app cache directory at `platformdirs.user_cache_dir(APP_NAME) / "extracted" / "READY"`; this single replaceable entry includes `assets/` raw source packages, prepared subdirectories, and `cache_info.json`, and is reused while installed archive contents match. Raw identity is tracked independently through `RAW_EXTRACTION_REVISION` and the game archive fingerprint. Catalog, parser, helper, descriptor, and preparation revisions form the prepared-cache identity through `PREPARED_CACHE_REVISION`; updating prepared logic does not invalidate unchanged raw assets. If the raw cache remains valid while prepared metadata is stale, the UI offers `Update Prepared Data`: it stages a replacement, hardlinks immutable `assets/` files into that staging tree, and regenerates prepared data there. Cache promotion remains atomic; the READY entry is replaced only after all source and prepared data validate. `Re-extract Game Data` continues to perform a full source extraction, and archive/raw-format changes or missing mandatory sources require a full Extract. The installed package inventory is separately persisted as a compact gzip index under the extracted-cache root, keyed by the complete installed-archive fingerprint, exact container-name set, inventory schema revision, and Retoc version. A mismatch or unreadable index is a cache miss and Retoc rebuilds it; it never substitutes for validation of required extracted source assets. Package inventory lookups are case-insensitive, but extraction retains the exact path spelling reported by Retoc because `to-legacy --filter` is case-sensitive. A catalog path with an FName Number may resolve to one unique physical `_N` package; extraction then uses that physical package's exact spelling. Research material under the repository's ignored `workspace/research_assets/` is separate from both runtime generation workspaces and the source cache.

After a successful Randomize, the app installs the generation's `Spoiler.txt` beside the mod containers as `~mods/Spoiler.txt` and records its hash for safe replacement/removal. The previous `CV2Randomizer_Spoiler.txt` is removed during upgrade only when the installed ownership record and hash confirm that it is unchanged; modified legacy files are preserved and block migration. The app does not retain a second spoiler in the output root and removes a prior copy only when its header matches the app's generated format. It verifies installed files against their expected SHA-256 hashes, then removes bulky generated assets from recognized app-owned generation workspaces while retaining each workspace's `logs/` contents. The small ownership marker and log directory remain under `output/generated/<run-id>/logs/`, so diagnostics survive successful cleanup and later generations. A legacy saved output directory is honored only when its canonical path remains inside the app-owned data directory; an external path is ignored. The extraction cache and its indexes live outside that cleanup subtree and are retained for later seeds. Failed runs retain their diagnostic workspace and failure record; cleanup never traverses the cache, application resources, catalogs, tools, settings, or game installation.

Re-extract the cache when:
- installed game archive contents change
- a current mandatory core package is absent from the cached inventory
- cached source package integrity validation fails

When only prepared-cache identity changes and the raw cache still validates, use `Update Prepared Data`; this rebuilds the derived tree from the existing raw source packages. `Re-extract Game Data` stays an explicit full rebuild.

---

## 15. UI design

Use PySide6.

The provided `docs/ui_reference.png` is a visual reference only.

Desired style:
- traditional desktop utility
- compact and clear
- functional rather than overly stylized
- information-dense without looking cluttered
- native-feeling controls
- tabs/group boxes/check boxes similar in spirit to classic randomizer tools
- do not copy the screenshot exactly

### Main window

Top area:

- Game Path
  - detected installation path
  - Browse button
  - validation status
- Seed
  - text field
  - Copy button: copies the current seed to the clipboard
  - Random button
- buttons:
  - Randomize
  - Uninstall Randomizer
  - Open Install Folder

Main tab: `Item Randomizer`

Groups:

The window and Windows executable use `ico/icon.ico` as their app icon. The About tab uses `ico/icon.png` as its display icon; the release bundle includes both files. The build embeds the ICO into the executable for the title bar and taskbar icon. The window uses a neutral light-gray Fusion palette, subtle borders, and compact Windows-style controls. It opens at 1000x500 with a 900x500 minimum. Game Path/Browse and Seed/Copy/Random share one row; Copy writes the current seed text to the clipboard. Below the options and above Status, one action row places Extract/Re-extract at the far left, the Game Data readiness text immediately to its right, and Randomize, Uninstall Randomizer, and Open Install Folder at the right. Text fields and buttons use a shared 30-pixel height. Checkboxes have a dark outline and visible check mark; disabled controls such as Protection use muted text and a faded indicator. The Extract button is highlighted while the cache is not READY and becomes subdued while disabled. Every app launch generates and displays a fresh seed, while the field remains editable and a manual seed is used exactly as entered. The Game Path example is placeholder text only and is never treated as a selected path. Randomizer options remain available while game data is extracting or being validated. Seed controls, `Randomize`, and `Uninstall Randomizer` remain disabled until the matching cache is ready; game-path selection, extraction, and Clear Extracted Game Data remain accessible. Once ready, `Randomize` unlocks and keeps the existing Generate, package, and install behavior. Re-extract and Clear ask before deleting all content under `Cache\extracted\`, including older revisions; cancellation does nothing. Cache removal runs in a worker and validates the exact extracted-cache root before deleting children without traversing links. Re-extraction starts only after a successful full clear; Clear finishes with the UI in `Not Extracted` and never starts Extract. The app resolves and validates the bundled `CV2.usmap` itself; if it is missing or unreadable, it shows an internal application error instead of offering a Browse control.

The Item Randomizer tab has no Content Mixing section. Compatible available content always shares its feature-specific pool, including valid `UNKNOWN` content. Only the Shop/World cross-system pool has an extra user control.

The visible randomizer groups follow this order:

| Row | Left | Right |
| --- | --- | --- |
| 1 | Initial Inventory — starting Blood Code stays vanilla because it comes from the initial quest. | Blood Codes |
| 2 | Boosters — mastery rewards and Booster crafting recipes. | Shops |
| 3 | Enemy Drops | Healing |
| 4 | Protection | Miscellaneous — six Booster slots, full NPC Trader affinity tiers, Motorcycle, and second weapon slot are available in new games. |

World Loot is the always-on baseline and has no master checkbox. `Enemy Drops` controls normal-enemy drops and the confirmed one-time enemy reward locations that join World Loot. `Boosters > Randomize Boosters` shuffles mastery Booster rewards among mastery locations; it does not mix them into World Loot. `Randomize Booster Extractor` shuffles crafted Booster outputs from its canonical Booster identity pool; unallocated identities return to World Loot only while crafting randomization is enabled. Miscellaneous contains `Cheaper Booster Extractor (only 1x of each material)`, which reduces each required material quantity to one and leaves Haze unchanged. Other options include the six-Booster-slot, `Unlock all NPC Trader Shop Tiers`, `Unlock Motorcycle at Start`, second-weapon-slot, and Disable Tutorials settings. Disable Tutorials sets every installed base-game, DLC, and After Ending tutorial StoryFlag default to viewed and disables save-value retention for those rows, so it also suppresses first-time pop-ups on existing saves; the in-game Tutorial encyclopedia remains accessible. Extract includes its three small optional StoryFlag tables in the reduced cache; Generate reads those tables from READY data and adds them to the mod only when enabled. Older caches use the persistent content-addressed extractor for these tables, so later seeds reuse them. The NPC option makes affinity-gated Buddy-row tiers available from the start without changing reward identities; the current maximum requirement is 72. The second weapon setting enables the audited `UnlockEquipSlot_Weapon2` StoryFlag for new games, the same flag raised at Jail rank +11. The Motorcycle option adds the protected Motorcycle Forma, enables `BloodMagic_BikeCreator` for new games, and switches the player's mount-availability formula from late quest progression to that flag. It does not change quest-completion flags or the component's other properties. Protection remains visibly checked and disabled. Every successful generation creates one installed `~mods/Spoiler.txt`; the app output root does not keep a duplicate. The optional CSV spoiler log is not exposed in the UI. The action row below the options and above Status includes Open Install Folder beside Uninstall Randomizer.

The Item Randomizer tab fills the available vertical space down to the bottom action row and shows a vertical scrollbar when the option grid exceeds the viewport. Option groups keep their natural height. When the main window is resized taller, the tab area grows while the action row and Status panel remain pinned at the bottom. Descriptions appear in parentheses beside their options, wrap within the available width, and remain clickable. The Healing Items section uses the `Healing Items` title and `Randomize Healing Items` option, with `Golden Blood Beads` spelled out. The Extract/Game Data and Generate/Remove/Open action row and the Status panel stay anchored at the bottom and keep their normal heights. The current app version, `v0.9.1`, appears in the main-window title, Diagnostic, and About. The About tab follows Diagnostic and shows the app name, canonical display version, short description, bundled `ico/icon.png`, and high-contrast underlined GitHub and official Steam links. The links use a deep blue color and a distinct visited-link color. Nexus Mods remains disabled as “Coming soon” until a real URL is configured. About links open through the system browser and report failures without closing the app. The Windows release is a single-file executable with runtime catalogs, tools, mapping, ICO and PNG icons, and helper bundled inside; the ZIP keeps only the EXE, README, and combined third-party notices at its root. Retoc, UAssetAPI helper, and .NET subprocesses used by extraction and generation run without opening console windows; their captured output remains available to app status and logs. The `Diagnostic` tab shows app, game, cache, output, and settings information; it omits the separate Logs row and `CV2.usmap` because the final application bundles that resource. Its buttons are Open Output Folder, Open Settings Folder, Open Cache Folder, and Clear Extracted Game Data; it has no support-status row. Clearing extracted game data runs in a background worker and shows an indeterminate busy bar with a clearing status until completion. A compact Status panel replaces the on-screen activity log; detailed messages continue to the application log file. Startup cache checks show “Verifying extracted game data…” with an indeterminate progress bar, then report readiness after validation. Long operations show short stage descriptions and determinate progress where the worker reports counts, or an indeterminate busy bar during unmeasured work. The Randomize button is green only while generation is available and asks the user to confirm that the currently installed mod will be overwritten by a new Randomizer for the selected seed; matching mod and `Spoiler.txt` files are backed up first. Cancel stays available during cancellable generation.

The UI has no Generate Only action and no Recommended, Everything Safe, or Disable All preset buttons; users choose the individual randomizer checkboxes. Install-after-generation is always enabled for the user-facing Generate action. After a successful run, the Status panel says `Randomizer completed`.

Descriptions stay short and user-facing. Parser structures, exclusion details, and validation diagnostics belong in Diagnostic or logs.

### Additional tabs

`Diagnostic`
- app version
- detected game version
- game path and extracted game-data cache state/path
- output, logs and settings paths
- Open Output Folder
- Clear Extracted Game Data (asks before removing all extracted-cache revisions)

`About`
- app icon and name
- canonical display version and short description
- clickable GitHub and official Steam links
- Nexus Mods shown as “Coming soon” until a URL is configured

Do not add unfinished enemy/boss randomizer tabs in V1. Blood Codes and Boosters are implemented as checkboxes in the existing item-randomizer view, not as separate tabs.

---

## 16. Default options

The individual checkboxes start with these product defaults; there are no preset/action buttons:

- World Loot: always ON; no user toggle
- Randomize Healing Items: ON by default
- Renewable Ingredients: OFF by default; when enabled, use the separate CDO reward pool
- Shops: ON
- Enemy Drops: OFF
- Blood Codes: OFF
- Boosters: OFF
- Battle Axe: Idris's Conceit beginning quest reward: always in World Loot; no separate toggle
- Mix Shops with World Loot: OFF

### Unrestricted Blood Code Mastery

`Miscellaneous > Unrestricted Blood Code Mastery` sets every installed, non-debug Blood Code Article's `MaxProficiency` to the original value read from the installed Lou 1 Article. It also sets `AppropriateLevel` to 1 where present, removing enemy-level restrictions that otherwise prevent some codes, such as Iris 2, from gaining proficiency in lower-level areas. The option is off by default, applies to existing and new saves, and does not alter the Booster mastery reward table. Extract already includes installed Blood Code Articles in its reduced cache; Generate reuses those cached packages and validates each staged Article after serialization.
- protection rules: always ON
- spoiler log: OFF
- install after generation: ON

Selecting Shops activates all available Shop families. Shop/World mixing stays an optional sub-control. A successful Randomize keeps the persistent extracted cache, installed mod containers, latest `Spoiler.txt`, and per-generation logs; disposable per-generation staging, package copies, manifests, and helper outputs are removed only after install and hash verification succeed. A failed run does not clear the READY cache or delete its diagnostic workspace.

---

## 17. Validation

Before generation:
- game directory exists
- expected executable exists
- game version supported
- required archives available
- required helper tools available
- output directory writable

After extraction:
- expected source packages found
- package hashes/signatures valid where required

After patching:
- every destination still resolves
- count of shuffled entries matches count expected
- no locked item moved
- no duplicate/missing reward unless duplicates existed in original pool
- shop destination price unchanged
- finite Shop stock remains byte-for-byte unchanged; infinite and uncertain slots remain vanilla and are excluded from sources and destinations; protected Tool Shop stock remains vanilla
- Blood Code grants change only `ArticleDataPath`
- Battle Axe: Idris's Conceit changes only the `ArticleDataPath` in its catalogued `GainItem` slot; Count, notification, IDs, and quest structure stay fixed. Its assignment is produced by the shared World Loot plan and appears under World Loot in the spoiler.
- Absolute Executioner is one World Loot assignment mirrored to both alternative Lyle quest grants; the player can receive it through either route, not twice from a duplicated candidate.
- Booster StoryFlag and Booster `ArticleDataPath` move together; mastery owner/rank, Blood Code path, Count, and notification stay at the destination
- Available compatible sources share their feature-specific pools regardless of BASE, DLC, AFTER_ENDING, or UNKNOWN labels; these labels do not determine availability.
- Enemy Drop rows may cross active compatible tables. Shops include every available supported family; Shop/World mode moves Article identity while retaining all destination-specific metadata.
- World Loot uses actual source package presence and retains supported entries with UNKNOWN labels. Renewable Ingredients use their own cache-backed pool and shuffle reward Articles globally by original ingredient type, preserving canonical/mirror assignments; Generate edits the reward-owner Blueprint CDO and leaves destination map actors unchanged.
- serialized package can be reopened/validated

Gameplay has confirmed World Loot, Shops, Enemy Drops, Blood Code grants, and Booster rewards. Renewable Ingredients now use deterministic global Article mappings with CDO-only edits and mirror validation; this corrected implementation still needs controlled in-game save-load confirmation.

After packaging:
- `.pak/.utoc/.ucas` all exist
- non-zero sizes
- staging paths are correct

Only then install.

---

## 18. Error handling

Missing optional packages are skipped and recorded. If an available optional source fails extraction or structural validation, mark that source `INVALID`, skip its independent subsystem block where safe, and show a warning. Installer updates normally require every existing container to match the app's ownership hashes. If a registered Randomizer trio has at least one unchanged container that confirms its identity, modified members are preserved with hashes and the prior ownership record in app-data recovery backups before a controlled replacement. If no unchanged container anchors the registered package, or any destination is not registered to the app, installation remains blocked. A core installation failure, invalid global staging/package output, or patcher failure that could leave partial or unsafe output remains fatal: do not package or install partial output, show a concise error, and write technical details to the log.

Known unsafe packages that failed parser tests should remain in an exclusion list for the supported data revision.

## Generate I/O and cache safety

The READY extracted cache is immutable across Seeds. Deep package-file hashes are established while the cache is extracted/validated. Normal Generate validates cache identity and required-source metadata, then uses the prepared package descriptors and checks the exact expected bytes, NameMap indices, range bounds, and non-overlap for each binary patch. It must not hash every large prepared World Loot package again for every seed.

Binary World Loot outputs are written to sibling temporary files inside the generation workspace. The writer checks source and staged expected bytes, reads back each replacement, verifies unchanged file sizes, then atomically promotes the staged files. It never mutates a READY cache file. Unsafe or unprepared cases continue through the structural serializer and its reopen validation.

Retoc receives the explicit validated package inventory from Generate. Its temporary mount may hardlink staged files because Retoc only reads them; copy fallback is allowed and counted. Generated containers must pass `info`, exact `list --path` inventory comparison, and `verify` before same-volume atomic promotion. A failed build or validation must not leave a partial package trio.

---

## 19. Logging

Use Python `logging`.

Developer log should include:
- app version
- game version
- seed
- options
- source hashes
- pool sizes
- protected counts
- skipped counts
- parser warnings
- helper process command/exit code
- packaging results

Do not log personal data beyond paths necessary for debugging.

---

## 20. Catalog/data files

Do not bury all reverse-engineered game metadata in Python source.

Use versioned data files under `data/`.

Example:

```text
data/
  versions/
    <version>.json
  catalogs/
    current/
      world_loot_catalog.csv
      pickup_types.csv
      shop_catalog_all_tables.csv
      shop_tables_summary.csv
      materials_catalog.csv
      world_loot.json
      materials.json
      shops.json
      exclusions.json
      manifest.json
  enemy_drops/
    enemy_drops_pool_v1.csv
    manifest.json
  blood_codes/current/
    blood_code_grants.json
    manifest.json
  boosters/current/
    mastery_boosters.json
    manifest.json
  reward_pools/current/
    defensive_gadgets.json
    manifest.json
  jails/current/
    jails.json
  bFormae/current/
    bequeathed_formae.json
```

The World Loot catalog is metadata, not a game asset, and records trusted content classes for its supported destinations; the Materials catalog contains BASE-only renewable slots. Blood Code, Booster, Defensive Gadget, Jail, Bequeathed Forma, canonical Article reward, and Extractor catalogs also contain metadata only. Blood Code, Booster, Defensive Gadget, and Extractor manifests bind catalog hashes, revisions, expected counts/distributions, aliases where used, and source asset fingerprints. Their loaders validate identities and row/field relationships; Jail metadata is cross-checked against its current World Loot source slots, while Bequeathed Forma metadata is checked against the current World Loot and Shop source entries. The canonical Article reward catalog currently contains three unique Iris Booster identities; the two mirrored c00070 exports are recorded as two physical occurrences per identity, not duplicated candidates. Current inventories are 79 logical Blood Code reward locations (Lou_01 is not included and remains vanilla), 102 Booster mastery rows, 18 Defensive Gadget locations, 12 player Jail identities, 11 Bequeathed Forma catalog rows, and 43 Extractor recipe rows (36 BASE / 7 DLC). The World Loot category audit includes 251 chests, 3,340 ground-pickup rows, 1,311 normal-enemy drop slots, 54 one-time enemy rewards, and 423 NPC Trader products. `Extract Game Data` obtains source packages from the user's game through the shared extractor and persists them in the app cache; Generate reads that vanilla cache plus checked-in runtime metadata. Canonical Forma DataAssets are not extracted or staged.

Catalog revision validation is necessary but does not by itself confer official game-version support. The canonical `workspace/research_assets/CV2.usmap` is a runtime dependency; other research assets in that directory are local-only.

---

## 21. Tests

At minimum:

### Seed tests
- same seed/options -> identical assignment
- different seed -> different assignment

### Pool tests
- locked entries never move
- source and destination counts preserved
- material never enters world-loot pool
- Shops enter the flexible World Loot pool only when Shop/World mode is enabled; restricted pools remain isolated
- Important rewards participate except for exact Key and quest/flag exclusions and structural locks; excluded vanilla source slots remain unchanged
- compatible available BASE, DLC, AFTER_ENDING, and UNKNOWN sources participate without a user-facing class-mixing option
- source availability uses exact package inventory; absent optional sources are skipped before compatible-pool construction
- World Loot keeps supported entries when a class label is UNKNOWN and skips entries whose required source packages are absent; Renewable Ingredients are prepared from validated Blueprint profiles when enabled

### Shop parser tests
- reads known table
- identifies expected product count
- preserves price
- moves Article identity only for finite destinations; infinite or uncertain destinations and sources are rejected
- important slots remain unchanged
- patched table can be parsed again
- Shops ON includes every available supported family; optional absent tables are skipped
- Shop/World mode moves Article identity only across eligible finite slots and preserves destination price, stock, unlock, and other metadata
- Shop/World mode moves only Article identity and preserves Shop and World destination metadata
- missing World Loot packages/Shop tables are filtered before the joint pool; available counterparts continue independently
- diagnostics and generation manifest list missing packages, invalid sources, and skipped optional groups

### World loot tests
- reads known pickup/chest reward
- patches reward tuple
- preserves actor identity
- protected actors remain unchanged
- includes all three confirmed Jadwiga grants, the DLC e90360 grant, five Harmony Moon Article grants, and three Iris Character Episode Article rewards as generic World Loot destinations
- writes raw Jadwiga rewards as full canonical path/object strings with both FName Number values zero and preserves unrelated RawExport data
- changes only the DLC quest reward's structured ArticleDataPath, preserving its other grant fields and the neighboring Blood Code grant
- reopens fingerprint-bound Jadwiga packages and the DLC/Harmony Moon/Iris quest packages, checking reward paths, preserved fields/exports, package identities, mirrored c00070 writes, and staged package inventory
- checks that Iris canonical reward paths are deduplicated and may be assigned to a compatible ordinary chest when absent from its reward sources
- validates the FieldEnemy catalog and uses each installed location while keeping all 10 key/progression exclusions outside the pool
- enables each FieldEnemy destination whose source package exists, skipping absent locations with a warning
- preserves repeated FieldEnemy reward occurrences, validates exact global Key identities and both source-sidecar hashes, and skips all FieldEnemy destinations before extraction when the group is incomplete
- checks FieldEnemy direct Article assignments use the existing quest patch/staging path and leave unchanged packages unstaged

### Enemy Drop tests
- checks the BASE, AFTER_ENDING, and additional tables independently; an absent optional table does not stop the available tables
- exercises BASE, BASE+AFTER_ENDING, BASE+DLC, and all-three availability in Python and the real .NET helper; rejects unknown, missing-BASE, noncanonical, malformed, and missing-sidecar requests
- shuffles complete `Articles[]` arrays across all active compatible tables
- preserves `Weight`, `DropRate`, `DropDataID`, and excluded rows
- preserves the reward multiset and deterministic seed plan
- reopens patched DataTables and packages only those tables
- replays the exact 1,239-assignment failing helper request directly, then verifies the three outputs reopen and retain destination metadata

### Blood Code and Booster tests
- validates catalog hashes, package fingerprints, slot counts, and content distributions
- same seed/options reproduce the same permutation; pools preserve their reward multisets
- available compatible BASE, DLC, and AFTER_ENDING rows share the same pool automatically
- Blood Code patch changes only the grant `ArticleDataPath`
- Booster patch moves StoryFlag and Booster `ArticleDataPath` together while preserving the mastery fields and all other row data
- reopens patched packages, compares untouched fields and original source hashes, and verifies the packaged virtual asset inventory

### Extractor / Booster Crafting tests
- validates the exact 36 BASE + 7 DLC row metadata and canonical full-path deduplication
- verifies unique no-replacement allocations, deterministic seeds, and exact allocated/remaining partition
- proves Extractor-origin rewards can remain available for a treasure chest and Iris-source Boosters can enter Extractor
- when only `DT_BoosterRecipe` is present, plans 36 rows and never extracts/stages the additional table; with both sources it plans up to 43 rows
- mixing OFF keeps content classes separate; mixing ON combines only active classes
- changes only `CreateArticleDataPath` and preserves all row names, fields, order, and immutable fingerprints
- returns unallocated Booster identities to World Loot only when Extractor is enabled

### Jail and World Loot candidate tests
- validates 12 canonical player Jails (6 BASE / 6 DLC), no Buddy assets or Death alias, and 11 World Loot sources plus Ogre in Initial Inventory metadata
- cross-checks each vanilla World Loot source against the authoritative package/export, reward, payload, save ID, and protection state
- verifies movable Jail source occurrences retain multiplicity, Hounds stays protected, Ogre remains owned by Initial Inventory, and Stinger's exact MainWorld/DLC mirror pair is randomized with only its Article reward changed
- verifies stable source-occurrence IDs are unique, actual physical source multiplicity is conserved, and catalogs cannot synthesize World rewards
- checks manifest and World Loot spoiler assignments against the planner, including unchanged/self-mapped occurrences; no Jail DataAssets are staged

### Defensive Gadget tests
- checks 17 canonical gadgets, 16 BASE / 1 DLC, and aliases
- verifies World Loot source occurrence multiplicity, no catalog-driven reward injection, and deterministic output
- verifies that all available NPC Trader rows are included with Shops, with optional Shop/World mixing and deterministic assignment
- validates the catalogued Warp grant and reopens the final patched `.uasset/.uexp` with unrelated export data preserved

### Bequeathed Forma tests
- validates 10 identities and the 8 BASE / 2 DLC, 1 World / 7 Shop / 2 direct source distribution
- cross-checks the canonical World and Shop slots against their authoritative catalogs
- cross-checks the Idris's Conceit grant against the World Loot destination catalog and keeps it out of the singleton Forma-only pool
- verifies the isolated direct pool produces no patch when only one logical identity remains
- validates the direct quest reward package through the shared World Loot serializer and preserves Count, notification, array length, task IDs, and unrelated quest exports
- records Idris's Conceit and Absolute Executioner once each under World Loot in the spoiler; both physical Executioner quest paths receive its one assignment

### UI and settings tests
- two-column feature grid order and compact descriptions
- Blood Codes and Boosters have separate GroupBoxes
- World Loot is always on; `Randomize Healing Items` defaults ON and can preserve Nectar/Bead locations when switched OFF
- Battle Axe: Idris's Conceit is always randomized with World Loot; no Bequeathed Forma checkbox exists
- Content Mixing and content-class mixing settings are absent; stale saved fields are ignored
- no DLC availability or ownership checkbox exists; obsolete saved `include_dlc_content` settings are ignored
- old `shops_dlc` settings are ignored; DLC participation is derived from catalog and installed assets
- CV2.usmap is resolved from the bundled canonical resource, shown read-only in diagnostics, and missing/invalid mappings produce a clear internal error
- fresh app startups display a new seed and the placeholder Game Path remains unselected
- Randomize is the only generation action; the retired preset controls are absent
- Spoiler.txt starts with the exact generation seed
- successful install cleanup retains the cache, installed files, and spoiler while removing generation staging
- a second generation succeeds from the retained READY cache without extraction
- failed runs retain diagnostics and do not report `Randomizer completed`

### Packaging tests
- correct staging tree
- package helper invoked correctly
- expected three output files exist

Prefer fixture copies/synthetic samples. Never let tests modify the installed game.

---

## 22. Development constraints

- Windows-first.
- Python 3.12+.
- PySide6 UI.
- Keep UI responsive; generation runs in a worker thread/process.
- No user-machine absolute paths in source.
- No game assets committed to the repository.
- No secrets/API keys needed.
- Original game files remain untouched.
- All generated changes live in temp/output/mod folders.

---

## 23. Definition of V1 success

V1 is successful when a developer can:

1. run the Python app
2. select/detect Code Vein 2
3. enter a seed
4. leave the always-on World Loot baseline enabled, choose whether to include Healing, and optionally enable Shops, Enemy Drops, Blood Codes, mastery Boosters, Extractor / Booster Crafting, and Initial Inventory; Battle Axe: Idris's Conceit follows World Loot; Renewable Ingredients are optional; installed package availability is automatic
5. generate a deterministic randomizer
6. produce valid mod archives
7. install them to `Paks/~mods`
8. launch the game separately
9. observe randomized supported content, including World Loot and Healing when selected, direct Blood Code grants, mastery Booster rewards, Booster crafting recipes, Enemy Drops, Jadwiga's World Loot direct rewards, the DLC e90360 reward when enabled, the five Harmony Moon rewards, the three Iris rewards, and Battle Axe: Idris's Conceit as a World Loot quest reward
10. regenerate the same seed/options and get the same mapping
11. remove the randomizer without affecting unrelated mods

Gameplay checks have confirmed direct Blood Code grants, the Lou Rank01 Booster reward swap, and Enemy Drops. The automatic compatible-pool behavior still needs a controlled in-game confirmation. These checks do not add the detected game version to the official supported-version list.

Defensive Gadget World Loot, player Jail World Loot, the Warp/Lise grant, all three Jadwiga generic direct rewards, the DLC e90360 structured quest reward, the five Harmony Moon rewards, and the three Iris rewards have structural coverage. The DLC, Harmony Moon, and Iris packages pass real-asset round trips. The Iris reward swaps were gameplay-tested manually; gameplay using a Randomizer-generated package remains unconfirmed. The manually tested Jadwiga literal identity representation also succeeded in gameplay. Initial starting-gadget/Jail inventory changes remain future work.

