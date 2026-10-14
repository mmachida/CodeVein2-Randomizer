"""DEV audit of production cache, exact Article identities, and Shop/World plans.

Never installs output. Uses the existing extractor, cache, planners and patchers.
The first seed runs full Generate; later seeds use its production dry-run path.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import fields, is_dataclass, replace
from pathlib import Path

from cv2randomizer.core.config import SettingsStore
from cv2randomizer.core.workspace import WorkspaceManager
from cv2randomizer.game.content_availability import PackageInventory, resolve_content_availability
from cv2randomizer.game.asset_extraction import materialize_extracted_assets
from cv2randomizer.game.content_classification import build_explicit_article_class_index, classify_package_content
from cv2randomizer.game.fname_identity import FNameIdentity, SoftObjectPathIdentity
from cv2randomizer.game.game_data_cache import CachedAssetExtractor
from cv2randomizer.game.shop_generation import prepare_shop_tables
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES
from cv2randomizer.generation.pipeline import GenerationEngine, GenerationRequest
from cv2randomizer.randomizer.shops import is_npc_trader_row, shop_slot_lock_reason


# Write report
def write_report(path: Path, rows: list[dict], summary: dict) -> None:
    path.with_suffix('.json').write_text(json.dumps({'summary': summary, 'rows': rows}, indent=2), encoding='utf-8')
    if rows:
        with path.with_suffix('.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows({k: json.dumps(v) if isinstance(v, (list, dict, tuple)) else v for k, v in row.items()} for row in rows)


# Resolve the shop slot
def slot(p) -> tuple:
    return p.family, p.shop_row, p.product_index


# Build identity
def identity(p) -> tuple:
    return p.article_path, p.article_path_number, p.item_object, p.item_object_number


# Start the application
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path, required=True)
    parser.add_argument('--mappings', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--installed-inventory', type=Path, help='Inventory captured from Retoc during this audit; otherwise read the game now.')
    parser.add_argument('--seeds', nargs='+', default=['trader-validation-A', 'trader-validation-B', 'trader-validation-C'])
    parser.add_argument('--dry-run-all', action='store_true', help='Plan all seeds without patching or packaging; useful for follow-up comparisons.')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    engine = GenerationEngine(allow_local_unverified=True, workspace_manager=WorkspaceManager(root/'runs'))
    detection = engine.version_detector.detect(args.game)
    cache = engine.game_data_cache_store.inspect(args.game, detection)
    if not cache.ready:
        raise RuntimeError(cache.reason)
    installed = (PackageInventory.create(json.loads(args.installed_inventory.read_text(encoding='utf-8')))
                 if args.installed_inventory else engine.extractor.inspect_package_inventory(args.game))
    cached = cache.inventory
    extractor = CachedAssetExtractor(cache)
    catalogs = engine.catalog_store.load(detection.catalog_revision)
    stage = prepare_shop_tables(catalogs, game_root=args.game, work_root=root/'original',
        mappings_path=args.mappings, game_version=detection.version_id, data_revision=detection.catalog_revision,
        enabled_families=tuple(f for f, n in SHOP_TABLE_NAMES.items() if cached.contains('/Game/Blueprint/GameSystem/Shop/'+n)),
        extractor=extractor)
    products = tuple(p for table in stage.tables for p in table.parsed.products)
    refs: dict[tuple[str, int], set[str]] = defaultdict(set)

    # Add the requested reward item
    def add(path, system, number=0):
        if isinstance(path, str) and path.startswith('/Game/'):
            path = path.split('::', 1)[0].split('.', 1)[0]
            refs[(path, number)].add(system)

    # Visit the next classification node
    def visit(value, system):
        if isinstance(value, SoftObjectPathIdentity):
            add(value.package_name.value, system, value.package_name.number)
        elif isinstance(value, FNameIdentity):
            add(value.value, system, value.number)
        elif isinstance(value, str):
            for match in re.findall(r'/Game/[A-Za-z0-9_/-]+', value):
                add(match, system)
        elif is_dataclass(value):
            # Prefer exact FNames over the diagnostic literal path of a reward.
            sop = getattr(value, 'soft_object_path_identity', None)
            if sop is not None:
                visit(sop, system)
            numbered = hasattr(value, 'article_path_number')
            if numbered:
                add(value.article_path, system, value.article_path_number)
            for field in fields(value):
                if (sop is not None or numbered) and field.name in {'article_path', 'soft_object_path_identity'}:
                    continue
                visit(getattr(value, field.name), system)
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit(item, system)

    for system, paths in cache.metadata['source_registry'].items():
        if system.startswith('materials') or system == 'shops':
            continue
        for path in paths:
            add(path, system)
    for p in products:
        add(p.article_path, 'npc_traders' if is_npc_trader_row(p.family, p.shop_row) else 'shops', p.article_path_number)
    for loc in catalogs.world_loot:
        visit(loc, 'world_loot')
    # Inspect all Enemy Drop rows, including excluded rows, with the production reader.
    from cv2randomizer.game.enemy_drop_generation import ENEMY_DROP_TABLES
    enemy_files = {name: materialize_extracted_assets(extractor.extract_assets(args.game, (path,), game_version=detection.version_id,
        data_revision=detection.catalog_revision), root/'enemy-source'/'Content')[0].primary_file for name, path in ENEMY_DROP_TABLES if cached.contains(path)}
    visit(engine.enemy_drop_serializer.inspect_tables(work_root=root, mappings_path=args.mappings, table_files=enemy_files), 'enemy_drops')

    options = replace(SettingsStore().load().last_options, randomize_world_loot=True, randomize_shops=True,
        mix_shops_with_world=True, randomize_enemy_drops=True, randomize_blood_codes=True,
        randomize_boosters=True, randomize_extractor_recipes=True, randomize_initial_inventory=True,
        randomize_bequeathed_formae_quest_rewards=True, install_after_generation=False, generate_spoiler_log=True)
    reports = []
    slot_rows = []
    preserved_initial_articles: set[str] = set()
    for index, seed in enumerate(args.seeds):
        result = engine.generate(GenerationRequest(args.game, args.mappings, seed, options,
            dry_run=args.dry_run_all or index > 0, game_data_cache_entry=cache.entry_path), progress=lambda *v: print(seed, *v, flush=True))
        assignments = {slot(a.destination): a for a in result.shop_plan.assignments}
        reserved = {slot(p) for p in result.shop_plan.extractor_reserved_slots}
        patched = {slot(p): p for table in result.shop_result.outputs for p in table.parsed.products} if result.shop_result else {}
        per_npc = defaultdict(Counter)
        row_stats = []
        for p in products:
            a = assignments.get(slot(p))
            npc = p.shop_row.rsplit('.',1)[-1] if is_npc_trader_row(p.family,p.shop_row) else None
            reason = ('ARTICLE_UNAVAILABLE' if not cached.contains(p.article_package_path) else
                      'RESERVED_REWARD' if slot(p) in reserved else shop_slot_lock_reason(p))
            if a is None and reason is None:
                raise AssertionError(f'Unexplained exclusion: {slot(p)}')
            changed = a is not None and identity(p) != identity(a.source)
            source_id = a.source.source_id if a is not None and hasattr(a.source, 'source_id') else None
            exact_self_map = source_id == f"shop:{p.family.upper()}:{p.shop_row}:{p.product_index}"
            if npc:
                per_npc[npc]['total'] += 1
                per_npc[npc]['eligible' if a else reason] += 1
                per_npc[npc]['changed'] += int(changed)
                per_npc[npc]['self_maps'] += int(exact_self_map)
            if not result.dry_run:
                final = patched.get(slot(p), p)
                assert identity(final) == (identity(a.source) if a else identity(p)), slot(p)
                assert (final.price_bytes, final.stock_bytes, final.uses_stock_bytes, final.evaluation_raw) == (
                    p.price_bytes,p.stock_bytes,p.uses_stock_bytes,p.evaluation_raw), slot(p)
            if npc:
                source = a.source if a else None
                out = dict(seed=seed,family=p.family,shop_row=p.shop_row,npc=npc,product_index=p.product_index,
                    article_literal=p.article_path,article_number=p.article_path_number,object_literal=p.item_object,
                    object_number=p.item_object_number,resolved_article=p.article_package_path,
                    installed=installed.contains(p.article_package_path),cached=cached.contains(p.article_package_path),
                    alias_used=p.article_path!=p.article_package_path,unlock=p.evaluation_formula,
                    group_unlock=next(group.evaluation_formula for t in stage.tables if t.family==p.family for row in t.parsed.rows if row.name==p.shop_row for group in row.groups if p in group.products),
                    affinity_requirement='See exact unlock expressions; no inferred threshold',protected=p.protected,
                    protection_reason=p.protection_reason,eligible_source=a is not None,eligible_destination=a is not None,
                    first_exclusion=reason,assignment_source=None if source is None else source.source_id,
                    assignment_destination=list(slot(p)),assigned_article=None if source is None else FNameIdentity(source.article_path,source.article_path_number).resolved,
                    changed=changed,exact_self_map=exact_self_map,patched=changed if not result.dry_run else None)
                slot_rows.append(out)
        for name in ('world_plan','enemy_drop_plan','blood_code_plan','booster_plan','extractor_plan','initial_inventory_plan','defensive_gadget_plan','bequeathed_formae_plan'):
            visit(getattr(result,name),name)
        if result.initial_inventory_result:
            inspection = result.initial_inventory_result.inspection
            visit(inspection, 'initial_inventory')
            for preserved in inspection.preserved:
                preserved_initial_articles.add(preserved.article_path.casefold())
        summary=result.shop_plan.npc_traders
        report=dict(seed=seed,dry_run=result.dry_run,workspace=str(result.workspace.root),
            trader_eligible=summary.eligible_slots,trader_from_world=summary.trader_from_world,world_from_trader=summary.world_from_trader,
            trader_locked=len(summary.locked_slots),per_npc=dict(per_npc),
            shop_assignments=len(assignments),shop_locked=len(result.shop_plan.locked_slots),shop_disabled=len(result.shop_plan.disabled_slots),
            global_sources=result.shop_plan.global_candidate_count,global_destinations=result.shop_plan.global_world_locations+result.shop_plan.global_shop_locations,
            metadata_preserved=not result.dry_run,manifest=str(result.manifest_path))
        reports.append(report)
        (root/'seeds.json').write_text(json.dumps(reports,indent=2),encoding='utf-8')
        write_report(root/'npc_trader_audit',slot_rows,{'seeds':reports})

    class_index=build_explicit_article_class_index()
    rows=[]
    for (literal,number),systems in sorted(refs.items()):
        logical=FNameIdentity(literal,number).resolved
        target,variants=installed.resolve_package_path(logical)
        cache_target,cache_variants=cached.resolve_package_path(logical)
        state=resolve_content_availability({'audit':(logical,)},cached).sources[0].state.value
        classification=class_index.get(logical)
        package_class=classify_package_content(logical, installed)
        rows.append(dict(requested_logical_identity=logical,literal_fname=literal,fname_number=number,
            expected_package=logical,installed_package=target,cache_package=cache_target,
            installed='PRESENT' if target else 'ABSENT',cache='PRESENT' if cache_target else 'ABSENT',
            alias_used=number!=0 or (target is not None and target!=logical.casefold()),alias_target=target,
            classification=classification.content_class if classification else package_class.content_class,
            classification_evidence=(list(classification.evidence) if classification else package_class.classification_source),
            availability='ERROR' if (target and not cache_target) or (not target and variants) else 'AVAILABLE' if cache_target else 'OPTIONAL_ABSENT',
            runtime_state=state,systems=sorted(systems),ambiguity=list(variants) if not target else [],
            reference_role='preserved_vanilla_initial_entry' if logical.casefold() in preserved_initial_articles else 'randomizer_source_or_candidate',
            false_cache_absence=bool(target and not cache_target and logical.casefold() not in preserved_initial_articles)))
    summary=dict(references_checked=len(rows),article_references=sum('/article/' in r['expected_package'].lower() for r in rows),
        exact_matches=sum(r['installed']=='PRESENT' and not r['alias_used'] for r in rows),
        aliases_resolved=sum(r['installed']=='PRESENT' and r['alias_used'] for r in rows),
        true_optional_absences=sum(r['availability']=='OPTIONAL_ABSENT' for r in rows),
        false_cache_absences=sum(r['false_cache_absence'] for r in rows),
        unresolved_ambiguities=sum(bool(r['ambiguity']) for r in rows),cache_entry=str(cache.entry_path))
    write_report(root/'article_resolution_audit',rows,summary)
    print(json.dumps(summary,indent=2),flush=True)


if __name__ == '__main__':
    main()
