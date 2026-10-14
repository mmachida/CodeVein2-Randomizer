"""Deterministic, global type shuffling for renewable material actors."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from cv2randomizer.core.determinism import StableRandom, canonical_package_key
from cv2randomizer.core.seed import NormalizedSeed, normalize_seed
from cv2randomizer.game.catalogs import MaterialLocation

MaterialSlot = tuple[str, int]


@dataclass(frozen=True, slots=True)
class MaterialAssignment:
    """Use the same source ingredient type at every destination of a type."""

    destination: MaterialLocation
    source: MaterialLocation


@dataclass(frozen=True, slots=True)
class LockedMaterialLocation:
    """One catalog entry excluded from the validated renewable pool."""

    location: MaterialLocation
    reason: str


@dataclass(frozen=True, slots=True)
class MaterialRandomizationPlan:
    """A deterministic permutation of ingredient types, expanded to all pickups."""

    assignments: tuple[MaterialAssignment, ...]
    locked_locations: tuple[LockedMaterialLocation, ...]
    disabled_locations: tuple[MaterialLocation, ...]


# List material slot
def material_slot(location: MaterialLocation) -> MaterialSlot:
    """Stable map/export identity for a material actor."""
    return (canonical_package_key(location.package_path)[0], location.export_index)


# Create a seed-based random stream
def _rng(seed_value: int) -> StableRandom:
    key = f"cv2-material-reward-v5\0{seed_value}\0renewable-ingredients".encode("utf-8")
    return StableRandom.from_material(key)


# Build material randomization plan
def build_material_randomization_plan(
    locations: Iterable[MaterialLocation],
    seed: str | int | NormalizedSeed | None,
    *,
    validated_class_packages: Iterable[str],
    invalid_class_reasons: Mapping[str, str] = (),
    enabled: bool = True,
    unsafe_packages: Iterable[str] = (),
) -> MaterialRandomizationPlan:
    """Shuffle renewable reward Articles globally without changing pickup classes.

    Each distinct Article gets one deterministic replacement for the whole game,
    including across multiple pickup Blueprints that share that Article. Generate
    changes only the reward on each shared Blueprint CDO; placed map actors keep
    their original class, template, save identity, and serialized properties.

    Alternate-map mirrors share the canonical type mapping. If any location of a
    type cannot be validated or patched, that entire type stays vanilla so the
    game never contains mixed behavior for one ingredient type.
    """
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    validated = frozenset(value.casefold() for value in validated_class_packages)
    reasons = {key.casefold(): value for key, value in dict(invalid_class_reasons).items()}
    unsafe_values = {value.casefold() for value in unsafe_packages}
    all_locations = tuple(locations)
    by_slot: dict[MaterialSlot, MaterialLocation] = {}
    for location in all_locations:
        if location.export_index < 1:
            raise ValueError("Material export indices must be positive Unreal 1-based values.")
        slot = material_slot(location)
        if slot in by_slot:
            raise ValueError(f"Duplicate material location slot {slot!r}.")
        by_slot[slot] = location

    mirrors_by_canonical: dict[MaterialSlot, list[MaterialLocation]] = {}
    mirror_targets: set[MaterialSlot] = set()
    for mirror in all_locations:
        if mirror.mirror_of_package_path is None:
            if mirror.mirror_of_export_index is not None:
                raise ValueError("Material mirror export index has no target package.")
            continue
        if mirror.mirror_of_export_index is None:
            raise ValueError("Material mirror target is missing its export index.")
        canonical_slot = (mirror.mirror_of_package_path.casefold(), mirror.mirror_of_export_index)
        canonical = by_slot.get(canonical_slot)
        if canonical is None or canonical.mirror_of_package_path is not None:
            raise ValueError("Material mirror target is missing or is not canonical.")
        if canonical_slot in mirror_targets:
            raise ValueError("Multiple material mirrors point to one canonical ingredient.")
        mirror_targets.add(canonical_slot)
        if (
            mirror.actor_class.casefold() != canonical.actor_class.casefold()
            or mirror.resource_class_package.casefold() != canonical.resource_class_package.casefold()
            or mirror.resource_class_name.casefold() != canonical.resource_class_name.casefold()
            or mirror.save_id.casefold() != canonical.save_id.casefold()
            or mirror.reward != canonical.reward
        ):
            raise ValueError("Material mirror does not match its canonical ingredient identity.")
        mirrors_by_canonical.setdefault(canonical_slot, []).append(mirror)
        if mirror.source_file.casefold() in unsafe_values:
            unsafe_values.add(canonical.source_file.casefold())

    unsafe = frozenset(unsafe_values)
    locked: list[LockedMaterialLocation] = []
    disabled: list[MaterialLocation] = []
    eligible: list[MaterialLocation] = []
    canonical_reasons: dict[MaterialSlot, str] = {}
    canonical_locations = tuple(
        location for location in all_locations if location.mirror_of_package_path is None
    )
    blocked_types: dict[str, str] = {}
    for location in sorted(canonical_locations, key=material_slot):
        reason = _lock_reason(location, validated, reasons, unsafe)
        if reason is not None:
            locked.append(LockedMaterialLocation(location, reason))
            canonical_reasons[material_slot(location)] = reason
            blocked_types.setdefault(location.reward.article_path.casefold(), reason)
        elif not enabled:
            disabled.append(location)
        else:
            eligible.append(location)

    # Do not change only some map instances of one type: the feature's contract
    # is a single global type mapping, including alternate-map mirrors.
    if enabled and blocked_types:
        retained: list[MaterialLocation] = []
        for location in eligible:
            reason = blocked_types.get(location.reward.article_path.casefold())
            if reason is None:
                retained.append(location)
            else:
                locked.append(LockedMaterialLocation(
                    location, f"another pickup of this ingredient type is locked: {reason}",
                ))
                canonical_reasons[material_slot(location)] = reason
        eligible = retained

    representative_by_type: dict[str, MaterialLocation] = {}
    for location in eligible:
        article_key = location.reward.article_path.casefold()
        representative_by_type.setdefault(article_key, location)

    ordered = sorted(representative_by_type)
    assigned_type: dict[str, str] = {}
    if len(ordered) > 1:
        rng = _rng(normalized.internal_value)
        rng.shuffle(ordered)
        # Rotating a shuffled Article order creates a deterministic derangement.
        assigned_type.update((destination, ordered[(index + 1) % len(ordered)])
                             for index, destination in enumerate(ordered))
    elif ordered:
        assigned_type[ordered[0]] = ordered[0]

    assignment_by_slot: dict[MaterialSlot, MaterialAssignment] = {}
    for destination in eligible:
        destination_type = destination.reward.article_path.casefold()
        source_type = assigned_type[destination_type]
        assignment_by_slot[material_slot(destination)] = MaterialAssignment(
            destination, representative_by_type[source_type],
        )

    for canonical_slot, mirrors in mirrors_by_canonical.items():
        canonical_assignment = assignment_by_slot.get(canonical_slot)
        if canonical_slot in canonical_reasons:
            reason = canonical_reasons[canonical_slot]
            locked.extend(LockedMaterialLocation(mirror, reason) for mirror in mirrors)
        elif not enabled:
            disabled.extend(mirrors)
        elif canonical_assignment is not None:
            source_type = canonical_assignment.source.reward.article_path.casefold()
            for mirror in mirrors:
                assignment_by_slot[material_slot(mirror)] = MaterialAssignment(
                    mirror, representative_by_type[source_type],
                )

    assignments = tuple(assignment_by_slot[slot] for slot in sorted(assignment_by_slot))
    return MaterialRandomizationPlan(assignments, tuple(locked), tuple(disabled))


# Return lock reason
def _lock_reason(
    location: MaterialLocation,
    validated_class_packages: frozenset[str],
    invalid_class_reasons: Mapping[str, str],
    unsafe_packages: frozenset[str],
) -> str | None:
    if location.protected:
        return location.protection_reason or "catalog protection"
    if location.source_file.casefold() in unsafe_packages:
        return "unsafe package"
    if not location.actor_class_package:
        return "catalog map actor is not a generated Blueprint class"
    for package, label in (
        (location.actor_class_package, "map actor class"),
        (location.resource_class_package, "ingredient class"),
    ):
        class_key = package.casefold()
        if class_key not in validated_class_packages:
            return invalid_class_reasons.get(
                class_key, f"{label} hierarchy or reset behavior is unvalidated",
            )
    resource_class_key = location.resource_class_package.casefold()
    if location.reward.count != 1 or not location.reward.notification:
        return "renewable material reward tuple is not validated"
    if not location.save_id.strip():
        return "UniqueSaveID is missing"
    return None
