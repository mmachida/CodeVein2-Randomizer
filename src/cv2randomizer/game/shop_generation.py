"""Prepare and patch all cooked shop DataTables under an isolated Content tree."""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command
from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    IoStoreAssetExtractor,
    MaterializedAsset,
    materialize_extracted_assets,
)
from cv2randomizer.game.catalogs import RuntimeCatalogs, ShopProduct
from cv2randomizer.game.shop_packages import (
    SHOP_TABLE_NAMES,
    ShopPackageError,
    read_cooked_name_map,
)
from cv2randomizer.game.shop_tables import ShopTableParseResult, ShopTableProduct, parse_shop_table
from cv2randomizer.game.usmap import read_usmap
from cv2randomizer.patching.shop_tables import (
    ShopPatchError,
    patch_shop_table,
    validate_shop_replenish_groups,
)
from cv2randomizer.randomizer.shops import (
    ArticleRewardIdentity,
    ShopRandomizationPlan,
    shop_slot_lock_reason,
)
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION


class ShopGenerationError(RuntimeError):
    """Raised when a cooked shop table cannot be staged or patched safely."""


@dataclass(frozen=True, slots=True)
class ParsedShopTable:
    """Original staged files and catalog-validated parse for one family."""

    family: str
    table_name: str
    virtual_path: str
    asset: MaterializedAsset
    raw_export: bytes
    name_map: object
    parsed: ShopTableParseResult
    expected_products: tuple[ShopProduct, ...]


@dataclass(frozen=True, slots=True)
class ShopTablesStage:
    """All parsed shop families sharing one isolated source Content tree."""

    content_root: Path
    tables: tuple[ParsedShopTable, ...]
    unavailable_optional_families: tuple[str, ...] = ()
    unavailable_families: tuple[str, ...] = ()
    incompatible_families: tuple[str, ...] = ()
    skipped_catalog_product_count: int = 0


@dataclass(frozen=True, slots=True)
class ShopTableOutput:
    """One copied shop table after output reparsing."""

    family: str
    header_path: Path
    export_path: Path
    parsed: ShopTableParseResult
    additional_files: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class ShopGenerationResult:
    """Validated patched table outputs and plan counts."""

    assignment_count: int
    changed_slot_count: int
    locked_count: int
    unvalidated_count: int
    disabled_count: int
    outputs: tuple[ShopTableOutput, ...]


class ShopTableSerializer:
    """Use UAssetAPI for Shop NameMaps and safe raw ExportMap updates."""

    # Initialize this object
    def __init__(
        self,
        *,
        helper_project: Path | None = None,
        dotnet_executable: str = "dotnet",
        engine_version: str = SUPPORTED_ENGINE_VERSION,
        timeout_seconds: float = 1800,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
    ) -> None:
        self.helper_project = (helper_project or resolve_uassetapi_helper()).resolve()
        self.dotnet_executable = dotnet_executable
        self.engine_version = engine_version
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    # Prepare name maps
    def prepare_name_maps(self, *, work_root: Path, mappings_path: Path, packages: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise ShopGenerationError("Shop serializer, generation workspace, or CV2.usmap mapping is missing.")
        manifest = root / f".cv2-shops-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps({"packages": list(packages)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project, self.dotnet_executable, "prepare-shop-name-map",
            str(root), str(mappings), self.engine_version, str(manifest),
        )
        try:
            try:
                completed = self.runner(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout_seconds, cwd=root)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ShopGenerationError(f"Could not run UAssetAPI shop name-map helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise ShopGenerationError(f"UAssetAPI shop name-map preparation failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise ShopGenerationError("UAssetAPI shop name-map helper returned no result.")
            result = json.loads(lines[-1])
            if not isinstance(result, list) or len(result) != len(packages) or any(not isinstance(item, dict) for item in result):
                raise ShopGenerationError("UAssetAPI shop name-map helper returned an invalid package inventory.")
            return result
        except json.JSONDecodeError as error:
            raise ShopGenerationError("UAssetAPI shop name-map helper returned invalid JSON.") from error
        finally:
            manifest.unlink(missing_ok=True)

# Prepare shop tables
def prepare_shop_tables(
    catalogs: RuntimeCatalogs,
    *,
    game_root: Path,
    work_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    enabled_families: Iterable[str] | None = None,
    extractor: IoStoreAssetExtractor | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> ShopTablesStage:
    """Extract the selected Shop tables independently when their sources exist."""
    root = work_root.expanduser().resolve()
    mappings_file = mappings_path.expanduser().resolve(strict=True)
    if not mappings_file.is_file():
        raise ShopGenerationError("Shop usmap mapping does not exist.")
    content_root = root / "source" / "Content"
    catalog_families = {product.family.upper() for product in catalogs.shops}
    if enabled_families is None:
        selected_families = set(SHOP_TABLE_NAMES)
    else:
        selected_families = {family.strip().upper() for family in enabled_families}
        unknown = selected_families - set(SHOP_TABLE_NAMES)
        if unknown:
            raise ShopGenerationError(f"Unknown requested shop families: {', '.join(sorted(unknown))}.")
    requested_families = tuple(
        family for family in SHOP_TABLE_NAMES
        if family in selected_families and family in catalog_families
    )
    optional_families = tuple(family for family in requested_families if family in {"DLC", "TRADE_DLC"})
    package_by_family = {
        family: f"/Game/Blueprint/GameSystem/Shop/{SHOP_TABLE_NAMES[family]}"
        for family in requested_families
    }
    package_paths = tuple(package_by_family.values())
    # Shop tables vary between game versions. A missing family must not block
    # extraction of unrelated systems, so every selected table is optional here.
    optional_paths = package_paths
    service = extractor or IoStoreAssetExtractor()
    try:
        extracted = service.extract_assets(
            game_root,
            package_paths,
            optional_virtual_paths=optional_paths,
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        materialized = materialize_extracted_assets(extracted, content_root)
        by_virtual = {asset.virtual_path.casefold(): asset for asset in materialized}
        mappings = read_usmap(mappings_file)
        tables: list[ParsedShopTable] = []
        unavailable_optional: list[str] = []
        unavailable: list[str] = []
        incompatible: list[str] = []
        skipped_catalog_products = 0
        for index, family in enumerate(requested_families, start=1):
            table_name = SHOP_TABLE_NAMES[family]
            virtual_path = package_by_family[family]
            asset = by_virtual.get(virtual_path.casefold())
            if asset is None:
                unavailable.append(family)
                if family in optional_families:
                    unavailable_optional.append(family)
                if progress:
                    progress("parsing-shop-tables", index, len(requested_families))
                continue
            export_path = asset.primary_file.with_suffix(".uexp")
            expected = tuple(product for product in catalogs.shops if product.family == family)
            try:
                if not export_path.is_file():
                    raise ShopPackageError(f"Extracted {family} table has no .uexp export.")
                name_map = read_cooked_name_map(
                    asset.primary_file,
                    expected_package_name=virtual_path,
                )
                names = set(name_map.names)
                compatible_expected = tuple(
                    product for product in expected
                    if product.shop_row in names
                    and product.original_product_path in names
                    and product.item_object in names
                )
                missing_finite_offers = sum(
                    _is_finite_catalog_offer(product)
                    for product in expected
                    if product not in compatible_expected
                )
                if not compatible_expected:
                    raise ShopPackageError(
                        f"{family} has no catalogued products in this game's NameMap."
                    )
                expected_by_row: dict[str, list[int]] = defaultdict(list)
                for product in compatible_expected:
                    expected_by_row[product.shop_row].append(product.product_index)
                if any(
                    sorted(indices) != list(range(len(indices)))
                    for indices in expected_by_row.values()
                ):
                    raise ShopPackageError(
                        f"{family} has gaps in the catalog slots available in this game's NameMap."
                    )
                raw_export = export_path.read_bytes()
                parsed = parse_shop_table(
                    raw_export,
                    family=family,
                    name_map=name_map,
                    mappings=mappings,
                    expected_products=compatible_expected,
                    source=str(export_path),
                    verify_stock_against_catalog=False,
                )
            except (OSError, ShopPackageError, ValueError):
                # Leave an unsupported table untouched while other systems keep
                # using the packages that were validated successfully.
                unavailable.append(family)
                incompatible.append(family)
                if progress:
                    progress("parsing-shop-tables", index, len(requested_families))
                continue
            skipped_catalog_products += missing_finite_offers
            tables.append(ParsedShopTable(
                family, table_name, virtual_path, asset, raw_export, name_map, parsed,
                compatible_expected,
            ))
            if progress:
                progress("parsing-shop-tables", index, len(requested_families))
        return ShopTablesStage(
            content_root,
            tuple(tables),
            tuple(unavailable_optional),
            tuple(dict.fromkeys(unavailable)),
            tuple(dict.fromkeys(incompatible)),
            skipped_catalog_products,
        )
    except ShopGenerationError:
        raise
    except (AssetExtractionError, OSError, ValueError) as error:
        raise ShopGenerationError(f"Could not prepare shop DataTables: {error}") from error


# Check catalog slot stock
def _is_finite_catalog_offer(product: object) -> bool:
    protected = getattr(product, "protected", False)
    uses_stock = getattr(product, "uses_stock", True)
    stock = getattr(product, "stock", 1)
    return (
        protected is False
        and uses_stock is True
        and type(stock) is int
        and stock > 0
    )


# Apply shop plan
def apply_shop_plan(
    stage: ShopTablesStage,
    plan: ShopRandomizationPlan,
    catalogs: RuntimeCatalogs,
    *,
    output_content_root: Path,
    mappings_path: Path,
    serializer: ShopTableSerializer | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    unlock_npc_trader_shops: bool = False,
) -> ShopGenerationResult:
    """Patch assignment families, copy them, and reparse bytes from final staging."""
    if type(unlock_npc_trader_shops) is not bool:
        raise ShopGenerationError("NPC Trader unlock switch must be a boolean.")
    grouped: dict[str, list] = defaultdict(list)
    physical_assignments = (*plan.assignments, *plan.linked_assignments)
    for assignment in physical_assignments:
        grouped[assignment.destination.family.upper()].append(assignment)
    active = [
        table for table in stage.tables
        if grouped.get(table.family)
        or (unlock_npc_trader_shops and table.family in {"TRADE", "TRADE_DLC"})
    ]
    if not active:
        return ShopGenerationResult(
            len(physical_assignments), 0, len(plan.locked_slots), len(plan.unvalidated_slots),
            len(plan.disabled_slots), (),
        )
    output_root = output_content_root.expanduser().resolve()
    root = stage.content_root.parent.parent.resolve()
    if not _is_within(output_root, root) or output_root == stage.content_root:
        raise ShopGenerationError("Shop output Content must be separate and inside the shared generation work root.")
    mappings = read_usmap(mappings_path.expanduser().resolve(strict=True))
    if (
        plan.mix_postgame_dlc_with_base
        or plan.mix_shops_with_world
        or plan.requires_name_map_preparation
    ):
        return _apply_mixed_shop_plan(
            stage,
            plan,
            grouped,
            active,
            output_root=output_root,
            mappings_path=mappings_path,
            mappings=mappings,
            serializer=serializer or ShopTableSerializer(),
            progress=progress,
            unlock_npc_trader_shops=unlock_npc_trader_shops,
        )
    jobs: list[tuple[Path, Path, bytes]] = []
    output_headers: dict[str, Path] = {}
    try:
        for table in active:
            assignments = tuple(grouped.get(table.family, ()))
            expected_after = _expected_after(table.expected_products, assignments)

            # Reopen the package after patching
            def reparse(output: bytes, *, current: ParsedShopTable = table,
                        expected: tuple[ShopProduct, ...] = expected_after) -> ShopTableParseResult:
                return parse_shop_table(
                    output,
                    family=current.family,
                    name_map=current.name_map,
                    mappings=mappings,
                    expected_products=expected,
                    source=f"patched {current.family} table",
                    verify_stock_against_catalog=False,
                )

            try:
                patched = patch_shop_table(
                    table.raw_export, table.parsed, assignments, reparse=reparse,
                    unlock_npc_trader_shops=unlock_npc_trader_shops,
                )
            except ShopPatchError as error:
                raise ShopGenerationError(f"{table.family} shop table patch was rejected: {error}") from error
            relative = PurePosixPath(*table.virtual_path.split("/")[2:])
            directory = output_root.joinpath(*relative.parts[:-1])
            if not _is_within(directory, output_root):
                raise ShopGenerationError("Resolved shop output path escaped the Content tree.")
            output_header = directory / (relative.name + ".uasset")
            output_export = directory / (relative.name + ".uexp")
            output_headers[table.family] = output_header
            if len(patched.data) != len(table.raw_export):
                raise ShopGenerationError("Shop patch changed the export size; variable-size shop edits are disabled.")
            extra_sources = tuple(
                path for path in table.asset.files if path.suffix.casefold() in {".ubulk", ".uptnl"}
            )
            extra_targets = tuple(output_header.with_suffix(path.suffix) for path in extra_sources)
            for target in (output_header, output_export, *extra_targets):
                if target.exists():
                    raise ShopGenerationError(f"Refusing to overwrite staged shop output {target.name}.")
            jobs.append((table.asset.primary_file, output_header, b""))
            jobs.append((table.asset.primary_file.with_suffix(".uexp"), output_export, patched.data))
            jobs.extend((source, target, b"") for source, target in zip(extra_sources, extra_targets, strict=True))

        copied: list[Path] = []
        try:
            for source, target, replacement in jobs:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".cv2-tmp")
                try:
                    if replacement:
                        temporary.write_bytes(replacement)
                        if temporary.read_bytes() != replacement:
                            raise ShopGenerationError(f"Patched shop export failed write validation: {target.name}.")
                    else:
                        shutil.copyfile(source, temporary)
                        if _sha256_file(source) != _sha256_file(temporary):
                            raise ShopGenerationError(f"Copied shop header failed integrity validation: {target.name}.")
                    temporary.replace(target)
                    copied.append(target)
                finally:
                    if temporary.exists():
                        temporary.unlink()

            final_outputs: list[ShopTableOutput] = []
            table_by_family = {table.family: table for table in active}
            for family in sorted(table_by_family):
                table = table_by_family[family]
                relative = PurePosixPath(*table.virtual_path.split("/")[2:])
                directory = output_root.joinpath(*relative.parts[:-1])
                header = directory / (relative.name + ".uasset")
                export = directory / (relative.name + ".uexp")
                assignments = tuple(grouped.get(family, ()))
                expected_after = _expected_after(table.expected_products, assignments)
                final_parsed = parse_shop_table(
                    export.read_bytes(),
                    family=family,
                    name_map=table.name_map,
                    mappings=mappings,
                    expected_products=expected_after,
                    source=str(export),
                    verify_stock_against_catalog=False,
                )
                _validate_mixed_shop_output(
                    table, final_parsed, assignments,
                    unlock_npc_trader_shops=unlock_npc_trader_shops,
                )
                if not (
                    plan.mix_shops_with_world or plan.mix_postgame_dlc_with_base or plan.requires_name_map_preparation
                ) and _sha256_file(table.asset.primary_file) != _sha256_file(header):
                    raise ShopGenerationError(f"Final {family} header differs from the extracted original.")
                extras = tuple(header.with_suffix(path.suffix) for path in table.asset.files
                               if path.suffix.casefold() in {".ubulk", ".uptnl"})
                final_outputs.append(ShopTableOutput(family, header, export, final_parsed, extras))
            return ShopGenerationResult(
                len(physical_assignments),
                sum(len({
                    (item.destination.shop_row, item.destination.product_index)
                    for item in grouped.get(table.family, ())
                }) for table in active),
                len(plan.locked_slots),
                len(plan.unvalidated_slots),
                len(plan.disabled_slots),
                tuple(final_outputs),
            )
        except Exception:
            for path in copied:
                if path.is_file() and _is_within(path, output_root):
                    path.unlink()
            raise
    except ShopGenerationError:
        raise
    except (OSError, ValueError) as error:
        raise ShopGenerationError(f"Could not write/reparse shop staging outputs: {error}") from error


# Apply mixed shop plan
def _apply_mixed_shop_plan(
    stage: ShopTablesStage,
    plan: ShopRandomizationPlan,
    grouped: dict[str, list],
    active: list[ParsedShopTable],
    *,
    output_root: Path,
    mappings_path: Path,
    mappings,
    serializer: ShopTableSerializer,
    progress: Callable[[str, int, int], None] | None,
    unlock_npc_trader_shops: bool,
) -> ShopGenerationResult:
    root = stage.content_root.parent.parent.resolve()
    table_by_family = {table.family: table for table in active}
    name_packages: list[dict[str, Any]] = []
    output_by_family: dict[str, Path] = {}
    expected_by_family: dict[str, tuple[ShopProduct, ...]] = {}
    missing_names_by_family: dict[str, tuple[str, ...]] = {}
    for family in sorted(table_by_family):
        table = table_by_family[family]
        relative = PurePosixPath(*table.virtual_path.split("/")[2:])
        directory = output_root.joinpath(*relative.parts[:-1])
        if not _is_within(directory, output_root):
            raise ShopGenerationError("Resolved shop output path escaped the Content tree.")
        header = directory / (relative.name + ".uasset")
        if any(path.exists() for path in _package_files(header)):
            raise ShopGenerationError(f"Refusing to overwrite staged shop output {header.name}.")
        output_by_family[family] = header
        assignments = tuple(grouped.get(family, ()))
        expected_by_family[family] = _expected_after(table.expected_products, assignments)
        required_names = {
            name
            for item in assignments
            for name in (item.source.article_path, item.source.item_object)
        }
        missing_names = tuple(sorted(required_names - set(table.name_map.names)))
        missing_names_by_family[family] = missing_names
        if missing_names:
            name_packages.append({
                "family": family,
                "inputFile": str(table.asset.primary_file.resolve(strict=True)),
                "outputFile": str(header),
                "names": list(missing_names),
            })

    try:
        if progress:
            progress("patching-shops", 0, len(table_by_family))
        helper_results = serializer.prepare_name_maps(
            work_root=root,
            mappings_path=mappings_path,
            packages=tuple(name_packages),
        ) if name_packages else []
        prepared_by_family: dict[str, tuple[Path, ...]] = {}
        for response in helper_results:
            family = str(response.get("family", "")).upper()
            if family not in table_by_family or family in prepared_by_family or not missing_names_by_family[family]:
                raise ShopGenerationError("UAssetAPI returned an unexpected shop name-map result.")
            header = output_by_family[family].resolve()
            if Path(str(response.get("outputFile", ""))).resolve() != header:
                raise ShopGenerationError(f"UAssetAPI returned an unexpected {family} shop header path.")
            if response.get("addedNames") != len(missing_names_by_family[family]):
                raise ShopGenerationError(f"UAssetAPI added an unexpected number of {family} FName entries.")
            raw_files = response.get("outputFiles")
            if not isinstance(raw_files, list):
                raise ShopGenerationError(f"UAssetAPI omitted the {family} shop header inventory.")
            files = tuple(Path(str(value)).resolve(strict=True) for value in raw_files)
            if (
                header not in files
                or any(not _is_within(path, output_root) or path.stem.casefold() != header.stem.casefold()
                       or path.suffix.casefold() not in {".uasset", ".ubulk", ".uptnl"} for path in files)
                or len(files) != len(set(files))
            ):
                raise ShopGenerationError(f"UAssetAPI returned an invalid {family} shop header inventory.")
            prepared_by_family[family] = files

        for family in sorted(table_by_family):
            table = table_by_family[family]
            header = output_by_family[family]
            export = header.with_suffix(".uexp")
            if family not in prepared_by_family:
                _copy_validated(table.asset.primary_file, header)
                extra_sources = tuple(
                    path for path in table.asset.files
                    if path.suffix.casefold() in {".ubulk", ".uptnl"}
                )
                for source in extra_sources:
                    target = header.with_suffix(source.suffix)
                    _copy_validated(source, target)
                prepared_by_family[family] = (header, *(header.with_suffix(path.suffix) for path in extra_sources))

            if export.exists():
                raise ShopGenerationError(f"Refusing to overwrite staged shop export {export.name}.")
            name_map = read_cooked_name_map(header, expected_package_name=table.virtual_path)
            assignments = tuple(grouped.get(family, ()))
            encoded_paths: dict[tuple[str, str, int], bytes] = {}
            for assignment in assignments:
                source = assignment.source
                try:
                    package_index = name_map.names.index(source.article_path)
                    object_index = name_map.names.index(source.item_object)
                except ValueError as error:
                    raise ShopGenerationError(
                        f"{family} output name map lacks an assigned ArticleDataPath reference."
                    ) from error
                encoded_paths[(family, assignment.destination.shop_row, assignment.destination.product_index)] = struct.pack(
                    "<iiiii", package_index, source.article_path_number,
                    object_index, source.item_object_number, 0,
                )

            source_data = {item.family: item.raw_export for item in stage.tables}

            # Reopen the package after patching
            def reparse(output: bytes, *, current_family: str = family, current_map=name_map,
                        expected: tuple[ShopProduct, ...] = expected_by_family[family]) -> ShopTableParseResult:
                return parse_shop_table(
                    output, family=current_family, name_map=current_map, mappings=mappings,
                    expected_products=expected, source=f"mixed {current_family} shop table",
                    verify_stock_against_catalog=False,
                )

            patched = patch_shop_table(
                table.raw_export,
                table.parsed,
                assignments,
                reparse=reparse,
                allow_cross_family=True,
                source_data_by_family=source_data,
                encoded_article_path_bytes=encoded_paths,
                unlock_npc_trader_shops=unlock_npc_trader_shops,
            )
            if len(patched.data) != len(table.raw_export):
                raise ShopGenerationError("Shop patch changed the export size; variable-size shop edits are disabled.")
            temporary = export.with_name(export.name + ".cv2-tmp")
            try:
                temporary.write_bytes(patched.data)
                if temporary.read_bytes() != patched.data:
                    raise ShopGenerationError(f"Patched shop export failed write validation: {export.name}.")
                temporary.replace(export)
            finally:
                temporary.unlink(missing_ok=True)

        final_outputs: list[ShopTableOutput] = []
        for family in sorted(table_by_family):
            table = table_by_family[family]
            header = output_by_family[family]
            export = header.with_suffix(".uexp")
            final_map = read_cooked_name_map(header, expected_package_name=table.virtual_path)
            final_parsed = parse_shop_table(
                export.read_bytes(), family=family, name_map=final_map, mappings=mappings,
                expected_products=expected_by_family[family], source=str(export),
                verify_stock_against_catalog=False,
            )
            _validate_mixed_shop_output(
                table, final_parsed, tuple(grouped.get(family, ())),
                unlock_npc_trader_shops=unlock_npc_trader_shops,
            )
            extras = tuple(path for path in prepared_by_family[family] if path not in {header, export})
            final_outputs.append(ShopTableOutput(family, header, export, final_parsed, extras))
        if progress:
            progress("patching-shops", len(table_by_family), len(table_by_family))
        return ShopGenerationResult(
            sum(len(items) for items in grouped.values()),
            sum(len({
                (item.destination.shop_row, item.destination.product_index)
                for item in grouped.get(table.family, ())
            }) for table in active),
            len(plan.locked_slots),
            len(plan.unvalidated_slots),
            len(plan.disabled_slots),
            tuple(final_outputs),
        )
    except Exception:
        # Every target package was verified absent before helper execution, so remove
        # partial helper output too, including files created before its response arrived.
        for header in output_by_family.values():
            for path in _package_files(header):
                if _is_within(path, output_root):
                    path.unlink(missing_ok=True)
            temporary = header.with_suffix(".uexp").with_name(header.stem + ".uexp.cv2-tmp")
            if _is_within(temporary, output_root):
                temporary.unlink(missing_ok=True)
        raise

# Validate mixed shop output
def _validate_mixed_shop_output(
    table: ParsedShopTable,
    after: ShopTableParseResult,
    assignments: tuple,
    *,
    unlock_npc_trader_shops: bool = False,
) -> None:
    try:
        validate_shop_replenish_groups(
            table.parsed, after,
            unlock_npc_trader_shops=unlock_npc_trader_shops,
        )
    except ShopPatchError as error:
        raise ShopGenerationError(f"{table.family} unlock/evaluation metadata validation failed: {error}") from error
    before_rows = {row.name: row for row in table.parsed.rows}
    after_rows = {row.name: row for row in after.rows}
    if tuple(before_rows) != tuple(after_rows):
        raise ShopGenerationError(f"{table.family} shop row identities or order changed after serialization.")
    assigned = {(item.destination.shop_row, item.destination.product_index): item for item in assignments}
    for row_name, before_row in before_rows.items():
        after_row = after_rows[row_name]
        if len(before_row.products) != len(after_row.products):
            raise ShopGenerationError(f"{table.family}:{row_name} product slot count changed.")
        for before, current in zip(before_row.products, after_row.products, strict=True):
            assignment = assigned.get((row_name, before.product_index))
            expected_stock = (
                assignment.stock_override
                if assignment is not None and assignment.stock_override is not None
                else before.stock
            )
            expected_stock_bytes = (
                struct.pack("<i", expected_stock)
                if assignment is not None and assignment.stock_override is not None
                else before.stock_bytes
            )
            stock_valid = (
                current.uses_stock == before.uses_stock
                and current.uses_stock_bytes == before.uses_stock_bytes
                and current.stock == expected_stock
                and current.stock_bytes == expected_stock_bytes
            )
            if assignment is None:
                unchanged = (
                    current.article_path == before.article_path
                    and current.item_object == before.item_object
                    and current.article_path_bytes == before.article_path_bytes
                    and current.price_bytes == before.price_bytes
                    and current.evaluation_raw == before.evaluation_raw
                    and current.evaluation_formula == before.evaluation_formula
                    and stock_valid
                )
                if not unchanged:
                    raise ShopGenerationError(f"Unassigned {table.family}:{row_name}[{before.product_index}] changed.")
                continue
            source = assignment.source
            if assignment.article_only:
                assert isinstance(source, ArticleRewardIdentity)
                article_valid = (
                    current.article_path == source.article_path
                    and current.item_object == source.item_object
                    and current.article_path_number == source.article_path_number
                    and current.item_object_number == source.item_object_number
                    and current.price == before.price
                    and current.price_bytes == before.price_bytes
                    and current.evaluation_raw == before.evaluation_raw
                    and current.evaluation_formula == before.evaluation_formula
                )
                if source.source_product is not None and shop_slot_lock_reason(source.source_product) is not None:
                    raise ShopGenerationError("Reopened Shop source is protected, infinite, or has unresolved stock semantics.")
            else:
                if shop_slot_lock_reason(source) is not None:
                    raise ShopGenerationError("Reopened Shop source is protected, infinite, or has unresolved stock semantics.")
                article_valid = (
                    current.article_path == source.article_path
                    and current.item_object == source.item_object
                    and current.article_path_number == source.article_path_number
                    and current.item_object_number == source.item_object_number
                    and current.price == before.price
                    and current.price_bytes == before.price_bytes
                    and current.evaluation_raw == before.evaluation_raw
                    and current.evaluation_formula == before.evaluation_formula
                )
            if shop_slot_lock_reason(before) is not None:
                raise ShopGenerationError("Reopened Shop destination is protected, infinite, or has unresolved stock semantics.")
            if not article_valid or not stock_valid:
                raise ShopGenerationError(f"Reopened {table.family}:{row_name}[{before.product_index}] does not match its compatible-family assignment.")


# Build package files
def _package_files(header: Path) -> tuple[Path, ...]:
    return tuple(header.with_suffix(suffix) for suffix in (".uasset", ".uexp", ".ubulk", ".uptnl"))


# Copy validated
def _copy_validated(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ShopGenerationError(f"Refusing to overwrite staged shop file {target.name}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".cv2-tmp")
    try:
        shutil.copyfile(source, temporary)
        if _sha256_file(source) != _sha256_file(temporary):
            raise ShopGenerationError(f"Shop file copy failed integrity validation: {target.name}.")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


# Find expected after
def _expected_after(
    expected: tuple[ShopProduct, ...],
    assignments: Iterable,
) -> tuple[ShopProduct, ...]:
    by_slot = {(product.family, product.shop_row, product.product_index): product for product in expected}
    result = dict(by_slot)
    for assignment in assignments:
        destination = assignment.destination
        source = assignment.source
        slot = (destination.family.upper(), destination.shop_row, destination.product_index)
        catalog = result.get(slot)
        if catalog is None:
            raise ShopGenerationError(f"Shop patch assignment is outside its catalog family: {slot!r}.")
        if assignment.article_only:
            if not isinstance(source, ArticleRewardIdentity):
                raise ShopGenerationError("Article-only assignment is missing its canonical Article source.")
            changes = dict(
                original_product_path=source.article_path,
                item_object=source.item_object,
                what_content_class=source.content_class,
                article_path_number=source.article_path_number,
                item_object_number=source.item_object_number,
            )
        else:
            changes = dict(
                original_product_path=source.article_path,
                item_object=source.item_object,
                what_content_class=source.what_content_class,
                article_path_number=source.article_path_number,
                item_object_number=source.item_object_number,
            )
        if assignment.stock_override is not None:
            changes["stock"] = assignment.stock_override
        result[slot] = replace(catalog, **changes)
    return tuple(result[key] for key in sorted(result))


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
