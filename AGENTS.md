# AGENTS.md

## Project
Code Vein 2 Randomizer — Python desktop app that generates deterministic randomizer mods for Code Vein 2.

## Always
- Update `docs/progress.md` after meaningful work.
- Keep changes small, testable, and focused.
- Preserve vanilla files; never modify the game installation archives directly.
- Important/key/scripted content is excluded unless a future design explicitly enables it.
- Determinism matters: the same game version + options + seed must produce the same result.
- Fail safely. Never package/install partial or invalid output.

## Design
- `docs/design.md` is the source of truth for architecture and behavior.
- Read it once at project start, then only when needed for context or when the plan references it.
- Do not repeatedly reread the entire design on every task.

## Coding
- Python 3.12+.
- Prefer clear modules over large files.
- UI must not contain core randomizer logic.
- Add tests for parsers, shuffling, protection rules, and patch generation.
- Do not hardcode developer-machine absolute paths.
