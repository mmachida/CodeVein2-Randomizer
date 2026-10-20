# Code Vein 2 Randomizer

[![Version](https://img.shields.io/github/v/release/mmachida/CodeVein2-Randomizer?label=version)](https://github.com/mmachida/CodeVein2-Randomizer/releases/latest) ![Platform](https://img.shields.io/badge/platform-Windows%20x64-0078D4)

Code Vein 2 Randomizer is a tool that creates a MOD to randomize items in Code Vein II and DLC (Mask of Idris). Choose a seed and the options you want, then click **Randomize**. The original game archives stay untouched.
Using the same seed, settings, and supported game version gives you the same result.

<img width="997" height="498" alt="Captura de tela 2026-10-20 174504" src="https://github.com/user-attachments/assets/2896ca88-1d3b-4027-8704-b379431ca40e" />
<img width="750" height="415" alt="image" src="https://github.com/user-attachments/assets/1afb859e-951a-475c-bdfb-4679f3a4b2e8" />

## What it can randomize

- World pickups and reviewed one-time rewards, including the supported Hot Springs and quest rewards.
- Finite shop stock and supported NPC Trader rewards.
- Healing items and renewable ingredients. Ingredients shuffle only with other renewable ingredients.
- Supported enemy drops, Blood Code rewards, mastery Boosters, and Booster Extractor recipes.
- Selected starting equipment and a few optional quality-of-life settings.

Only rewards the app has checked are included. Unsupported scripted rewards stay as they are. Pre-order rewards, MagMell's Tool Shop, and unlimited shop stock are left alone. Key-item and general quest-item randomization are still in progress and cannot be switched on in the app.

The app skips optional content that is not present in the installed game files. It does not check whether DLC is owned through a store.

<img width="1280" height="720" alt="asd" src="https://github.com/user-attachments/assets/dfaa730b-4edc-4b7a-a83f-1fa4354ddc69" />

## Dependencies

If you download the ready-to-run Windows release, you need:

- Windows 10 or 11, 64-bit.
- Code Vein II installed.

Python and .NET do not need to be installed for the ready-to-run release.

To run the project from source, you also need:

- Python 3.12 or newer.
- The .NET 10 SDK. The source app uses it to run its asset helper when extracting and building a mod.
- The checkout includes `tools/oo2core_9_win64.dll`, which Retoc uses to read Oodle-compressed game archives. You do not need to copy this DLL from the game installation.

The checkout already includes the Retoc command-line tool and the `CV2.usmap` mapping file. Python installs the app's runtime packages—PySide6, platformdirs, and psutil—from `pyproject.toml`.

For tests and release packaging, install the optional `dev` and `app` packages too. They add pytest and PyInstaller. Building a release also needs the .NET 10 SDK and the local Retoc input described in the build section below.

Only game versions listed in `data/versions/` are marked as officially supported. The app may let you inspect another installation, but that does not mean its generated mod has been validated for that version.

## Run from source

Open PowerShell in the project folder and set up the environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m cv2randomizer.main
```

To run tests or build the Windows app, install the optional packages:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,app]"
```

Once the environment is ready, start the app with the command above. Choose your Code Vein II folder and click **Extract Game Data** if the app asks. The first extraction can take a few minutes. Later runs reuse the cache while the game's archives stay the same. Pick a seed and settings, then click **Randomize**.

## Remove the mod

Click **Uninstall Randomizer** in the app. It removes the files owned by this Randomizer and leaves other mods in `Paks/~mods` alone. You can also delete this Randomizer's mod files and `Spoiler.txt` from that folder yourself. The original game archives are not changed.

## Checks and release builds

Run the project's quick audit from PowerShell:

```powershell
.\.venv\Scripts\python.exe tools\final_audit.py
```

For the full Python test suite, use:

```powershell
.\.venv\Scripts\python.exe tools\final_audit.py --full
```

These checks do not replace testing Extract and Randomize with a real game installation. Real-game and gameplay checks still need to be done manually.

To build a Windows release, set up the optional Python packages, install the .NET 10 SDK, and make sure the pinned Retoc CLI and its license are in `workspace/tools/retoc-v0.1.5/`. The build script uses the included Oodle DLL at `tools/oo2core_9_win64.dll` and needs NuGet license files after restore. Then run:

```powershell
.\scripts\build_release.ps1
```

The script builds under `release/<version>/` and runs startup and resource checks. Include Retoc's license and the generated third-party notices with distributed builds.

## License

The original source code and documentation are licensed under the MIT License; see the root `LICENSE` file.

The project MIT license does not cover third-party software or Code Vein II files and data. Retoc has its own MIT license in `workspace/tools/retoc-v0.1.5/LICENSE`; the Oodle DLL, `CV2.usmap`, and game-derived catalog data remain subject to their respective owners' terms. Check those terms before reusing or redistributing those files.