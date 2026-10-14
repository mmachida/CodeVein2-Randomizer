param(
    [string]$Python = ".venv\Scripts\python.exe",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonPath = Join-Path $ProjectRoot $Python
$ReleaseVersion = (& $PythonPath -c "import tomllib; from pathlib import Path; print(tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])").Trim()
if ($LASTEXITCODE -ne 0 -or $ReleaseVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "Could not read a valid project version from pyproject.toml."
}
$DisplayVersion = "v$ReleaseVersion"
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = "release\$ReleaseVersion"
}
$ReleaseRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $OutputDirectory))
$ProjectPrefix = $ProjectRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $ReleaseRoot.StartsWith($ProjectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Release output must stay inside the project directory: $ReleaseRoot"
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$ScratchParent = Join-Path $ProjectRoot "workspace\release-build"
$ScratchRoot = Join-Path $ScratchParent $Timestamp
$HelperOutput = Join-Path $ScratchRoot "helper"
$DistRoot = Join-Path $ScratchRoot "dist"
$WorkRoot = Join-Path $ScratchRoot "pyinstaller-work"
$SpecRoot = Join-Path $ScratchRoot "spec"
$StageRoot = Join-Path $ScratchRoot "release-stage"
$ZipValidationRoot = Join-Path $ScratchRoot "zip-validation"
$ZipName = "CodeVein2Randomizer-$DisplayVersion-win-x64.zip"
$ExecutableName = "CodeVein2Randomizer-$DisplayVersion.exe"
$ZipPath = Join-Path $ReleaseRoot $ZipName
$ChecksumsPath = Join-Path $ReleaseRoot "SHA256SUMS.txt"
$DataPath = Join-Path $ProjectRoot "data"
$IconPath = Join-Path $ProjectRoot "ico\icon.ico"
$IconPngPath = Join-Path $ProjectRoot "ico\icon.png"
$MappingPath = Join-Path $ProjectRoot "workspace\research_assets\CV2.usmap"
$OodlePath = Join-Path $ProjectRoot "tools\oo2core_9_win64.dll"
$RetocRoot = Join-Path $ProjectRoot "workspace\tools\retoc-v0.1.5"
$RetocPath = Join-Path $RetocRoot "retoc.exe"
$RetocLicense = Join-Path $RetocRoot "LICENSE"
$HelperProject = Join-Path $ProjectRoot "tools\uassetapi-worldloot\UAssetAPIWorldLoot.csproj"
$EntryPoint = Join-Path $ProjectRoot "src\cv2randomizer\main.py"
$UAssetLicense = Join-Path $env:USERPROFILE ".nuget\packages\uassetapi\1.1.0\LICENSE"
$NewtonsoftLicense = Join-Path $env:USERPROFILE ".nuget\packages\newtonsoft.json\13.0.3\LICENSE.md"
$BuildSucceeded = $false

foreach ($RequiredFile in @(
    $PythonPath, $IconPath, $IconPngPath, $MappingPath, $OodlePath, $RetocPath,
    $RetocLicense, $HelperProject, $UAssetLicense, $NewtonsoftLicense, $EntryPoint
)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required release input is missing: $RequiredFile"
    }
}
if ((Get-Item -LiteralPath $OodlePath).Length -le 0) {
    throw "The local Oodle DLL is empty: $OodlePath"
}
if ((Test-Path -LiteralPath $ZipPath) -or (Test-Path -LiteralPath $ChecksumsPath)) {
    throw "The $DisplayVersion release output already exists; preserve it and choose a fresh output directory: $ReleaseRoot"
}
if (Test-Path -LiteralPath $ScratchRoot) {
    throw "The timestamped build scratch path already exists: $ScratchRoot"
}

New-Item -ItemType Directory -Force -Path $HelperOutput, $DistRoot, $WorkRoot, $SpecRoot, $StageRoot, $ZipValidationRoot, $ReleaseRoot | Out-Null
$PythonBase = (& $PythonPath -c "import sys; print(sys.base_prefix)").Trim()
$PythonSitePackages = (Resolve-Path (Join-Path (Split-Path $PythonPath -Parent) "..\Lib\site-packages")).Path
$PythonLicenseRoot = Join-Path $ScratchRoot "python-licenses"
New-Item -ItemType Directory -Force -Path $PythonLicenseRoot | Out-Null
foreach ($Distribution in Get-ChildItem -LiteralPath $PythonSitePackages -Directory -Filter "*.dist-info") {
    $LicenseDirectory = Join-Path $Distribution.FullName "licenses"
    if (Test-Path -LiteralPath $LicenseDirectory -PathType Container) {
        Copy-Item -LiteralPath $LicenseDirectory -Destination (Join-Path $PythonLicenseRoot $Distribution.Name) -Recurse
    }
}
$PythonLicense = Join-Path $PythonBase "LICENSE.txt"
if (Test-Path -LiteralPath $PythonLicense -PathType Leaf) {
    Copy-Item -LiteralPath $PythonLicense -Destination (Join-Path $PythonLicenseRoot "Python-LICENSE.txt")
}

Push-Location $ProjectRoot
try {
    $PyInstallerVersion = (& $PythonPath -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed in $PythonPath."
    }

    $DotnetArguments = @(
        "publish", $HelperProject,
        "--configuration", "Release",
        "--runtime", "win-x64",
        "--self-contained", "true",
        "--property:PublishSingleFile=true",
        "--property:IncludeAllContentForSelfExtract=true",
        "--property:IncludeNativeLibrariesForSelfExtract=true",
        "--property:EnableCompressionInSingleFile=true",
        "--output", $HelperOutput
    )
    & dotnet @DotnetArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Could not publish the self-contained UAssetAPI helper."
    }
    $HelperExe = Join-Path $HelperOutput "UAssetAPIWorldLoot.exe"
    if (-not (Test-Path -LiteralPath $HelperExe -PathType Leaf)) {
        throw "Published UAssetAPI helper executable is missing."
    }

    $SpecArguments = @(
        "--onefile", "--windowed",
        "--name", "CodeVein2Randomizer-$DisplayVersion",
        "--paths", (Join-Path $ProjectRoot "src"),
        "--specpath", $SpecRoot,
        "--icon", $IconPath,
        "--add-data", "$DataPath;data",
        "--add-data", "$IconPath;ico",
        "--add-data", "$IconPngPath;ico",
        "--add-data", "$MappingPath;workspace/research_assets",
        "--add-binary", "$RetocPath;tools",
        "--add-binary", "$HelperExe;tools",
        "--add-binary", "$OodlePath;tools",
        $EntryPoint
    )
    $MakeSpec = Join-Path (Split-Path $PythonPath -Parent) "pyi-makespec.exe"
    & $MakeSpec @SpecArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Could not generate the PyInstaller spec file."
    }

    $SpecPath = Join-Path $SpecRoot "CodeVein2Randomizer-$DisplayVersion.spec"
    if (-not (Test-Path -LiteralPath $SpecPath -PathType Leaf)) {
        throw "Generated PyInstaller spec file is missing: $SpecPath"
    }
    $SpecText = Get-Content -LiteralPath $SpecPath -Raw
    $InsertionPoint = "pyz = PYZ("
    if (-not $SpecText.Contains($InsertionPoint)) {
        throw "PyInstaller spec layout changed; cannot apply the ICU exclusion safely."
    }
    $IcuFilter = @(
        "import os",
        "from PyInstaller.building.datastruct import TOC",
        "",
        "a.binaries = TOC(entry for entry in a.binaries if not os.path.basename(entry[0]).casefold().startswith('icu'))",
        ""
    ) -join [Environment]::NewLine
    Set-Content -LiteralPath $SpecPath -Value $SpecText.Replace($InsertionPoint, $IcuFilter + $InsertionPoint) -Encoding UTF8

    $PyInstallerArguments = @("--noconfirm", "--clean", "--distpath", $DistRoot, "--workpath", $WorkRoot, $SpecPath)
    & $PythonPath -m PyInstaller @PyInstallerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller one-file build failed with exit code $LASTEXITCODE."
    }

    $BuiltExecutable = Join-Path $DistRoot $ExecutableName
    if (-not (Test-Path -LiteralPath $BuiltExecutable -PathType Leaf)) {
        throw "PyInstaller one-file executable was not created: $BuiltExecutable"
    }

    $StageExecutable = Join-Path $StageRoot $ExecutableName
    Copy-Item -LiteralPath $BuiltExecutable -Destination $StageExecutable
    $ReadmeLines = @(
        "Code Vein 2 Randomizer $DisplayVersion",
        "Windows 10/11, 64-bit",
        "",
        "1. Extract this ZIP.",
        "2. Run $ExecutableName.",
        "3. Select the Code Vein 2 installation root. For Steam, this is usually",
        "   ...\steamapps\common\CodeVein2, the folder containing CodeVein2.",
        "4. Use Extract Game Data if the app requests it, then choose a seed and options.",
        "5. Click Randomize.",
        "",
        "The executable includes Python, Qt, Retoc, the UAssetAPI helper, CV2.usmap,",
        "the app data catalogs, the icon, and the local Oodle runtime. No extra",
        "runtime folders or dependency downloads are needed.",
        "",
        "The game must already be installed. Original game assets are read from",
        "the selected game installation. Generated cache and output are stored",
        "under your Windows user data folders.",
        "",
        "Some game versions and rewards may not have gameplay validation."
    )
    Set-Content -LiteralPath (Join-Path $StageRoot "README.txt") -Value $ReadmeLines -Encoding UTF8

    $Notices = [System.Text.StringBuilder]::new()
    [void]$Notices.AppendLine("Third-party notices for Code Vein 2 Randomizer $DisplayVersion")
    [void]$Notices.AppendLine()
    foreach ($LicenseSpec in @(
        @{ Name = "Retoc CLI 0.1.5"; Path = $RetocLicense },
        @{ Name = "UAssetAPI 1.1.0"; Path = $UAssetLicense },
        @{ Name = "Newtonsoft.Json 13.0.3"; Path = $NewtonsoftLicense }
    )) {
        [void]$Notices.AppendLine(("=" * 72))
        [void]$Notices.AppendLine($LicenseSpec.Name)
        [void]$Notices.AppendLine(("=" * 72))
        [void]$Notices.AppendLine((Get-Content -LiteralPath $LicenseSpec.Path -Raw))
        [void]$Notices.AppendLine()
    }
    foreach ($LicenseFile in Get-ChildItem -LiteralPath $PythonLicenseRoot -File -Recurse | Sort-Object FullName) {
        $RelativeLicense = $LicenseFile.FullName.Substring($PythonLicenseRoot.Length).TrimStart('\')
        [void]$Notices.AppendLine(("=" * 72))
        [void]$Notices.AppendLine(("Python dependency: " + $RelativeLicense))
        [void]$Notices.AppendLine(("=" * 72))
        [void]$Notices.AppendLine((Get-Content -LiteralPath $LicenseFile.FullName -Raw))
        [void]$Notices.AppendLine()
    }
    [void]$Notices.AppendLine("The app bundles oo2core_9_win64.dll for the local Retoc compression fallback.")
    [void]$Notices.AppendLine("This runtime file is supplied locally and is not distributed through source control.")
    Set-Content -LiteralPath (Join-Path $StageRoot "THIRD-PARTY-NOTICES.txt") -Value $Notices.ToString() -Encoding UTF8

    $SmokeArguments = @{
        FilePath = $StageExecutable
        ArgumentList = "--startup-smoke-test"
        WorkingDirectory = $StageRoot
        PassThru = $true
        WindowStyle = "Hidden"
        Wait = $true
    }
    $Smoke = Start-Process @SmokeArguments
    if ($Smoke.ExitCode -ne 0) {
        throw "Packaged one-file startup/resource smoke test failed with exit code $($Smoke.ExitCode)."
    }

    $SourceUsmapHash = (Get-FileHash -LiteralPath $MappingPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $SourceIconHash = (Get-FileHash -LiteralPath $IconPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $SourceOodleHash = (Get-FileHash -LiteralPath $OodlePath -Algorithm SHA256).Hash.ToLowerInvariant()

    Compress-Archive -Path (Join-Path $StageRoot "*") -DestinationPath $ZipPath -CompressionLevel Optimal
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        throw "Release ZIP was not created."
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $Entries = @($Archive.Entries | ForEach-Object { $_.FullName } | Sort-Object)
        $ExpectedEntries = @($ExecutableName, "README.txt", "THIRD-PARTY-NOTICES.txt")
        if (($Entries -join "|") -ne ($ExpectedEntries -join "|")) {
            throw "Release ZIP has unexpected contents: $($Entries -join ', ')"
        }
        foreach ($EntryName in $ExpectedEntries) {
            $Entry = $Archive.GetEntry($EntryName)
            $ExtractPath = Join-Path $ZipValidationRoot $EntryName
            $InputStream = $Entry.Open()
            $OutputStream = [System.IO.File]::Create($ExtractPath)
            try {
                $InputStream.CopyTo($OutputStream)
            } finally {
                $OutputStream.Dispose()
                $InputStream.Dispose()
            }
        }
    } finally {
        $Archive.Dispose()
    }

    $BuiltHash = (Get-FileHash -LiteralPath $BuiltExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    $ZippedHash = (Get-FileHash -LiteralPath (Join-Path $ZipValidationRoot $ExecutableName) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($BuiltHash -ne $ZippedHash) {
        throw "The executable extracted from the ZIP does not match the built executable."
    }

    $ZipSmokeArguments = @{
        FilePath = (Join-Path $ZipValidationRoot $ExecutableName)
        ArgumentList = "--startup-smoke-test"
        WorkingDirectory = $ZipValidationRoot
        PassThru = $true
        WindowStyle = "Hidden"
        Wait = $true
    }
    $ZipSmoke = Start-Process @ZipSmokeArguments
    if ($ZipSmoke.ExitCode -ne 0) {
        throw "Executable extracted from the release ZIP failed its startup/resource smoke test."
    }

    $ZipHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "SHA256  $ZipName  $ZipHash" | Set-Content -LiteralPath $ChecksumsPath -Encoding ASCII

    $ZipSize = (Get-Item -LiteralPath $ZipPath).Length
    $BuiltSize = (Get-Item -LiteralPath $BuiltExecutable).Length
    Write-Output "Version: $DisplayVersion"
    Write-Output "Release ZIP: $ZipPath"
    Write-Output "ZIP size: $ZipSize bytes"
    Write-Output "EXE size: $BuiltSize bytes"
    Write-Output "EXE SHA-256: $BuiltHash"
    Write-Output "ZIP SHA-256: $ZipHash"
    Write-Output "ZIP entries: $($Entries -join ', ')"
    Write-Output "Embedded CV2.usmap SHA-256: $SourceUsmapHash"
    Write-Output "Embedded icon SHA-256: $SourceIconHash"
    Write-Output "Embedded Oodle SHA-256: $SourceOodleHash"
    $BuildSucceeded = $true
} finally {
    Pop-Location
    if ($BuildSucceeded -and (Test-Path -LiteralPath $ScratchRoot -PathType Container)) {
        $ResolvedScratch = (Resolve-Path -LiteralPath $ScratchRoot).Path
        $ResolvedScratchParent = (Resolve-Path -LiteralPath $ScratchParent).Path.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
        $ResolvedScratchLeaf = Split-Path -Leaf $ResolvedScratch
        if (
            $ResolvedScratch.StartsWith($ResolvedScratchParent, [System.StringComparison]::OrdinalIgnoreCase) -and
            $ResolvedScratchLeaf -eq $Timestamp
        ) {
            Remove-Item -LiteralPath $ResolvedScratch -Recurse -Force
        } else {
            Write-Warning "Kept build scratch because its resolved path failed the cleanup guard: $ResolvedScratch"
        }
    } elseif (Test-Path -LiteralPath $ScratchRoot -PathType Container) {
        Write-Warning "Build did not complete; kept diagnostic scratch at $ScratchRoot"
    }
}
