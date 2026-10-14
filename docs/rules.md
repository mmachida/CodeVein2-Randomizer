# Randomizer Rules

## Shop slot stock eligibility

A Shop slot may participate in any randomization pool only when the vanilla
serialized payload proves finite stock: `bIsUseStock = True`, both stock fields
are explicitly present, and `Stock > 0`. Unreal's unversioned zero mask selects
the property's schema default. For `bIsUseStock`, that default is `False`, so a
zero-masked flag is known to be infinite even if a stale catalog row says True.

Vanilla infinite slots are locked with `INFINITE_SHOP_STOCK`. They remain fully
vanilla and cannot be used as a source or destination in Shop-only, NPC Trader,
or Shop/World randomization. The randomizer never converts them to quantity 1.

When `bIsUseStock` is True, a zero-masked/missing stock quantity, a conflicting
explicit value, or a nonpositive quantity remains locked with
`UNKNOWN_SHOP_STOCK_SEMANTICS`. A zero-masked `Stock` decodes to 0 for audit,
but does not prove finite quantity or a sentinel meaning. Uncertain slots are
excluded in both directions just like infinite slots.

Finite eligible slots may exchange only their Article identity. Destination
price, `bIsUseStock`, `Stock`, unlock/evaluation data, row, and slot identity
remain unchanged.

The 56 slots in the Magmell Tool Shop remain explicitly protected with
`MAGMELL_TOOL_SHOP`, regardless of their stock fields. They cannot be sources
or destinations even if a slot appears finite. This rule does not protect the
separate Magmell Weapon Shop; its finite slots still follow the shared stock
rule.

## World Loot protection boundary

For physical World Loot, exact Key and Pathos/progression rewards stay
protected. A scenario or flag association by itself is not enough to lock an
ordinary item. When the serialized destination is validated, change only its
Article path and preserve its flags, scenario reference, save identity, Count,
and notification. The six mirrored `DA_Report_A/B/C` pickups in the two
reviewed MainWorld maps use this Article-only scenario mode. Unsafe or opaque
packages and ambiguous mirror data remain unsupported for structural reasons;
they are not policy-protected items.

## One-for-one cross-system ownership

Every randomized item is a physical reward occurrence, identified by its
source slot, rather than only by its Article path. When an occurrence moves
between systems, its old owner receives the displaced destination reward in
return. Never add catalog-only candidates on top of physical sources. Preserve
the combined Article multiset and the source-occurrence count, including when
two distinct slots originally contain the same Article.

For categories whose contents are unique identities (Blood Codes, mastery
Boosters, Defensive Gadgets, Jails, and Bequeathed Formae), the final combined
eligible inventory must contain no duplicate identity. A repeated vanilla
Article in two locations must be audited as separate ownership before it can
enter one of these pools; do not silently preserve a duplicate as another
candidate. This identity rule is different from ordinary World Loot, where two
physical pickups with the same consumable/material Article are separate units
and their occurrence count must be preserved. Initial Inventory exchanges must
also respect the unique-identity pools so a starting item is not left behind
elsewhere after it moves.

Booster mastery, Booster Crafting, and Booster direct rewards must follow one
unique-identity allocation when their respective options are enabled. In the
current catalog there are 102 mastery rewards, 43 recipe outputs, and 10
direct World Booster reward identities, all 155 Article identities distinct.
When both Booster and Booster Crafting are enabled, assign 145 unique Boosters
to the 102 mastery and 43 recipe destinations; route the remaining 10 Booster
identities through World Loot. When only one option is enabled, locations from
the disabled system stay vanilla and do not enter its pool. A mastery row's
Booster `ArticleDataPath` and matching StoryFlag must move together. The current
implementation still keeps mastery rows in a separate pool and only draws
crafting candidates from recipe outputs and active World Booster occurrences;
the unified allocation is a required follow-up, not current behavior.

The 12 canonical player Jails are eligible without replacement. The 11
confirmed World Loot Jail sources—including Bat and Hounds—join World Loot,
while the starting Ogre Jail remains an Initial Inventory occurrence.
Companion Jail assets and the UI label `Death` are not player Jail identities.
The World Loot planner now treats the Bat MainWorld/DlcWorld copies as one
logical source and mirrors the assignment to both. Global uniqueness still
requires resolving repeated physical sources outside World Loot: Stinger also
has a finite Shop source, and Ogre also appears in the starting inventory and
a finite Shop slot.

Absolute Executioner is one unique Bequeathed Forma obtained by either of two
Lyle quest routes. Its World Loot assignment is counted once and mirrored to
both `QST_MainEpisode_Lyle_e00382` and `QST_MainEpisode_Lyle_e00560`; the player
should receive the same randomized item through either route.

## Enemy reward location ownership

The `Randomize Enemy Drops` checkbox controls both enemy reward paths. When it
is off, enemy `DropItem` tables and all catalogued one-time enemy reward
locations remain vanilla. When it is on, `NORMAL_ONLY` and `SHARED_BOSS_NORMAL`
table bundles are shuffled in the Enemy Drop pool. A shared table row changes
the reward for both its respawning enemies and boss/miniboss encounters, while
confirmed one-time boss/miniboss and other audited enemy Article rewards join
the always-on World Loot pool. They may also mix with eligible Shop rewards when Shop/World mixing is enabled. Ordinary
World Loot continues to randomize regardless of this checkbox. Classify the
reward slot from encounter, quest/event, or persistence evidence; never infer
it from a `Boss` name.

Use the serialized reward Article as the identity. UI labels and texture names identify presentation assets, not the Article being granted; for example, Alexis’s `T_UI_SGadget_StrikeRiser` icon corresponds to the Adrenaline Formae Article `DA_GB_CV19_AtkUpFixed`.

Protect exact Key and Pathos/progression reward identities using the existing
immutable classification. Keep Blood Codes, story flags, abilities, unlocks,
and other non-Article rewards in their existing systems. For a mixed-reward
encounter, protect only the proven Key/Pathos slots; ordinary item slots remain
eligible. Preserve the destination trigger, Count, chance/guaranteed behavior,
notification, and all other reward metadata while changing only the Article
identity. The 63 currently listed unsupported reward slots are ordinary
materials/items, not Key/Pathos exclusions: 12 serialized DropItem rows are
used by both recurring enemies and one-time/boss owners. They require a safe
row/table clone and reference redirection before recurring drops can be
shuffled separately from unique rewards. Separately, 32 table rows remain
UNRESOLVED and are not yet represented in the 1,311-row inventory report.

## Renewable ingredient nodes and healing upgrades

Classify renewable ground ingredients by physical pickup location and validated
Blueprint behavior: the class must belong to the `BP_PickupMaterial_*`
hierarchy and its inherited `bResetOnYadorigiRest` value must resolve to
`true`. Keep those nodes out of every World Loot and Shop/World pool with
`RENEWABLE_INGREDIENT_DISABLED`; the separate Renewable Ingredients option may
shuffle their reward Articles. Do not exclude an Article globally; a one-time
pickup containing that same Article remains eligible for its ordinary pool.

Extract validates the installed Blueprint ancestry and records the runtime reset
observation separately from the physical-location audit in
`world_loot_renewable_ingredients.csv`. An explicit runtime `false` conflicts
with renewable classification and fails validation. When the reader omits an
inherited/default property, use the audited catalog value and record that
validation source; do not claim the value was directly read from the asset.
Missing or conflicting structural evidence must never silently promote a
location into a randomizable pool. Ingredients keep their original placed
Blueprint class, map export, and save identity. Generate changes the validated
Article path on the reward-owning shared Blueprint CDO; it never swaps pickup
classes or map actor template references.

Golden Nectars (`DA_Item_MatBasicRecoveryCountUp_1`) and Golden Blood Beads
(`DA_Item_MatBasicRecoveryAmountUp_1`) are controlled by the `Randomize
Healing` option (ON by default). When enabled, the complete audit's 46 Nectar
and 12 Blood Bead physical locations join the shared World Loot pool. When
disabled, those healing locations are omitted from both destinations and
sources, so their original rewards remain in place. World Loot itself is
always randomized. The planner keeps the reward multiset when healing is on
and avoids assigning the same Article path back to a healing location when a
safe singleton swap is available. A non-installing local-unverified Generate
packaged and changed all 58 audited destinations; gameplay validation of this
complete output remains pending.

For Healing, the per-location Blueprint/CDO override path changes the cloned
CDO's `ArticleDataPath` while preserving the actor's destination. Ingredient
randomization has a different audited owner: all physical pickups of a given
ingredient type read the shared CDO on their original Blueprint, so Generate
changes that original reward-owner CDO's `ArticleDataPath` in the mod override
and leaves map actors untouched. Count, notification, actor identity, and
mirrored location assignment remain fixed. Planner tests verify Healing and
ordinary World Loot rewards exchange in both directions. World Loot OFF keeps
all Nectar/Bead locations vanilla.

## Blood Code reward pool

Randomize Blood Code rewards only among Blood Code rewards. Preserve each
reward event and its unlock/quest requirements; change only the Blood Code
identity at that destination. Each Blood Code identity has one logical pool
entry. When a quest and scenario are alternate routes to the same reward,
group them as one location, patch every physical route with the same assignment,
and count the identity once. Distinct reward events remain distinct.

When `Randomize Blood Codes` is OFF, every Blood Code stays vanilla. When it is
ON, the 79 confirmed catalogued reward locations participate in one shared
Blood Code-only pool. For now, `DA_BloodCode_Lou_01` is intentionally protected
outside that pool by user decision; all other confirmed obtainable Blood Codes
are randomized exactly once among those locations. `DA_BloodCode_LouDisp_02`
has a quest grant and two mirrored scenario routes; these are patched as one
logical assignment. The true New Game initializer remains
unresolved, but investigating or replacing it is parked. Replacing
`DA_InitialInventory_Player.BloodCodes[0]` was tested in a fresh game and added
a second Blood Code instead of replacing Lou_01, so that field remains
unchanged. Do not promote ownership checks, tutorial references, alternate
buddy settings, or Debug assets into reward locations.
Preserve literal FName text and Number exactly; do not infer aliases from suffixes.
BuddySettings rewards change only the `BloodCode` reference; keep the companion's
affinity, dialogue, and unlock requirements in their original owner asset.
Initial Inventory remains a separate feature. Its checkbox controls only the
starting Weapon, Defensive Gadget, and Jail. `DA_InitialInventory_Player`'s
Blood Code reference remains vanilla and is protected from patching because it
was proven to be an additive grant path. BodyLight and BasicRecovery remain
vanilla.

## Deterministic assignment contract

For the same randomizer algorithm/app version, game/content fingerprint,
effective options, and normalized seed, the assignment manifest and Spoiler
assignment sections must match across machines, cache/output roots, fresh
processes, and repeated runs. The Spoiler header's local creation date and
assignment-count summary are informational and do not enter those hashes.
Different game/content fingerprints can legitimately produce different
assignments.

All assignment randomness uses the fixed application-owned `StableRandom`
implementation: versioned SHA-256 domain derivation, SplitMix64 state,
rejection-sampled bounded draws, descending Fisher-Yates shuffle, and partial
Fisher-Yates sample. Do not use `random.Random`, global RNG state, Python
`hash()`, or runtime-dependent RNG implementations in planners. Features and
intentionally shared pools derive isolated named streams from the normalized
seed. Canonicalize every pool before shuffle, sample, choice, or pairing:
normalize Unicode to NFC; compare case-folded ordinal identifiers; then use the
normalized original spelling as a stable tie-break. Normalize Unreal path
separators before package comparison. Never use localized labels or absolute
paths as pool keys. Sets and maps may support lookup/deduplication, but their
contents must be sorted before order-sensitive use; parallel discovery results
must likewise be sorted after collection.

The PRNG, seed normalization, canonical ordering, and assignment semantics are
versioned compatibility behavior. Increment `RANDOMIZER_ALGORITHM_VERSION`
when changing any of them. The canonical `assignment_manifest.json` carries the
algorithm version and content/config fingerprints and excludes paths and
timestamps. The fingerprint and manifest hash appear in the generated spoiler
header for comparison between users.

## Current inventory audit (2026-10-08)

The regenerated report at `workspace/randomizer_inventory_summary/` supersedes
the older 8-unsupported-Chest and 201-unsupported-ground-pickup totals. Across
the category rows it records 8,469 entries: 5,920 included, 13 excluded,
75 protected, 2,253 intentionally disabled, and 208 structurally unsupported.
`UNSUPPORTED` means that the current patcher cannot safely edit or classify the
serialized location; it does not mean that the reward is a Key or should stay
vanilla by design.

| Category | Current result | Rule / remaining work |
|---|---:|---|
| Bequeathed Formae | 11/11 included | Absolute Executioner is one logical World Loot assignment mirrored to both alternative Lyle quest routes. |
| Blood Codes | 79/79 included | Lou_01 stays vanilla. Each of the 79 eligible Blood Code identities occurs once in the logical pool; Lou Disp's quest/scenario paths share one assignment. |
| Boosters | 102/102 mastery; 43/43 crafting | Direct World Booster rewards add 10 distinct identities. The required unified 155-identity allocation across mastery, recipes and World Loot is not implemented yet; mastery currently remains in a separate pool. |
| Chests | 251/251 included | The former 8 unsupported chest rows are now supported. |
| Defensive Gadgets / direct rewards | 18/18 and 3/3 included | Preserve one-of-each identity semantics when these exchange with Initial Inventory or World Loot. |
| Enemy Drops | 1,311/1,311 supported report entries | The 63 entries in 12 `SHARED_BOSS_NORMAL` rows now join the pool; that row's result changes for both respawning enemies and boss/miniboss encounters. Another 32 table rows (187 potential slots) remain `UNRESOLVED` and outside the pool. |
| Ground pickups | 3,304 included, 10 protected, 26 unsupported | See the breakdown below. The previous 201 unsupported count is stale. |
| Healing | 116/116 included when enabled | Golden Nectar and Blood Bead occurrences join World Loot only when `Randomize Healing` is on. |
| Initial Inventory | 3 randomized, 3 protected | Only starting weapon, Defensive Gadget and Jail are movable. Lou_01 and the other fixed startup fields remain vanilla. |
| Jails | 12/12 identities included | Hounds and Bat are included; Bat's MainWorld/DlcWorld exports are one source and receive the same assignment. Stinger and Ogre still have cross-system repeats in Shop/Initial Inventory that must be reconciled before claiming global one-of-each. |
| NPC Traders | 423/423 catalog slots | Infinite/uncertain stock rules still apply; Shop/World exchange is controlled by the existing mix option. |
| One-time enemy rewards | 44/54 included; 10 protected | The ten protected entries are explicitly classified Keys. Other confirmed unique boss/miniboss rewards join World Loot only with `Randomize Enemy Drops` enabled. |
| Other Supported | 0/8 included; 8 unsupported | These are 81 physical actors in eight Blueprint classes whose map actors are opaque `RawExport`s. Editing the shared class CDO could also change uninspected overrides, so per-instance-safe parsing/redirects are required. |
| Quest rewards | 6/6 included | The earlier two exclusions no longer apply in the current report. |
| Renewable ingredients | 2,253 locations | Optional separate Ingredients pool; never mixed into World Loot. |
| Shops | 252 included, 13 infinite excluded, 56 protected, 111 unsupported | Magmell Tool Shop stays protected. The 111 have no proof of finite stock; they are not randomizable until stock semantics are validated. |

The 10 protected ground-pickup locations are two each of
`DA_Item_Important_StorageKey`, `DA_Item_Important_WarehouseKey`,
`DA_Item_Important_SpellForTimeTravel`, `DA_Item_Important_Spice_01`, and
`DA_Item_Important_EngravedWeapon_01`. The two Storage/Warehouse items are
exact Keys; the remainder are quest progression.
The policy also excludes Spice 02–03, Engraved Weapon 02–03, and Rift Book
01–04 by exact Article identity. Their 16
branch locations remain `UNSUPPORTED` because the serialized MainWorld/DlcWorld
copies have unresolved structure or reset semantics. The 26 unsupported ground
locations consist of 10 on two unsafe oversized map packages
(`3TZQY8KTP83X39VD1QMH1152D` and `75Z36FWXWUVOJG361NBC6YK52`, including their
DLC mirrors), 8 MainWorld/DlcWorld branch copies with divergent rewards under
reused save IDs (Spice 02–03 and Engraved Weapon 02–03), and 8 Rift Book 01–04
branch copies whose reset/renewal semantics are unconfirmed.
The old user-listed ordinary item identities are
eligible per safe physical occurrence; an occurrence on an unsafe map or an
unresolved branch remains unsupported for that location only.

The previously flagged ordinary item identities are not global exclusions:
`DA_GB_CV11_RecoveryOnHit`, `DA_Item_DefUpIce_1`, `DA_Item_ExpA_09`,
`DA_Item_ExpA_10`, `DA_Item_JadwigaCoin_1`, `DA_Item_MatLevelUpJailA_02`,
`DA_Item_MatLevelUpJailA_06`, `DA_Item_MatLevelUpJailA_07`,
`DA_Item_MatLevelUpWeaponA_01`, `DA_Item_MatLevelUpWeaponA_07`,
`DA_Item_MatLevelUpWeaponB_02`, `DA_Item_RemoveRinne_1`,
`DA_Item_RemoveSlow_1`, `DA_Item_ResUpMoon_1`, `DA_Item_ThrowingKnife_1`,
`DA_Item_Trap_Capture_1`, and `DA_Item_Trap_Stun_1`. Each safe pickup/shop
occurrence can enter its normal pool; renewable, unsafe-map, and unresolved
branch occurrences still follow their location-specific rule.

The eight `Other Supported` class CDO groups are `BP_it20_001_A1_0` (4 actors,
MatCommonFire_2), `BP_it20_001_B1_0` (7, MatCommonBlood_2),
`BP_it20_001_C1_0` (5, MatCommonIce_2), `BP_it20_001_C2_0` (11,
MatCommonIce_1), `BP_it21_008_A_0` (7, MatCommonLeak_1),
`BP_it21_008_A_1` (3, MatCommonLeak_1), `BP_it21_012_A_0` (18,
MatCommonSun_1), and `BP_it22_004_X1_0` (26, MatControlBase_1). These 81 map
instances need instance-safe mutation before inclusion.

The inventory status does not yet satisfy every requested global unique-identity
constraint. In particular, cross-system Shop/World/Initial Inventory ownership,
the two LouDisp Blood Code locations, the unified Booster allocation, and the
63 shared enemy drop rows still need implementation and validation. The
occurrence-based World Loot planner already preserves counts and does not mint
extra occurrences; that guarantee alone is not equivalent to one copy of each
unique Article across every system.
