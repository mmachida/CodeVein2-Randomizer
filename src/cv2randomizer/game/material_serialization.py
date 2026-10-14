"""Patch renewable material rewards on their shared Blueprint CDOs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .world_loot_serialization import (
    ArticleReward,
    WorldLootPackagePatch,
    WorldLootPackagePatchResult,
    WorldLootSerializer,
    WorldLootTuplePatch,
)


class MaterialSerializationError(RuntimeError):
    """Raised when a renewable material CDO reward cannot be patched safely."""


@dataclass(frozen=True, slots=True)
class MaterialCdoRewardPatch:
    """One shared pickup Blueprint CDO reward edit; actor/map bytes are not targeted."""

    class_package: str
    class_name: str
    input_asset: Path
    output_asset: Path
    expected_original: ArticleReward
    assigned: ArticleReward


@dataclass(frozen=True, slots=True)
class MaterialCdoRewardPatchResult:
    class_package: str
    class_name: str
    export_name: str
    export_index: int
    original: ArticleReward
    assigned: ArticleReward


@dataclass(frozen=True, slots=True)
class MaterialPackagePatchResult:
    input_path: Path
    output_path: Path
    patches: tuple[MaterialCdoRewardPatchResult, ...]


class MaterialSerializer:
    """Use World Loot's validated ArticleBulkInfo writer on staged Blueprint CDOs."""

    # Initialize this object
    def __init__(self, *, world_loot_serializer: WorldLootSerializer | None = None) -> None:
        self.world_loot_serializer = world_loot_serializer or WorldLootSerializer()

    # Patch package reward data
    def patch_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        packages: Iterable[MaterialCdoRewardPatch],
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[MaterialPackagePatchResult, ...]:
        """Patch one CDO reward per package and verify every returned identity."""
        requests = tuple(packages)
        if not requests:
            return ()

        package_requests: list[WorldLootPackagePatch] = []
        by_input: dict[Path, MaterialCdoRewardPatch] = {}
        for request in requests:
            if not request.class_package.startswith("/Game/") or "\\" in request.class_package:
                raise MaterialSerializationError("Material CDO schema package is not a canonical /Game/... path.")
            if request.class_name != request.class_package.rsplit("/", 1)[-1] + "_C":
                raise MaterialSerializationError("Material CDO class name does not match its Blueprint package.")
            source = request.input_asset.expanduser().resolve(strict=True)
            output = request.output_asset.expanduser().resolve()
            if source.suffix.casefold() != ".uasset" or output.suffix.casefold() != ".uasset":
                raise MaterialSerializationError("Renewable material reward owners must be Blueprint .uasset packages.")
            if source in by_input:
                raise MaterialSerializationError("Each material Blueprint package may be patched only once.")
            if request.expected_original.count != request.assigned.count or (
                request.expected_original.b_notification != request.assigned.b_notification
            ):
                raise MaterialSerializationError("Ingredient shuffling may change ArticleDataPath only.")
            cdo_name = "Default__" + request.class_name
            package_requests.append(WorldLootPackagePatch(
                source,
                output,
                (WorldLootTuplePatch(
                    export_index=0,
                    reward_index=0,
                    export_name=cdo_name,
                    expected_original=request.expected_original,
                    assigned=request.assigned,
                    require_unique_save_id=False,
                ),),
                blueprint_schema_package=request.class_package,
            ))
            by_input[source] = request

        try:
            results = self.world_loot_serializer.patch_packages(
                work_root=work_root,
                mappings_path=mappings_path,
                packages=package_requests,
                progress=progress,
            )
        except Exception as error:
            raise MaterialSerializationError(f"Material Blueprint CDO reward patch failed: {error}") from error

        if len(results) != len(package_requests):
            raise MaterialSerializationError("Material serializer result count did not match the requested CDO packages.")
        output_by_input = {result.input_path.resolve(): result for result in results}
        if set(output_by_input) != set(by_input):
            raise MaterialSerializationError("Material serializer returned an unexpected CDO package set.")

        validated: list[MaterialPackagePatchResult] = []
        try:
            for source, request in by_input.items():
                package_result = output_by_input[source]
                if len(package_result.patches) != 1:
                    raise MaterialSerializationError("Each material CDO package must contain exactly one reward edit.")
                patch = package_result.patches[0]
                expected_name = "Default__" + request.class_name
                if (
                    patch.export_name != expected_name
                    or patch.export_index < 1
                    or patch.reward_index != 0
                    or patch.unique_save_id != ""
                    or patch.output_path.resolve() != request.output_asset.resolve()
                    or patch.original.article_data_path != request.expected_original.article_data_path
                    or patch.original.count != request.expected_original.count
                    or patch.original.b_notification != request.expected_original.b_notification
                    or patch.assigned.article_data_path != request.assigned.article_data_path
                    or patch.assigned.count != request.assigned.count
                    or patch.assigned.b_notification != request.assigned.b_notification
                ):
                    raise MaterialSerializationError(
                        "Reopened material CDO did not match its exact requested reward edit: "
                        f"observed={patch!r}, expected_class={request.class_name}, "
                        f"expected_original={request.expected_original!r}, expected_assigned={request.assigned!r}."
                    )
                validated.append(MaterialPackagePatchResult(
                    package_result.input_path,
                    package_result.output_path,
                    (MaterialCdoRewardPatchResult(
                        request.class_package,
                        request.class_name,
                        patch.export_name,
                        patch.export_index,
                        patch.original,
                        patch.assigned,
                    ),),
                ))
        except Exception:
            _remove_outputs(results, work_root)
            raise
        return tuple(validated)


# Remove outputs
def _remove_outputs(results: Iterable[WorldLootPackagePatchResult], root: Path) -> None:
    base = root.expanduser().resolve()
    for result in results:
        header = result.output_path.resolve()
        try:
            header.relative_to(base)
        except ValueError:
            continue
        for path in (header, *(header.with_suffix(ext) for ext in (".uexp", ".ubulk", ".uptnl"))):
            if path.is_file() and not path.is_symlink():
                path.unlink()
