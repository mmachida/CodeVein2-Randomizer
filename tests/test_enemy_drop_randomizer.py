from __future__ import annotations

import unittest
from collections import Counter

from cv2randomizer.game.enemy_drop_catalog import (
    RANDOMIZED_CLASSIFICATIONS,
    EnemyDropCatalogStore,
    TABLE_ORDER,
)
from cv2randomizer.randomizer.enemy_drops import (
    EnemyDropPlanError,
    EnemyDropRowSnapshot,
    EnemyDropSlotSnapshot,
    EnemyDropTableSnapshot,
    build_enemy_drop_plan,
    validate_patched_tables,
)


class EnemyDropRandomizerTests(unittest.TestCase):
    # Set Up Class
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = EnemyDropCatalogStore().load("current")
        cls.rows_by_table = {
            table: [row for row in cls.catalog.rows if row.table == table]
            for table in TABLE_ORDER
        }

    # Read snapshots
    def _snapshots(self) -> tuple[EnemyDropTableSnapshot, ...]:
        output = []
        for table in TABLE_ORDER:
            rows = []
            for row in self.rows_by_table[table]:
                slots = tuple(
                    EnemyDropSlotSnapshot(
                        table,
                        row.row_id,
                        index,
                        f"weight:{table}:{row.row_id}:{index}",
                        f"non-reward:{table}:{row.row_id}:{index}",
                        f"bundle:{table}:{row.row_id}:{index}",
                    )
                    for index in range(row.expected_slots)
                )
                rows.append(EnemyDropRowSnapshot(table, row.row_id, slots))
            output.append(EnemyDropTableSnapshot(table, tuple(rows)))
        return tuple(output)

    # Check pool totals table boundaries
    def test_pool_totals_and_table_boundaries(self) -> None:
        snapshots = self._snapshots()
        plan = build_enemy_drop_plan(snapshots, self.catalog, "12345")
        self.assertEqual(plan.eligible_rows, 201)
        self.assertEqual(plan.eligible_slots, 1311)
        self.assertEqual(
            [(item.table, item.eligible_rows, item.eligible_slots) for item in plan.table_stats],
            [("BASE", 130, 762), ("AFTER_ENDING", 3, 27), ("DLC", 68, 522)],
        )
        self.assertTrue(all(item.destination.table == item.source.table for item in plan.assignments))
        shared_slots = {
            (table.table, row.row_id.casefold(), slot.slot_index)
            for table in snapshots
            for row in table.rows
            if self.catalog.by_row()[(table.table, row.row_id.casefold())].classification == "SHARED_BOSS_NORMAL"
            for slot in row.slots
        }
        assigned_slots = {
            (item.destination.table, item.destination.row_id.casefold(), item.destination.slot_index)
            for item in plan.assignments
        }
        self.assertEqual(len(shared_slots), 63)
        self.assertTrue(shared_slots.issubset(assigned_slots))

    # Check global mix combines eligible bundles
    def test_global_mix_combines_only_eligible_bundles_across_all_three_tables(self) -> None:
        source = self._snapshots()
        first = build_enemy_drop_plan(source, self.catalog, "cross-table-seed", mix_postgame_dlc_with_base=True)
        second = build_enemy_drop_plan(source, self.catalog, "cross-table-seed", mix_postgame_dlc_with_base=True)
        self.assertEqual(first, second)
        self.assertTrue(first.mix_postgame_dlc_with_base)
        self.assertEqual(len(first.assignments), 1311)
        self.assertTrue(any(item.destination.table != item.source.table for item in first.assignments))
        all_eligible = [
            slot.bundle_fingerprint
            for table in source
            for row in table.rows
            if self.catalog.by_row()[(table.table, row.row_id.casefold())].classification in RANDOMIZED_CLASSIFICATIONS
            for slot in row.slots
        ]
        self.assertEqual(Counter(all_eligible), Counter(item.source.bundle_fingerprint for item in first.assignments))
        replacements = {
            (item.destination.table, item.destination.row_id.casefold(), item.destination.slot_index): item.source.bundle_fingerprint
            for item in first.assignments
        }
        after = tuple(
            EnemyDropTableSnapshot(table.table, tuple(
                EnemyDropRowSnapshot(row.table, row.row_id, tuple(
                    EnemyDropSlotSnapshot(
                        slot.table, slot.row_id, slot.slot_index, slot.weight_fingerprint,
                        slot.non_reward_fingerprint,
                        replacements.get((slot.table, slot.row_id.casefold(), slot.slot_index), slot.bundle_fingerprint),
                    ) for slot in row.slots
                )) for row in table.rows
            )) for table in source
        )
        validate_patched_tables(source, after, first, self.catalog)

    # Check available table subset keep base
    def test_available_table_subset_keeps_base_and_after_ending_slots(self) -> None:
        snapshots = tuple(table for table in self._snapshots() if table.table != "DLC")
        plan = build_enemy_drop_plan(
            snapshots, self.catalog, "optional-table-absent", active_tables=("BASE", "AFTER_ENDING"),
        )
        self.assertEqual({table.table for table in plan.table_stats}, {"BASE", "AFTER_ENDING"})
        self.assertEqual(plan.eligible_rows, 133)
        self.assertEqual(plan.eligible_slots, 789)

    # Check plan is deterministic preserve full
    def test_plan_is_deterministic_and_preserves_full_bundle_multisets(self) -> None:
        source = self._snapshots()
        first = build_enemy_drop_plan(source, self.catalog, "12345")
        second = build_enemy_drop_plan(source, self.catalog, "12345")
        other_seed = build_enemy_drop_plan(source, self.catalog, "12346")
        self.assertEqual(first.assignments, second.assignments)
        self.assertNotEqual(first.assignments, other_seed.assignments)
        for table in TABLE_ORDER:
            original = [
                slot.bundle_fingerprint
                for row in next(snapshot for snapshot in source if snapshot.table == table).rows
                if self.catalog.by_row()[(table, row.row_id.casefold())].classification in RANDOMIZED_CLASSIFICATIONS
                for slot in row.slots
            ]
            assigned = [assignment.source.bundle_fingerprint for assignment in first.assignments if assignment.destination.table == table]
            self.assertEqual(Counter(original), Counter(assigned))

    # Check output verification accept planned rewards
    def test_output_verification_accepts_only_planned_rewards_and_preserved_weights(self) -> None:
        before = self._snapshots()
        plan = build_enemy_drop_plan(before, self.catalog, "12345")
        replacements = {
            (assignment.destination.table, assignment.destination.row_id.casefold(), assignment.destination.slot_index): assignment.source.bundle_fingerprint
            for assignment in plan.assignments
        }
        after = tuple(
            EnemyDropTableSnapshot(
                table.table,
                tuple(
                    EnemyDropRowSnapshot(
                        row.table,
                        row.row_id,
                        tuple(
                            EnemyDropSlotSnapshot(
                                slot.table,
                                slot.row_id,
                                slot.slot_index,
                                slot.weight_fingerprint,
                                slot.non_reward_fingerprint,
                                replacements.get((slot.table, slot.row_id.casefold(), slot.slot_index), slot.bundle_fingerprint),
                            )
                            for slot in row.slots
                        ),
                    )
                    for row in table.rows
                ),
            )
            for table in before
        )
        validate_patched_tables(before, after, plan, self.catalog)

        changed = list(after)
        first_table = changed[0]
        first_row = first_table.rows[0]
        first_slot = first_row.slots[0]
        corrupted_slot = EnemyDropSlotSnapshot(
            first_slot.table,
            first_slot.row_id,
            first_slot.slot_index,
            "changed-weight",
            first_slot.non_reward_fingerprint,
            first_slot.bundle_fingerprint,
        )
        changed[0] = EnemyDropTableSnapshot(
            first_table.table,
            (EnemyDropRowSnapshot(first_row.table, first_row.row_id, (corrupted_slot, *first_row.slots[1:])), *first_table.rows[1:]),
        )
        with self.assertRaisesRegex(EnemyDropPlanError, "Weight changed"):
            validate_patched_tables(before, tuple(changed), plan, self.catalog)

    # Check reject missing classified source row
    def test_rejects_missing_classified_source_row(self) -> None:
        tables = list(self._snapshots())
        tables[0] = EnemyDropTableSnapshot("BASE", tables[0].rows[1:])
        with self.assertRaisesRegex(EnemyDropPlanError, "classified row mismatch"):
            build_enemy_drop_plan(tables, self.catalog, "12345")


if __name__ == "__main__":
    unittest.main()
