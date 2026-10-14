"""Prepare ingredient assignments and patch only shared Blueprint CDO rewards."""

from __future__ import annotations

import hashlib
import inspect
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from cv2randomizer.game.world_loot_serialization import ArticleReward
from cv2randomizer.randomizer.materials import MaterialRandomizationPlan, build_material_randomization_plan

from .asset_extraction import IoStoreAssetExtractor, MaterializedAsset
from .catalogs import MaterialLocation
from .material_dependencies import (
    MaterialClassProfile,
    MaterialClassValidation,
    MaterialDependencyError,
    MaterialDependencyResolver,
)
from .material_serialization import (
    MaterialCdoRewardPatch,
    MaterialPackagePatchResult,
    MaterialSerializationError,
    MaterialSerializer,
)


class MaterialGenerationError(RuntimeError):
    """Raised when a material assignment or staged CDO cannot be validated."""


@dataclass(frozen=True, slots=True)
class MaterialPlanPreparation:
    """A deterministic plan based on Extract-time renewable class profiles."""

    plan: MaterialRandomizationPlan
    class_validation: MaterialClassValidation


@dataclass(frozen=True, slots=True)
class MaterialGenerationResult:
    """Validated shared-CDO reward edits and the Blueprint packages they changed."""

    assignment_count: int
    cdo_reward_patch_count: int
    locked_count: int
    package_results: tuple[MaterialPackagePatchResult, ...]
    output_packages: tuple[Path, ...]


# Prepare material plan
def prepare_material_plan(
    locations: Iterable[MaterialLocation],
    seed: str | int | None,
    *,
    game_root: Path,
    work_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    enabled: bool = True,
    unsafe_packages: Iterable[str] = (),
    resolver: MaterialDependencyResolver | None = None,
    extractor: IoStoreAssetExtractor | None = None,
    prepared_class_profiles: Mapping[str, object] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> MaterialPlanPreparation:
    """Reuse Extract validation when complete; otherwise validate from cached game data."""
    entries = tuple(locations)
    profile_locations = _material_profile_locations(entries)
    dependency_resolver = resolver or MaterialDependencyResolver()
    validation = _validation_from_prepared_profiles(
        profile_locations,
        prepared_class_profiles,
        content_root=work_root.expanduser().resolve() / "source" / "Content",
    )
    if validation is None:
        try:
            validation_kwargs = dict(
                game_root=game_root,
                locations=profile_locations,
                work_root=work_root,
                mappings_path=mappings_path,
                game_version=game_version,
                data_revision=data_revision,
                progress=progress,
            )
            if extractor is not None and "extractor" in inspect.signature(dependency_resolver.validate_classes).parameters:
                validation_kwargs["extractor"] = extractor
            validation = dependency_resolver.validate_classes(**validation_kwargs)
        except Exception as error:
            raise MaterialGenerationError(f"Material class validation failed: {error}") from error
    plan = build_material_randomization_plan(
        entries,
        seed,
        validated_class_packages=validation.eligible_class_packages,
        invalid_class_reasons=validation.invalid_class_reasons,
        enabled=enabled,
        unsafe_packages=unsafe_packages,
    )
    return MaterialPlanPreparation(plan, validation)


# Apply material plan
def apply_material_plan(
    preparation: MaterialPlanPreparation,
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    resolver: MaterialDependencyResolver | None = None,
    serializer: MaterialSerializer | None = None,
    extractor: IoStoreAssetExtractor | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> MaterialGenerationResult:
    """Change only ArticleDataPath on each affected renewable pickup Blueprint CDO."""
    plan = preparation.plan
    validation = preparation.class_validation
    root = work_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    output_root = output_content_root.expanduser().resolve()
    if not _is_within(output_root, root) or output_root == root:
        raise MaterialGenerationError("Output Content tree must be separate and inside the generation work root.")
    source_content = (root / "source" / "Content").resolve()
    if output_root == source_content:
        raise MaterialGenerationError("Material output cannot replace staged source inputs.")
    mappings = mappings_path.expanduser().resolve(strict=True)
    if not mappings.is_file():
        raise MaterialGenerationError("Material mapping file does not exist.")

    validation_by_package = {profile.package_path.casefold(): profile for profile in validation.profiles}
    class_locations: dict[tuple[str, str], MaterialLocation] = {}
    assignments_by_article: dict[str, str] = {}
    destination_by_class: dict[str, MaterialLocation] = {}
    class_destination_article: dict[str, str] = {}
    destinations: set[tuple[str, int]] = set()

    for assignment in plan.assignments:
        destination = assignment.destination
        source = assignment.source
        destination_slot = (destination.package_path.casefold(), destination.export_index)
        if destination_slot in destinations:
            raise MaterialGenerationError("Material plan contains a duplicate physical pickup.")
        destinations.add(destination_slot)

        destination_profile = validation_by_package.get(destination.resource_class_package.casefold())
        source_profile = validation_by_package.get(source.resource_class_package.casefold())
        if (
            destination_profile is None
            or not destination_profile.eligible
            or destination_profile.class_name != destination.resource_class_name
        ):
            raise MaterialGenerationError("Material destination has no validated renewable pickup profile.")
        if (
            source_profile is None
            or not source_profile.eligible
            or source_profile.class_name != source.resource_class_name
        ):
            raise MaterialGenerationError("Material source has no validated renewable pickup profile.")
        if destination.reward.count != 1 or not destination.reward.notification:
            raise MaterialGenerationError("Material destination reward tuple is not the audited ArticleBulkInfo form.")
        if source.reward.count != 1 or not source.reward.notification:
            raise MaterialGenerationError("Material source reward tuple is not the audited ArticleBulkInfo form.")

        original_article = destination.reward.article_path.casefold()
        assigned_article = source.reward.article_path
        existing_assignment = assignments_by_article.setdefault(original_article, assigned_article)
        if existing_assignment.casefold() != assigned_article.casefold():
            raise MaterialGenerationError(
                "The same renewable ingredient Article maps to multiple rewards; refusing a partial CDO shuffle."
            )

        class_key = destination.resource_class_package.casefold()
        existing_article = class_destination_article.setdefault(class_key, original_article)
        if existing_article != original_article:
            raise MaterialGenerationError("One pickup Blueprint CDO has conflicting catalogued source Articles.")
        existing_location = destination_by_class.setdefault(class_key, destination)
        if (
            existing_location.resource_class_name != destination.resource_class_name
            or existing_location.reward.article_path.casefold() != original_article
        ):
            raise MaterialGenerationError("One pickup Blueprint CDO has conflicting class or reward identities.")

        _add_material_profile_location(
            class_locations, destination, destination.resource_class_package, destination.resource_class_name,
        )
        _add_material_profile_location(
            class_locations, source, source.resource_class_package, source.resource_class_name,
        )

    if set(article.casefold() for article in assignments_by_article.values()) != set(assignments_by_article):
        raise MaterialGenerationError("Ingredient mapping is not a complete permutation of its eligible Articles.")

    cdo_assignments = {
        class_key: (destination, assignments_by_article[destination.reward.article_path.casefold()])
        for class_key, destination in destination_by_class.items()
        if assignments_by_article[destination.reward.article_path.casefold()].casefold()
        != destination.reward.article_path.casefold()
    }
    if not cdo_assignments:
        return MaterialGenerationResult(len(plan.assignments), 0, len(plan.locked_locations), (), ())

    all_class_locations = tuple(class_locations.values())
    dependency_resolver = resolver or MaterialDependencyResolver()
    try:
        validation_kwargs = dict(
            game_root=game_root,
            locations=all_class_locations,
            work_root=root,
            mappings_path=mappings,
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        if extractor is not None and "extractor" in inspect.signature(dependency_resolver.validate_classes).parameters:
            validation_kwargs["extractor"] = extractor
        staged_validation = dependency_resolver.validate_classes(**validation_kwargs)
    except Exception as error:
        raise MaterialGenerationError(f"Could not stage renewable ingredient Blueprints: {error}") from error

    staged_profiles = {profile.package_path.casefold(): profile for profile in staged_validation.profiles}
    staged_by_virtual = {asset.virtual_path.casefold(): asset for asset in staged_validation.blueprint_packages}
    for location in all_class_locations:
        prepared = validation_by_package.get(location.resource_class_package.casefold())
        staged = staged_profiles.get(location.resource_class_package.casefold())
        if (
            prepared is None
            or staged is None
            or staged.class_name != location.resource_class_name
            or prepared.direct_parent_package != staged.direct_parent_package
            or prepared.material_hierarchy != staged.material_hierarchy
            or not prepared.eligible
            or prepared.effective_reset_on_yadorigi_rest is not True
            or staged.effective_reset_on_yadorigi_rest is False
        ):
            raise MaterialGenerationError("Staged material Blueprint schema/reset profile changed after planning.")

    serializer = serializer or MaterialSerializer()
    patch_requests: list[MaterialCdoRewardPatch] = []
    virtual_by_source: dict[Path, str] = {}
    for class_key, (destination, assigned_path) in sorted(cdo_assignments.items()):
        asset = staged_by_virtual.get(destination.resource_class_package.casefold())
        if asset is None:
            raise MaterialGenerationError(f"Material CDO owner was not staged: {destination.resource_class_package}.")
        source_file = asset.primary_file.resolve(strict=True)
        if source_file.suffix.casefold() != ".uasset" or not _is_within(source_file, root):
            raise MaterialGenerationError("Material CDO input is not an owned staged Blueprint .uasset package.")
        output_file = source_file.with_name(source_file.stem + ".cv2-material-patched.uasset")
        patch_requests.append(MaterialCdoRewardPatch(
            destination.resource_class_package,
            destination.resource_class_name,
            source_file,
            output_file,
            ArticleReward(
                destination.reward.article_path,
                destination.reward.count,
                destination.reward.notification,
            ),
            ArticleReward(
                assigned_path,
                destination.reward.count,
                destination.reward.notification,
            ),
        ))
        virtual_by_source[source_file] = destination.resource_class_package

    if progress:
        progress("patching-material-cdos", 0, len(patch_requests))
    try:
        package_results = serializer.patch_packages(
            work_root=root,
            mappings_path=mappings,
            packages=patch_requests,
            progress=(lambda current, total: progress("patching-material-cdos", current, total)) if progress else None,
        )
    except Exception as error:
        raise MaterialGenerationError(f"Material shared-CDO reward patch failed validation: {error}") from error

    output_root.mkdir(parents=True, exist_ok=True)
    copy_jobs: list[tuple[Path, Path]] = []
    for result in package_results:
        package_path = virtual_by_source.get(result.input_path.resolve())
        if package_path is None:
            raise MaterialGenerationError("Material serializer returned an unrequested Blueprint package.")
        relative = PurePosixPath(*package_path.split("/")[2:])
        directory = output_root.joinpath(*relative.parts[:-1])
        if not _is_within(directory, output_root):
            raise MaterialGenerationError("Material output path escaped Content.")
        for source_file in _package_files(result.output_path):
            if source_file.is_file():
                copy_jobs.append((source_file, directory / (relative.name + source_file.suffix)))
    targets = [target for _, target in copy_jobs]
    if len(set(targets)) != len(targets) or any(target.exists() for target in targets):
        raise MaterialGenerationError("Refusing to overwrite an existing material Blueprint or sidecar.")

    copied: list[Path] = []
    try:
        for source_file, target in copy_jobs:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".cv2-tmp")
            try:
                shutil.copyfile(source_file, temporary)
                if _sha256_file(source_file) != _sha256_file(temporary):
                    raise MaterialGenerationError(f"Copied material CDO package failed integrity validation: {target.name}.")
                temporary.replace(target)
                copied.append(target)
            finally:
                temporary.unlink(missing_ok=True)
    except Exception:
        for target in copied:
            if target.is_file() and _is_within(target, output_root):
                target.unlink()
        raise

    return MaterialGenerationResult(
        len(plan.assignments),
        sum(len(result.patches) for result in package_results),
        len(plan.locked_locations),
        tuple(package_results),
        tuple(sorted(copied, key=lambda path: str(path).casefold())),
    )


# Find material profile locations
def _material_profile_locations(locations: tuple[MaterialLocation, ...]) -> tuple[MaterialLocation, ...]:
    profiles: dict[tuple[str, str], MaterialLocation] = {}
    for location in locations:
        key = (location.resource_class_package.casefold(), location.resource_class_name.casefold())
        profiles.setdefault(key, location)
    return tuple(profiles[key] for key in sorted(profiles))


# Read validation from prepared profiles
def _validation_from_prepared_profiles(
    locations: tuple[MaterialLocation, ...],
    prepared_profiles: Mapping[str, object] | None,
    *,
    content_root: Path,
) -> MaterialClassValidation | None:
    """Rebuild trusted Extract profiles, including the catalogued reset-audit fallback."""
    if not isinstance(prepared_profiles, Mapping):
        return None
    expected: dict[str, tuple[str, str]] = {}
    for location in locations:
        key = location.resource_class_package.casefold()
        existing = expected.setdefault(key, (location.resource_class_package, location.resource_class_name))
        if existing[1] != location.resource_class_name:
            return None

    cached: dict[str, object] = {}
    for key, value in prepared_profiles.items():
        if not isinstance(key, str) or key.casefold() in cached:
            return None
        cached[key.casefold()] = value

    profiles: list[MaterialClassProfile] = []
    for key, (package_path, class_name) in sorted(expected.items()):
        row = cached.get(key)
        if not isinstance(row, Mapping):
            return None
        actual_package = row.get("package_path")
        actual_class = row.get("class_name")
        parent = row.get("direct_parent_package")
        hierarchy = row.get("material_hierarchy")
        reset = row.get("effective_reset_on_yadorigi_rest")
        catalogued_reset = row.get("catalogued_effective_reset_on_yadorigi_rest")
        reset_source = row.get("reset_validation_source")
        eligible = row.get("eligible")
        reason = row.get("reason")
        if (
            not isinstance(actual_package, str)
            or actual_package.casefold() != key
            or actual_class != class_name
            or (parent is not None and not isinstance(parent, str))
            or type(hierarchy) is not bool
            or (reset is not None and type(reset) is not bool)
            or (catalogued_reset is not None and type(catalogued_reset) is not bool)
            or (reset_source is not None and not isinstance(reset_source, str))
            or type(eligible) is not bool
            or (reason is not None and not isinstance(reason, str))
        ):
            return None
        if (
            hierarchy
            and reset is None
            and catalogued_reset is True
            and reset_source == "catalogued_physical_audit"
            and eligible is False
            and reason == "bResetOnYadorigiRest is absent from the staged Blueprint ancestry"
        ):
            reset, eligible, reason = True, True, None
        profiles.append(MaterialClassProfile(
            package_path, class_name, parent, hierarchy, reset, eligible, reason,
        ))
    return MaterialClassValidation(content_root, (), tuple(profiles))


# Add material profile location
def _add_material_profile_location(
    locations: dict[tuple[str, str], MaterialLocation],
    location: MaterialLocation,
    package: str,
    class_name: str,
) -> None:
    key = (package.casefold(), class_name.casefold())
    locations.setdefault(key, replace(
        location,
        resource_class_package=package,
        resource_class_name=class_name,
    ))


# Build package files
def _package_files(header: Path) -> tuple[Path, ...]:
    return (header, *(header.with_suffix(extension) for extension in (".uexp", ".ubulk", ".uptnl")))


# Parse SHA-256 file
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
