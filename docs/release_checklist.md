# Release checklist

Use this before publishing source or a Windows test build. Fixture checks are repeatable and suitable for CI; game-data and gameplay checks require a local Code Vein II installation and must not be represented as passing based on fixtures alone.

## Repository and build

- [ ] Start from a fresh checkout and confirm only the canonical `workspace/research_assets/CV2.usmap` is present from `workspace/`.
- [ ] Review `git status`, tracked file sizes, `.gitignore`, and the staged archive contents. Exclude generated workspaces, old releases, game package files, local settings, logs, and dumps.
- [ ] Confirm there are no secrets or machine-specific paths in source, configuration, or checked-in data.
- [ ] Run `python tools/final_audit.py` for the fast fixture suite, source-safety checks, `compileall`, and the UAssetAPI helper Release build.
- [ ] Run `python tools/final_audit.py --full` before a release candidate; record the exact pass/skip counts.
- [ ] Build the Windows bundle with `scripts/build_release.ps1`. Supply the pinned Retoc CLI plus its license and required local Oodle input; do not publish an Oodle-containing binary until redistribution rights are cleared.
- [ ] Confirm the packaged startup/resource smoke test passes, the ZIP contains only intended files, and generated third-party notices are included.
- [ ] Verify the app version, README, known limitations, and selected project license before publishing.

## Game-data cache and optional content

- [ ] On a clean app-data directory, launch the app, select the game, and run Extract until the cache reports `READY`.
- [ ] Close and reopen the app; confirm a valid cache is reused without a second full extraction.
- [ ] Exercise Update Prepared Data and Re-extract Game Data; confirm raw extracted assets remain distinct from prepared descriptors and a failed refresh cannot replace a valid cache with partial data.
- [ ] Check the visible cache states: `NOT_EXTRACTED`, `EXTRACTING`, `READY`, `OUTDATED`, and `INVALID`; include interrupted extraction and a changed game archive.
- [ ] With optional DLC/source files absent, confirm extraction and generation continue for available sources, report skips, and create no destination or Article references to missing packages.
- [ ] Confirm a missing required base package produces an actionable unsupported/incomplete-installation error.
- [ ] Exercise missing and malformed raw/prepared cache files and metadata; generation must fail clearly and must not use stale output as fresh.

## Randomizer and package validation

- [ ] Run the configuration matrix and representative one-feature and mixed-feature plans. Confirm disabled systems preserve vanilla assignments and option state does not leak between runs.
- [ ] Check planner counts against staged patch counts; inspect duplicate destination writes, missing assignments, locked entries, and source/destination occurrence multiplicity.
- [ ] Generate with installation disabled. Confirm changed package payloads, manifest, spoiler, and staged file inventory agree.
- [ ] Confirm Retoc produces non-empty `.pak`, `.utoc`, and `.ucas`, `retoc list` matches the expected package paths, and `retoc verify` succeeds.
- [ ] Check World Loot and Healing binary hits/fallback counts. Explain any structural fallback; compare exact expected bytes and confirm protected/unrelated fields remain unchanged.
- [ ] For identical game/cache/options/algorithm/seed, compare assignment-manifest hash, spoiler bytes, package paths, staged logical asset hashes, and multiplicity. Container hashes may differ only when Retoc metadata is nondeterministic and logical contents still match.
- [ ] Repeat Generate five times in one app session. Record duration, peak/after-run RSS, Python heap where available, handles, threads, children, and temporary files. Check that memory settles and child processes/handles/temp workspaces do not grow monotonically.
- [ ] Repeat after close/reopen and after Update Prepared Data; verify the next Generate uses the current validated cache.

## Safety, recovery, and user-facing checks

- [ ] Confirm original game archives have unchanged hashes across Extract and Generate.
- [ ] Confirm Remove Installed touches only Randomizer-owned containers and spoiler; unrelated `Paks/~mods` entries survive.
- [ ] Test install failure and recovery. No partial output may be promoted or described as installed.
- [ ] Interrupt or fail Extract, preparation, Generate, Retoc, and install where practical; relaunch and verify cache/output state recovers safely.
- [ ] Exercise unwritable cache/output/install folders and low disk space where practical; show clear errors and retain no partial `READY` state.
- [ ] Confirm double-clicks do not start duplicate Extract/Generate operations and UI controls return to usable state after success or failure.
- [ ] Launch from the repository root, parent directory, and a path with spaces/non-ASCII characters. Verify resource and helper lookup does not depend on current working directory.
- [ ] Manually load the generated mod in the game and check representative rewards from each enabled system. Keep gameplay-pending items listed as limitations until tested.

## Current audit record

The latest results and any unchecked gates are in [`workspace/final_app_audit.md`](../workspace/final_app_audit.md) and [`workspace/final_app_audit.json`](../workspace/final_app_audit.json). These local audit artifacts are intended to be kept with the source snapshot and refreshed for each release candidate.
