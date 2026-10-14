"""Deterministic, table-bounded Enemy Drop reward-bundle permutations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from cv2randomizer.core.determinism import StableRandom
from cv2randomizer.core.seed import NormalizedSeed, normalize_seed
from cv2randomizer.game.enemy_drop_catalog import (
    EXPECTED_TABLE_SHAPES,
    RANDOMIZED_CLASSIFICATIONS,
    TABLE_ORDER,
    EnemyDropCatalog,
)


class EnemyDropPlanError(ValueError):
    """Raised when parsed source tables or a proposed Enemy Drop plan are unsafe."""


@dataclass(frozen=True, slots=True)
class EnemyDropSlotSnapshot:
    table: str
    row_id: str
    slot_index: int
    weight_fingerprint: str
    non_reward_fingerprint: str
    bundle_fingerprint: str
    reward_labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EnemyDropRowSnapshot:
    table: str
    row_id: str
    slots: tuple[EnemyDropSlotSnapshot, ...]


@dataclass(frozen=True, slots=True)
class EnemyDropTableSnapshot:
    table: str
    rows: tuple[EnemyDropRowSnapshot, ...]


@dataclass(frozen=True, slots=True)
class EnemyDropAssignment:
    destination: EnemyDropSlotSnapshot
    source: EnemyDropSlotSnapshot


@dataclass(frozen=True, slots=True)
class EnemyDropTableStats:
    table: str
    source_rows: int
    source_slots: int
    eligible_rows: int
    eligible_slots: int
    changed_slots: int
    unchanged_slots: int


@dataclass(frozen=True, slots=True)
class EnemyDropPlan:
    seed: NormalizedSeed
    catalog_revision: str
    assignments: tuple[EnemyDropAssignment, ...]
    table_stats: tuple[EnemyDropTableStats, ...]
    reward_multiset_preserved: bool
    excluded_rows_preserved: bool
    weights_preserved: bool
    mix_postgame_dlc_with_base: bool = False
    active_tables: tuple[str, ...] = TABLE_ORDER

    # Find changed slots
    @property
    def changed_slots(self) -> int:
        return sum(table.changed_slots for table in self.table_stats)

    # List unchanged slots
    @property
    def unchanged_slots(self) -> int:
        return sum(table.unchanged_slots for table in self.table_stats)

    # List eligible rows
    @property
    def eligible_rows(self) -> int:
        return sum(table.eligible_rows for table in self.table_stats)

    # List eligible slots
    @property
    def eligible_slots(self) -> int:
        return len(self.assignments)


# Build table RNG
def _table_rng(seed: NormalizedSeed, table: str) -> StableRandom:
    material = f"cv2-enemy-drops-v1\0{seed.internal_value}\0{table}".encode("utf-8")
    return StableRandom.from_material(material)


# Build enemy drop plan
def build_enemy_drop_plan(
    tables: Iterable[EnemyDropTableSnapshot],
    catalog: EnemyDropCatalog,
    seed: str | int | NormalizedSeed | None,
    *,
    mix_postgame_dlc_with_base: bool = False,
    active_tables: Iterable[str] | None = None,
) -> EnemyDropPlan:
    if type(mix_postgame_dlc_with_base) is not bool:
        raise EnemyDropPlanError("Enemy Drop content-mix switch must be a boolean.")
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    table_list = tuple(tables)
    by_table = {table.table: table for table in table_list}
    if len(by_table) != len(table_list):
        raise EnemyDropPlanError("Duplicate Enemy Drop table snapshots were supplied.")
    requested_names = None if active_tables is None else frozenset(active_tables)
    active_table_names = (
        TABLE_ORDER
        if requested_names is None else tuple(table for table in TABLE_ORDER if table in requested_names)
    )
    if not active_table_names or set(by_table) != set(active_table_names):
        raise EnemyDropPlanError(f"Expected available source tables {active_table_names}; got {tuple(sorted(by_table))}.")
    classification = catalog.by_row()
    if len(classification) != len(catalog.rows):
        raise EnemyDropPlanError("Enemy Drop classification catalog contains duplicate rows.")

    eligible_by_table: dict[str, list[EnemyDropSlotSnapshot]] = {}
    table_metadata: dict[str, tuple[int, int, int]] = {}
    for table_name in active_table_names:
        table = by_table[table_name]
        rows = tuple(sorted(table.rows, key=lambda row: (row.row_id.casefold(), row.row_id)))
        row_keys = [(row.table, row.row_id.casefold()) for row in rows]
        if any(row.table != table_name for row in rows) or len(row_keys) != len(set(row_keys)):
            raise EnemyDropPlanError(f"{table_name} source contains an invalid or duplicate row identity.")
        expected_rows = {key[1] for key in classification if key[0] == table_name}
        actual_rows = {row.row_id.casefold() for row in rows}
        if actual_rows != expected_rows:
            missing = sorted(expected_rows - actual_rows)
            unexpected = sorted(actual_rows - expected_rows)
            raise EnemyDropPlanError(
                f"{table_name} classified row mismatch (missing={missing[:8]}, unexpected={unexpected[:8]})."
            )
        source_slots = sum(len(row.slots) for row in rows)
        expected_shape = EXPECTED_TABLE_SHAPES[table_name]
        if len(rows) != expected_shape[0] or source_slots != expected_shape[1]:
            raise EnemyDropPlanError(
                f"{table_name} source shape mismatch: expected {expected_shape[:2]}, got {(len(rows), source_slots)}."
            )

        eligible: list[EnemyDropSlotSnapshot] = []
        for row in rows:
            row_catalog = classification.get((table_name, row.row_id.casefold()))
            assert row_catalog is not None
            slots = tuple(sorted(row.slots, key=lambda slot: slot.slot_index))
            if len(slots) != row_catalog.expected_slots:
                raise EnemyDropPlanError(
                    f"{table_name}:{row.row_id} slot count changed: expected {row_catalog.expected_slots}, got {len(slots)}."
                )
            if any(
                slot.table != table_name or slot.row_id.casefold() != row.row_id.casefold()
                or slot.slot_index != index
                for index, slot in enumerate(slots)
            ):
                raise EnemyDropPlanError(f"{table_name}:{row.row_id} has invalid slot indexes or identities.")
            if row_catalog.classification in RANDOMIZED_CLASSIFICATIONS:
                eligible.extend(slots)
        eligible.sort(key=lambda slot: (slot.row_id.casefold(), slot.row_id, slot.slot_index))
        if (sum(1 for row in rows if classification[(table_name, row.row_id.casefold())].classification in RANDOMIZED_CLASSIFICATIONS), len(eligible)) != expected_shape[2:]:
            raise EnemyDropPlanError(f"{table_name} eligible pool differs from confirmed V1 counts.")

        eligible_by_table[table_name] = eligible
        table_metadata[table_name] = (len(rows), source_slots, expected_shape[2])

    assignments: list[EnemyDropAssignment] = []
    if mix_postgame_dlc_with_base:
        destinations = [slot for table_name in active_table_names for slot in eligible_by_table[table_name]]
        sources = list(destinations)
        # The distinct ALL stream enables cross-table assignments without changing
        # the established per-table streams used when global mixing is disabled.
        _table_rng(normalized, "ALL").shuffle(sources)
        assignments.extend(
            EnemyDropAssignment(destination, source)
            for destination, source in zip(destinations, sources, strict=True)
        )
        if Counter(item.source.bundle_fingerprint for item in assignments) != Counter(
            slot.bundle_fingerprint for slot in destinations
        ):
            raise EnemyDropPlanError("Global Enemy Drop reward bundle multiset changed while planning.")
    else:
        for table_name in active_table_names:
            destinations = eligible_by_table[table_name]
            sources = list(destinations)
            _table_rng(normalized, table_name).shuffle(sources)
            assignments.extend(
                EnemyDropAssignment(destination, source)
                for destination, source in zip(destinations, sources, strict=True)
            )
            if Counter(item.source.bundle_fingerprint for item in assignments if item.destination.table == table_name) != Counter(
                slot.bundle_fingerprint for slot in destinations
            ):
                raise EnemyDropPlanError(f"{table_name} reward bundle multiset changed while planning.")

    stats: list[EnemyDropTableStats] = []
    for table_name in active_table_names:
        table_assignments = [item for item in assignments if item.destination.table == table_name]
        expected_rows, source_slots, eligible_rows = table_metadata[table_name]
        if len({(item.destination.row_id.casefold(), item.destination.slot_index) for item in table_assignments}) != len(eligible_by_table[table_name]):
            raise EnemyDropPlanError(f"{table_name} plan does not assign every destination exactly once.")
        changed = sum(item.destination.bundle_fingerprint != item.source.bundle_fingerprint for item in table_assignments)
        stats.append(EnemyDropTableStats(
            table_name,
            expected_rows,
            source_slots,
            eligible_rows,
            len(eligible_by_table[table_name]),
            changed,
            len(table_assignments) - changed,
        ))

    expected_eligible = sum(EXPECTED_TABLE_SHAPES[table][3] for table in active_table_names)
    if len(assignments) != expected_eligible:
        raise EnemyDropPlanError(f"Expected {expected_eligible:,} eligible Enemy Drop slots, got {len(assignments)}.")
    if len(classification) != 233:
        raise EnemyDropPlanError("Enemy Drop classification catalog is not complete.")
    return EnemyDropPlan(
        normalized,
        catalog.revision,
        tuple(assignments),
        tuple(stats),
        True,
        True,
        True,
        mix_postgame_dlc_with_base,
        active_table_names,
    )


# Find expected bundle fingerprints
def expected_bundle_fingerprints(plan: EnemyDropPlan) -> dict[tuple[str, str, int], str]:
    """Return the exact post-patch reward fingerprint for every eligible destination."""
    return {
        (assignment.destination.table, assignment.destination.row_id.casefold(), assignment.destination.slot_index): assignment.source.bundle_fingerprint
        for assignment in plan.assignments
    }


# Validate patched tables
def validate_patched_tables(
    before: Iterable[EnemyDropTableSnapshot],
    after: Iterable[EnemyDropTableSnapshot],
    plan: EnemyDropPlan,
    catalog: EnemyDropCatalog,
) -> None:
    before_by_table = {table.table: table for table in before}
    after_by_table = {table.table: table for table in after}
    active_tables = plan.active_tables
    if set(before_by_table) != set(active_tables) or set(after_by_table) != set(active_tables):
        raise EnemyDropPlanError("Reopened output omitted one or more Enemy Drop tables.")
    classification = catalog.by_row()
    expected = expected_bundle_fingerprints(plan)
    for table_name in active_tables:
        before_rows = {row.row_id.casefold(): row for row in before_by_table[table_name].rows}
        after_rows = {row.row_id.casefold(): row for row in after_by_table[table_name].rows}
        if set(before_rows) != set(after_rows):
            raise EnemyDropPlanError(f"{table_name} row identities changed after patching.")
        for row_key, original_row in before_rows.items():
            patched_row = after_rows[row_key]
            if len(original_row.slots) != len(patched_row.slots):
                raise EnemyDropPlanError(f"{table_name}:{original_row.row_id} slot count changed after patching.")
            row_class = classification[(table_name, row_key)].classification
            for source_slot, patched_slot in zip(original_row.slots, patched_row.slots, strict=True):
                if source_slot.weight_fingerprint != patched_slot.weight_fingerprint:
                    raise EnemyDropPlanError(f"{table_name}:{original_row.row_id}[{source_slot.slot_index}] Weight changed.")
                if source_slot.non_reward_fingerprint != patched_slot.non_reward_fingerprint:
                    raise EnemyDropPlanError(f"{table_name}:{original_row.row_id}[{source_slot.slot_index}] non-reward properties changed.")
                key = (table_name, row_key, source_slot.slot_index)
                expected_fingerprint = expected.get(key)
                if row_class not in RANDOMIZED_CLASSIFICATIONS:
                    expected_fingerprint = source_slot.bundle_fingerprint
                elif expected_fingerprint is None:
                    raise EnemyDropPlanError(f"{table_name}:{original_row.row_id} eligible slot is absent from the plan.")
                if patched_slot.bundle_fingerprint != expected_fingerprint:
                    raise EnemyDropPlanError(f"{table_name}:{original_row.row_id}[{source_slot.slot_index}] reward bundle differs from plan.")
