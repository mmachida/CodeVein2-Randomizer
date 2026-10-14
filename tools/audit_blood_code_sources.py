"""DEV-only authoritative inventory and extraction using the production extractor."""
from __future__ import annotations

import json
from pathlib import Path

from cv2randomizer.core.config import SettingsStore
from cv2randomizer.game.asset_extraction import IoStoreAssetExtractor, materialize_extracted_assets


# Start the application
def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    output = repo / "workspace" / "blood-code-runtime-audit"
    output.mkdir(parents=True, exist_ok=True)
    settings = SettingsStore().load()
    if not settings.game_path:
        raise SystemExit("Select the installed game in the app first.")
    extractor = IoStoreAssetExtractor()
    inventory = extractor.inspect_package_inventory(Path(settings.game_path))
    paths = sorted(value for _, value in inventory.package_path_spellings)
    candidates = [path for path in paths if (
        path.startswith("/Game/Quest/")
        or "/SmallScenario/Blueprint/BloodCode/" in path
        or "/TalkToLou/" in path
        or path.rsplit("/", 1)[-1].startswith("DA_BloodCode_")
    )]
    (output / "installed-package-paths.json").write_text(json.dumps(paths, indent=2), encoding="utf-8")
    (output / "candidate-packages.json").write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    assets = extractor.extract_assets(
        Path(settings.game_path), candidates,
        game_version="installed-blood-code-audit", data_revision="blood-code-audit-v1",
        progress=lambda message, current, total: print(f"{current}/{total} {message}", flush=True),
    )
    materialized = materialize_extracted_assets(assets, output / "source" / "Content")
    (output / "extracted-packages.json").write_text(json.dumps([
        {"virtual_path": asset.virtual_path, "files": [str(path) for path in asset.files]}
        for asset in materialized
    ], indent=2), encoding="utf-8")
    print(f"Extracted {len(materialized)} candidate packages into {output}", flush=True)


if __name__ == "__main__":
    main()
