# Renewable Ingredients and Healing Upgrades Plan

## Blood Code completion audit

- [x] Inventory every installed `DA_BloodCode_*` DataAsset and record its exact export FName/Number and availability.
- [x] Resolve Noah_01 and Guardian's Heart to their primary `BuddySettings.BloodCode` owners; keep the two Beginning quest proficiency references as linked metadata, not separate reward locations. Keep Debug-only assets out of the pool.
- [x] By user decision, keep Lou_01 vanilla and outside the randomizer for now. The `DA_InitialInventory_Player.BloodCodes[0]` reference is an additive grant and remains protected; investigation of the true New Game initializer is parked.
- [x] Catalog direct quest, companion SmallScenario, and DLC Harmony rewards without merging separate reward events.
- [x] Add the twelve missing first-tier companion `BuddySettings.BloodCode` reward locations, preserving each companion's original unlock context.
- [x] Patch BuddySettings by exact import identity; for Noah_01 and Guardian's Heart also synchronize the assignment into both mirrored `BloodCodeArticleDatas` proficiency references. Reopen and validate export payload/sidecar preservation.
- [x] Add regression coverage for patching a BuddySettings owner and its linked quest references in the same assignment.
- [x] Rebuild the local Extract cache and complete a normal Blood Codes-only Generate without installation; confirm the final Spoiler, exact staged owners, package list, and Retoc verification.
- [x] Remove the failed additive Lou_01 workaround from the Blood Code catalog and patcher; keep the Initial Inventory BloodCodes field preserved.
- [x] Record the user's gameplay confirmation that Noah_01 and Guardian's Heart randomized successfully after the BuddySettings owner fix.
- [x] Park the true starting ownership/current Blood Code investigation. No Lou_01 replacement test is in scope while the user wants Lou_01 fixed.

Current pool rule: when Blood Code randomization is enabled, all 80 proven reward locations use one shared Blood Code-only pool, so eligible obtainable Blood Codes other than Lou_01 are randomized among those locations. Lou_01 remains vanilla and excluded by current design; Debug-only articles stay excluded. When disabled, all Blood Codes stay vanilla.

Implementation status: development Generate/Retoc validation passed for the corrected owner paths. A fresh-New-Game test disproved replacing `DA_InitialInventory_Player.BloodCodes[0]`: the player received both Lou_01 and the randomized Blood Code. That additive slot has been removed from production logic and the pool. Noah_01 and Guardian's Heart are user-confirmed gameplay PASS. Lou_01 source research is parked by user decision; the current intended behavior is to leave Lou_01 alone and randomize the other confirmed rewards. Generated packages are not installed. The tested game build remains `local-unverified`; this result does not add or imply an officially supported game-version signature.

## Renewable ground ingredients

- [x] Classify by physical actor, Blueprint ancestry, and reset behavior rather than Article names.
- [x] Preserve a physical-location catalog in `data/catalogs/current/world_loot_renewable_ingredients.csv` for a future renewable-only pool.
- [x] Add each of the 825 audited locations as an explicitly locked World Loot row with `RENEWABLE_INGREDIENT_DISABLED`.
- [x] Keep ingredient Articles available globally so a one-time pickup containing the same Article remains eligible.
- [x] Prepare per-class cache profiles and validate the installed class hierarchy during Extract.
- [x] Record runtime-observed reset values separately from catalog audit evidence. An explicit conflict fails; a missing inherited value may use the audited catalog evidence with its source recorded.

Catalog audit: 825 locations, 35 Blueprint classes, 35 unique Articles, and 100 generated map packages. All 825 locations were locked, produced zero assignments, and did not enter Shop/World mixing. One-time material pickups remained ordinary World Loot; the full spoiler includes such assignments.

## Golden Seeds and Golden Blood Beads

- [x] Scan and parse all 35,074 generated MainWorld maps; catalog all 46 Golden Nectar and 12 Golden Blood Bead physical instances in `data/catalogs/current/world_loot_healing_overrides.csv`.
- [x] Identify locations by generated-map package plus export/actor identity, not the shared Blueprint CDO.
- [x] Give every one of the 58 locations a separate deterministic assignment in the same World Loot pool as ordinary eligible pickups and rewards.
- [x] Confirm that two diagnostic healing pickups changed rewards in gameplay. Another unlisted Nectar remained vanilla, exposing the incomplete location audit; all additional instances found in the full scan are now catalogued.
- [x] Clone the source Blueprint package per physical pickup, patch only the cloned CDO's `ArticleDataPath`, and redirect that map actor's `ClassIndex` and `TemplateIndex` to the clone.
- [x] Give every clone a unique serialized `FolderName` matching its virtual package path, preventing Retoc IoStore package-ID collisions.
- [x] Preserve `Count`, notification, map actor properties and `.uexp`, unrelated exports, and opaque source sidecars; add only the clone class/CDO Imports needed by the actor.
- [x] Add a dedicated default-on `Randomize Healing` control while keeping World Loot always enabled. Saved legacy `randomize_healing_upgrades` values remain ignored; turning Healing off preserves Nectar/Bead pickup locations.
- [x] Prevent duplicate Nectar/Bead sources from leaving a healing destination with the same Article path when a safe singleton swap exists; preserve the reward multiset and deterministic seed behavior.

The complete catalog has 58 physical locations across two Blueprint/CDO groups and 58 generated map packages. A full scan found 18 additional physical actors beyond the old 40-row catalog (15 Nectar, three Blood Bead). Parser inspection confirmed every actor class and export index; source CDO metadata confirms the original Article, Count, and notification. Planner regression tests verify rewards move between healing and ordinary World Loot locations in both directions and do not silently return a healing pickup's own Article when a safe alternate exists. The earlier diagnostic confirmed two pickups; the full 58-location package still needs gameplay validation.

## Cache, docs, and regression tests

- [x] Bump source, prepared-descriptor, and preparation revisions so earlier caches are rebuilt for the 18 added locations.
- [x] Persist location category, actor/export identity, CDO source, original reward metadata, and patch strategy per healing location.
- [x] Test renewable locking and same-Article one-time eligibility; all 58 healing assignments and cross-mixing with ordinary World Loot; legacy setting migration; CDO preservation; serialization safety and package reopening.
- [x] Add a WHAT/WHERE planner regression: Blood Bead remains assignable to a Shop while the physical Bead slot receives a different Article (planner separation only; this is not runtime gameplay proof).
- [x] Update `docs/design.md`, `docs/rules.md`, and this plan with runtime behavior and the reset-evidence fallback.
- [x] Run a full local-unverified Generate/Retoc with installation disabled: all 58 healing destinations received a different Article path; Retoc lists all 58 maps and clones and verifies the 721-package container.

## Earlier Generate/Retoc validation

- [x] Rebuild the installed-game cache: READY with 1,866 available sources and no missing optional sources.
- [x] Generate with World Loot and Shops enabled; use a deterministic seed and leave installation disabled.
- [x] Confirm 825 renewable locations remain locked and all 40 then-catalogued healing locations received unique Blueprint/CDO clones and actor redirects.
- [x] Confirm ordinary one-time material pickups remain in World Loot assignments and Shop/World mixing completes.
- [x] Validate the final 713-package output using Retoc `info`, `list --path`, and `verify`; listed paths match the manifest, all 38 clone paths are present, and verification returns `verified`.
- [x] Produce `.pak`, `.ucas`, `.utoc`, and `Spoiler.txt`.
- [x] The user confirmed changed rewards for two diagnostic healing pickups. A third Nectar remained vanilla because it was outside the incomplete catalog; the complete map scan added the remaining physical instances.

The earlier non-installing output passed Retoc `verify`, and two diagnostic healing replacements passed gameplay. The final local cache is READY; the full 58-location output is available at `workspace/healing-upgrade-runtime-research/complete-healing-shared-world-test-final-v2/`. Installation stayed disabled and the local-unverified build is not officially supported by this result. Gameplay validation remains: test with a save where target UniqueSaveID pickups have not already been collected. No original game files were changed.

## Runtime-observed healing locations — earlier 40-row catalog (2026-10-05)

- [x] Add the runtime-observed Nectar at `DUYRG8IXAYWSVR879I7HM7TF0#8` and Blood Bead at `CFIM3HA7UTJ9F5VK2DHC7Q01H#7` to the physical healing catalog; preserve their actor identities and CDO-derived rewards.
- [x] Refresh the earlier local source-cache metadata to 40 healing locations, 2,628 prepared World Loot descriptors, 660 indexed generated maps, and 31/9 physical CDO counts.
- [x] Generate with saved seed `6187687306799046529` and saved options, with installation disabled during the build. The assignments are Nectar `DA_Item_MatBasicRecoveryCountUp_1 -> DA_Item_HpRecover_1` and Blood Bead `DA_Item_MatBasicRecoveryAmountUp_1 -> DA_SingleSword_CarmillaThorn_Lou_1`.
- [x] Validate the 792-package output with Retoc: both newly observed map paths and all 40 clone package paths are listed, `compression_methods: []`, and `verify=verified`.
- [x] Install only the Randomizer-owned `.pak/.ucas/.utoc` trio through `InstallManager`; installed hashes match the generated package.
- [x] Record the user gameplay confirmation that the two diagnostic pickup rewards changed. This confirmed those tested instances, not all 58 physical locations.

## One-time boss and miniboss rewards
- [x] Trace Alexis the Heartless Bodyguard's Adrenaline Formae grant to its actual quest/task owner and internal Article; the `T_UI_SGadget_StrikeRiser` texture is only its icon identity.
- [x] Scan all 1,088 installed Quest packages for Article grants; parse and classify 76 enemy-defeat graphs and their reachable reward slots.
- [x] Add the 13 confirmed dungeon and two confirmed DLC one-time enemy Article grants to World Loot. Retain the existing 24 FieldEnemy locations and five Harmony Moon rewards; the combined audited one-time enemy WHERE count is 44.
- [x] Keep key/quest/progression rewards protected, Blood Codes in their own pool, repeatable multiplayer grants out, and ordinary respawning/farmable `DropItem` rewards in Enemy Drops. Leave the 32 shared/unresolved generic drop rows unchanged without per-location respawn evidence.
- [x] Add deterministic catalog/planner, exact-slot protection, enemy-drop separation, and Shop/World crossing regression tests.
- [x] Make the Enemy Drops checkbox gate all 44 audited one-time enemy World Loot locations as well as normal-enemy table shuffling; when off, those enemy locations remain vanilla and do not enter the World Loot source pool.
- [x] Rebuild the local-unverified Extract cache, run multi-seed checks, patch/reopen changed quest grants, and run a non-installing full Generate with World Loot, Enemy Drops, Shops, and Shop/World mixing enabled; validate all Retoc containers.
- [ ] Gameplay validation by the user: confirm Adrenaline Formae and several other one-time rewards in game. This work did not install any files.

Audit boundary: this pass scanned all installed Quest packages for Article grants and classified reachable enemy-defeat reward graphs, along with the dungeon/FieldEnemy quest groups and known Josee main-episode boss candidate. It does not claim respawn semantics for 32 shared/unresolved generic `DropItem` rows or semantic classification of unrelated quests without a reachable enemy-defeat path.
