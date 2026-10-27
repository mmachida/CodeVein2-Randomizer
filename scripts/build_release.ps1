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
$ScratchParent = Join-Path $ProjectRoot "Delete\release-build"
$ScratchRoot = Join-Path $ScratchParent $Timestamp
$HelperSourceRoot = Join-Path $ScratchRoot "helper-source"
$HelperOutput = Join-Path $ScratchRoot "helper"
$BuildHelperProject = Join-Path $HelperSourceRoot "UAssetAPIWorldLoot.csproj"
$LauncherProject = Join-Path $ProjectRoot "launcher\CodeVein2Randomizer.Launcher\CodeVein2Randomizer.Launcher.csproj"
$LauncherOutput = Join-Path $ScratchRoot "dotnet-launcher"
$LauncherObjRoot = Join-Path $ScratchRoot "dotnet-launcher-obj"
$LauncherBaseIntermediateOutput = $LauncherObjRoot + [System.IO.Path]::DirectorySeparatorChar
$LauncherBinRoot = Join-Path $ScratchRoot "dotnet-launcher-bin"
$LauncherBaseOutputPath = $LauncherBinRoot + [System.IO.Path]::DirectorySeparatorChar
$PayloadArchive = Join-Path $ScratchRoot "randomizer-payload.zip"
$DistRoot = Join-Path $ScratchRoot "dist"
$WorkRoot = Join-Path $ScratchRoot "pyinstaller-work"
$SpecRoot = Join-Path $ScratchRoot "spec"
$VersionFile = Join-Path $ScratchRoot "version_info.txt"
$StageRoot = Join-Path $ScratchRoot "release-stage"
$ZipValidationRoot = Join-Path $ScratchRoot "zip validation café"
$RuntimeTempRoot = Join-Path $ScratchRoot "runtime temp"
$NoticesPath = Join-Path $ScratchRoot "THIRD-PARTY-NOTICES.txt"
$ZipName = "CodeVein2Randomizer-$DisplayVersion-win-x64.zip"
$ExecutableName = "CodeVein2Randomizer-$DisplayVersion.exe"
$ZipPath = Join-Path $ReleaseRoot $ZipName
$ChecksumsPath = Join-Path $ReleaseRoot "SHA256SUMS.txt"
$BuildManifestPath = Join-Path $ReleaseRoot "BUILD-MANIFEST.json"
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
$NugetPackagesRoot = Join-Path $ScratchRoot "nuget-packages"
$UAssetLicense = Join-Path $NugetPackagesRoot "uassetapi\1.1.0\LICENSE"
$NewtonsoftLicense = Join-Path $NugetPackagesRoot "newtonsoft.json\13.0.3\LICENSE.md"
$BuildSucceeded = $false

foreach ($RequiredFile in @(
    $PythonPath, $IconPath, $IconPngPath, $MappingPath, $OodlePath, $RetocPath,
    $RetocLicense, $HelperProject, $LauncherProject, $EntryPoint
)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required release input is missing: $RequiredFile"
    }
}
if ((Get-Item -LiteralPath $OodlePath).Length -le 0) {
    throw "The local Oodle DLL is empty: $OodlePath"
}
if ((Test-Path -LiteralPath $ZipPath) -or (Test-Path -LiteralPath $ChecksumsPath) -or (Test-Path -LiteralPath $BuildManifestPath)) {
    throw "The $DisplayVersion release output already exists; preserve it and choose a fresh output directory: $ReleaseRoot"
}
if (Test-Path -LiteralPath $ScratchRoot) {
    throw "The timestamped build scratch path already exists: $ScratchRoot"
}

New-Item -ItemType Directory -Force -Path $HelperSourceRoot, $HelperOutput, $LauncherOutput, $DistRoot, $WorkRoot, $SpecRoot, $StageRoot, $ZipValidationRoot, $ReleaseRoot | Out-Null
foreach ($HelperSourceFile in Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "tools\uassetapi-worldloot") -File | Where-Object {
    $_.Extension -eq ".cs" -or $_.Name -in @("UAssetAPIWorldLoot.csproj", "packages.lock.json")
}) {
    Copy-Item -LiteralPath $HelperSourceFile.FullName -Destination $HelperSourceRoot
}
if (-not (Test-Path -LiteralPath (Join-Path $HelperSourceRoot "packages.lock.json") -PathType Leaf)) {
    throw "The pinned UAssetAPI NuGet lock file is missing from the build source."
}
$OriginalNugetPackages = $env:NUGET_PACKAGES
$OriginalDotnetNoLogo = $env:DOTNET_NOLOGO
$OriginalDotnetTelemetry = $env:DOTNET_CLI_TELEMETRY_OPTOUT
try {
    $env:NUGET_PACKAGES = $NugetPackagesRoot
    $env:DOTNET_NOLOGO = "1"
    $env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
    $DotnetRestoreArguments = @(
        "restore", $BuildHelperProject,
        "--runtime", "win-x64",
        "--use-lock-file",
        "--property:RestoreLockedMode=true"
    )
    & dotnet @DotnetRestoreArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restore the UAssetAPI helper from its locked dependency graph."
    }
} finally {
    $env:NUGET_PACKAGES = $OriginalNugetPackages
    $env:DOTNET_NOLOGO = $OriginalDotnetNoLogo
    $env:DOTNET_CLI_TELEMETRY_OPTOUT = $OriginalDotnetTelemetry
}
foreach ($LicenseFile in @($UAssetLicense, $NewtonsoftLicense)) {
    if (-not (Test-Path -LiteralPath $LicenseFile -PathType Leaf)) {
        throw "Required dependency license is missing from the locked NuGet packages: $LicenseFile"
    }
}
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
[void]$Notices.AppendLine("The project tracks this runtime file at tools/oo2core_9_win64.dll.")
[void]$Notices.AppendLine("Its original source and redistribution terms were not independently verified for this build.")
Set-Content -LiteralPath $NoticesPath -Value $Notices.ToString() -Encoding UTF8

$VersionParts = @($ReleaseVersion.Split('.') | ForEach-Object { [int]$_ })
$NumericVersion = "{0}.{1}.{2}.0" -f $VersionParts[0], $VersionParts[1], $VersionParts[2]
$TupleVersion = "{0}, {1}, {2}, 0" -f $VersionParts[0], $VersionParts[1], $VersionParts[2]
$VersionResource = @"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($TupleVersion), prodvers=($TupleVersion),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('FileDescription', 'Code Vein 2 Randomizer'),
        StringStruct('FileVersion', '$NumericVersion'),
        StringStruct('InternalName', 'CodeVein2Randomizer'),
        StringStruct('OriginalFilename', '$ExecutableName'),
        StringStruct('ProductName', 'Code Vein 2 Randomizer'),
        StringStruct('ProductVersion', '$NumericVersion'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
Set-Content -LiteralPath $VersionFile -Value $VersionResource -Encoding UTF8

Push-Location $ProjectRoot
try {
    $PyInstallerVersion = (& $PythonPath -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed in $PythonPath."
    }

    $DotnetArguments = @(
        "publish", $BuildHelperProject,
        "--configuration", "Release",
        "--runtime", "win-x64",
        "--self-contained", "true",
        "--no-restore",
        "--property:RestoreLockedMode=true",
        "--property:PublishSingleFile=false",
        "--property:DebugType=None",
        "--property:DebugSymbols=false",
        "--output", $HelperOutput
    )
    $OriginalNugetPackages = $env:NUGET_PACKAGES
    $OriginalDotnetNoLogo = $env:DOTNET_NOLOGO
    $OriginalDotnetTelemetry = $env:DOTNET_CLI_TELEMETRY_OPTOUT
    try {
        $env:NUGET_PACKAGES = $NugetPackagesRoot
        $env:DOTNET_NOLOGO = "1"
        $env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
        & dotnet @DotnetArguments
        $DotnetPublishExitCode = $LASTEXITCODE
    } finally {
        $env:NUGET_PACKAGES = $OriginalNugetPackages
        $env:DOTNET_NOLOGO = $OriginalDotnetNoLogo
        $env:DOTNET_CLI_TELEMETRY_OPTOUT = $OriginalDotnetTelemetry
    }
    if ($DotnetPublishExitCode -ne 0) {
        throw "Could not publish the self-contained UAssetAPI helper."
    }
    $HelperExe = Join-Path $HelperOutput "UAssetAPIWorldLoot.exe"
    foreach ($HelperRequiredFile in @(
        $HelperExe,
        (Join-Path $HelperOutput "UAssetAPIWorldLoot.deps.json"),
        (Join-Path $HelperOutput "UAssetAPIWorldLoot.runtimeconfig.json")
    )) {
        if (-not (Test-Path -LiteralPath $HelperRequiredFile -PathType Leaf)) {
            throw "Published UAssetAPI helper file is missing: $HelperRequiredFile"
        }
    }

    $SpecArguments = @(
        "--onedir", "--windowed", "--noupx",
        "--name", "CodeVein2Randomizer-$DisplayVersion",
        "--version-file", $VersionFile,
        "--paths", (Join-Path $ProjectRoot "src"),
        "--specpath", $SpecRoot,
        "--icon", $IconPath,
        "--add-data", "$DataPath;data",
        "--add-data", "$IconPath;ico",
        "--add-data", "$IconPngPath;ico",
        "--add-data", "$MappingPath;workspace/research_assets",
        "--add-data", "$NoticesPath;licenses",
        "--add-binary", "$RetocPath;tools",
        "--add-binary", "$OodlePath;tools",
        "--exclude-module", "PySide6.QtNetwork",
        "--exclude-module", "PySide6.QtNetworkAuth",
        "--exclude-module", "PySide6.QtQmlNetwork",
        "--exclude-module", "PySide6.QtPdf",
        "--exclude-module", "PySide6.QtPdfWidgets",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtVirtualKeyboard",
        $EntryPoint
    )
    foreach ($HelperFile in Get-ChildItem -LiteralPath $HelperOutput -File -Recurse | Where-Object {
        $_.Extension -in @(".exe", ".dll", ".json") -and $_.Name -ne "createdump.exe"
    }) {
        $HelperRelativePath = $HelperFile.FullName.Substring($HelperOutput.Length).TrimStart('\').Replace('\', '/')
        $HelperDirectory = (Split-Path -Parent $HelperRelativePath).Replace('\', '/')
        $HelperDestination = if ([string]::IsNullOrWhiteSpace($HelperDirectory)) { "tools" } else { "tools/$HelperDirectory" }
        $HelperArgument = "$($HelperFile.FullName);$HelperDestination"
        if ($HelperFile.Extension -eq ".json") {
            $SpecArguments += @("--add-data", $HelperArgument)
        } else {
            $SpecArguments += @("--add-binary", $HelperArgument)
        }
    }
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
        throw "PyInstaller spec layout changed; cannot apply the runtime dependency filter safely."
    }
    $RuntimeFilter = @(
        "import os",
        "from PyInstaller.building.datastruct import TOC",
        "",
        "excluded_runtime_binaries = {",
        "    'qt6network.dll', 'qt6networkauth.dll', 'qt6qmlnetwork.dll',",
        "    'qt6pdf.dll', 'qt6qml.dll', 'qt6qmlmeta.dll', 'qt6qmlmodels.dll', 'qt6qmlworkerscript.dll',",
        "    'qt6quick.dll', 'qt6virtualkeyboard.dll', 'qpdf.dll', 'qtvirtualkeyboardplugin.dll',",
        "    'qtnetwork.pyd', 'qtnetworkauth.pyd', 'qtqmlnetwork.pyd',",
        "    'qnetworklistmanager.dll', 'qcertonlybackend.dll', 'qopensslbackend.dll', 'qschannelbackend.dll',",
        "    'libcrypto-3-x64.dll', 'libssl-3-x64.dll',",
        "}",
        "a.binaries = TOC(entry for entry in a.binaries if not os.path.basename(entry[0]).casefold().startswith('icu') and os.path.basename(entry[0]).casefold() not in excluded_runtime_binaries)",
        "a.datas = TOC(entry for entry in a.datas if os.path.basename(entry[0]).casefold() not in excluded_runtime_binaries)",
        ""
    ) -join [Environment]::NewLine
    Set-Content -LiteralPath $SpecPath -Value $SpecText.Replace($InsertionPoint, $RuntimeFilter + $InsertionPoint) -Encoding UTF8

    $PyInstallerArguments = @("--noconfirm", "--clean", "--distpath", $DistRoot, "--workpath", $WorkRoot, $SpecPath)
    $OriginalBuildPath = $env:PATH
    $OriginalPyInstallerConfig = $env:PYINSTALLER_CONFIG_DIR
    $BuildPathEntries = @(
        (Join-Path $env:SystemRoot "System32"),
        $PythonBase,
        (Join-Path $PythonBase "DLLs"),
        (Split-Path $PythonPath -Parent)
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Container }
    $env:PATH = $BuildPathEntries -join [System.IO.Path]::PathSeparator
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $ScratchRoot "pyinstaller-config"
    try {
        & $PythonPath -m PyInstaller @PyInstallerArguments
        $PyInstallerExitCode = $LASTEXITCODE
    } finally {
        $env:PATH = $OriginalBuildPath
        $env:PYINSTALLER_CONFIG_DIR = $OriginalPyInstallerConfig
    }
    if ($PyInstallerExitCode -ne 0) {
        throw "PyInstaller onedir build failed with exit code $PyInstallerExitCode."
    }

    $BuiltAppDirectory = Join-Path $DistRoot "CodeVein2Randomizer-$DisplayVersion"
    $BundledAppExecutable = Join-Path $BuiltAppDirectory $ExecutableName
    if (-not (Test-Path -LiteralPath $BundledAppExecutable -PathType Leaf)) {
        throw "PyInstaller onedir executable was not created: $BundledAppExecutable"
    }

    $PackagedHelper = Join-Path $BuiltAppDirectory "_internal\tools\UAssetAPIWorldLoot.exe"
    if (-not (Test-Path -LiteralPath $PackagedHelper -PathType Leaf)) {
        throw "The onedir app is missing its UAssetAPI helper: $PackagedHelper"
    }
    $HelperFixtureOutput = Join-Path $ScratchRoot "helper-fixture.json"
    $HelperFixtureError = Join-Path $ScratchRoot "helper-fixture-error.txt"
    $HelperFixture = Start-Process -FilePath $PackagedHelper -ArgumentList "test-raw-article-grant-fixtures" `
        -WorkingDirectory $ZipValidationRoot -PassThru -WindowStyle Hidden -Wait `
        -RedirectStandardOutput $HelperFixtureOutput -RedirectStandardError $HelperFixtureError
    if ($HelperFixture.ExitCode -ne 0) {
        $HelperErrorText = if (Test-Path -LiteralPath $HelperFixtureError) { Get-Content -LiteralPath $HelperFixtureError -Raw } else { "" }
        throw "Packaged UAssetAPI helper fixture check failed ($($HelperFixture.ExitCode)): $HelperErrorText"
    }
    $FixtureResults = Get-Content -LiteralPath $HelperFixtureOutput -Raw | ConvertFrom-Json
    if (@($FixtureResults).Count -ne 9 -or @($FixtureResults | Where-Object { -not $_.UnrelatedRawBytesPreserved }).Count -gt 0) {
        throw "Packaged UAssetAPI helper fixture check returned incomplete or invalid results."
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $BuiltAppDirectory,
        $PayloadArchive,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )
    if (-not (Test-Path -LiteralPath $PayloadArchive -PathType Leaf)) {
        throw "The intermediate application payload archive was not created."
    }

    $OriginalNugetPackages = $env:NUGET_PACKAGES
    $OriginalDotnetNoLogo = $env:DOTNET_NOLOGO
    $OriginalDotnetTelemetry = $env:DOTNET_CLI_TELEMETRY_OPTOUT
    try {
        $env:NUGET_PACKAGES = $NugetPackagesRoot
        $env:DOTNET_NOLOGO = "1"
        $env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
        $LauncherRestoreArguments = @(
            "restore", $LauncherProject,
            "--runtime", "win-x64",
            "--property:BaseIntermediateOutputPath=$LauncherBaseIntermediateOutput",
            "--property:MSBuildProjectExtensionsPath=$LauncherBaseIntermediateOutput",
            "--property:BaseOutputPath=$LauncherBaseOutputPath"
        )
        & dotnet @LauncherRestoreArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Could not restore the self-contained .NET launcher."
        }

        $LauncherPublishArguments = @(
            "publish", $LauncherProject,
            "--configuration", "Release",
            "--runtime", "win-x64",
            "--self-contained", "true",
            "--no-restore",
            "--property:Version=$ReleaseVersion",
            "--property:AssemblyName=CodeVein2Randomizer-$DisplayVersion",
            "--property:PublishSingleFile=true",
            "--property:IncludeNativeLibrariesForSelfExtract=true",
            "--property:DebugType=None",
            "--property:DebugSymbols=false",
            "--property:PayloadArchive=$PayloadArchive",
            "--property:BaseIntermediateOutputPath=$LauncherBaseIntermediateOutput",
            "--property:MSBuildProjectExtensionsPath=$LauncherBaseIntermediateOutput",
            "--property:BaseOutputPath=$LauncherBaseOutputPath",
            "--output", $LauncherOutput
        )
        & dotnet @LauncherPublishArguments
        $LauncherPublishExitCode = $LASTEXITCODE
    } finally {
        $env:NUGET_PACKAGES = $OriginalNugetPackages
        $env:DOTNET_NOLOGO = $OriginalDotnetNoLogo
        $env:DOTNET_CLI_TELEMETRY_OPTOUT = $OriginalDotnetTelemetry
    }
    if ($LauncherPublishExitCode -ne 0) {
        throw "Could not publish the self-contained .NET launcher."
    }

    $BuiltExecutable = Join-Path $LauncherOutput "CodeVein2Randomizer-$DisplayVersion.exe"
    if (-not (Test-Path -LiteralPath $BuiltExecutable -PathType Leaf)) {
        throw "The self-contained .NET launcher executable was not created: $BuiltExecutable"
    }
    Copy-Item -LiteralPath $BuiltExecutable -Destination (Join-Path $StageRoot $ExecutableName)
    $StageExecutable = Join-Path $StageRoot $ExecutableName
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
        "The game must already be installed. Original game assets are read from",
        "the selected game installation. Generated cache and output are stored",
        "under your Windows user data folders.",
        "",
        "Some game versions and rewards may not have gameplay validation.",
        "",
        "Repo: https://github.com/mmachida/CodeVein2-Randomizer"
    )
    $ExpectedReadmeText = ($ReadmeLines -join [Environment]::NewLine) + [Environment]::NewLine
    Set-Content -LiteralPath (Join-Path $StageRoot "README.txt") -Value $ReadmeLines -Encoding UTF8

    New-Item -ItemType Directory -Force -Path $RuntimeTempRoot | Out-Null
    $OriginalTemp = $env:TEMP
    $OriginalTmp = $env:TMP
    try {
        $env:TEMP = $RuntimeTempRoot
        $env:TMP = $RuntimeTempRoot
        $SmokeArguments = @{
            FilePath = $StageExecutable
            ArgumentList = "--startup-smoke-test"
            WorkingDirectory = $ZipValidationRoot
            PassThru = $true
            WindowStyle = "Hidden"
            Wait = $true
        }
        $Smoke = Start-Process @SmokeArguments
    } finally {
        $env:TEMP = $OriginalTemp
        $env:TMP = $OriginalTmp
    }
    if ($Smoke.ExitCode -ne 0) {
        throw "Packaged .NET launcher startup/resource smoke test failed with exit code $($Smoke.ExitCode)."
    }
    $LauncherTempRoots = @(Get-ChildItem -LiteralPath $RuntimeTempRoot -Directory -Filter "CV2R-DotNetHost-*" -ErrorAction SilentlyContinue)
    if ($LauncherTempRoots.Count -gt 0) {
        throw "The .NET launcher did not clean its extracted application payload after startup smoke."
    }

    $SourceUsmapHash = (Get-FileHash -LiteralPath $MappingPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $SourceIconHash = (Get-FileHash -LiteralPath $IconPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $SourceOodleHash = (Get-FileHash -LiteralPath $OodlePath -Algorithm SHA256).Hash.ToLowerInvariant()

    Compress-Archive -Path (Join-Path $StageRoot "*") -DestinationPath $ZipPath -CompressionLevel Optimal
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        throw "Release ZIP was not created."
    }
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $ZipValidationRoot -Force
    $StagePrefix = (Resolve-Path -LiteralPath $StageRoot).Path.TrimEnd('\') + '\'
    $StagedFiles = @(Get-ChildItem -LiteralPath $StageRoot -File -Recurse | Sort-Object FullName)
    $ExpectedEntries = @($StagedFiles | ForEach-Object { $_.FullName.Substring($StagePrefix.Length).Replace('\', '/') } | Sort-Object)
    $RequiredReleaseEntries = @(@($ExecutableName, "README.txt") | Sort-Object)
    if (($ExpectedEntries -join "|") -cne ($RequiredReleaseEntries -join "|")) {
        throw "The release stage must contain only the versioned EXE and README.txt."
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $Entries = @($Archive.Entries | Where-Object { -not $_.FullName.EndsWith('/') } | ForEach-Object { $_.FullName } | Sort-Object)
        if (($Entries -join "|") -cne ($ExpectedEntries -join "|")) {
            throw "Release ZIP does not match the staged file inventory."
        }
    } finally {
        $Archive.Dispose()
    }

    foreach ($StagedFile in $StagedFiles) {
        $RelativePath = $StagedFile.FullName.Substring($StagePrefix.Length)
        $ExtractedFile = Join-Path $ZipValidationRoot $RelativePath
        if (-not (Test-Path -LiteralPath $ExtractedFile -PathType Leaf)) {
            throw "ZIP validation is missing a staged file: $RelativePath"
        }
        $StageHash = (Get-FileHash -LiteralPath $StagedFile.FullName -Algorithm SHA256).Hash
        $ExtractedHash = (Get-FileHash -LiteralPath $ExtractedFile -Algorithm SHA256).Hash
        if ($StageHash -cne $ExtractedHash) {
            throw "ZIP validation hash mismatch for: $RelativePath"
        }
    }
    $BuiltHash = (Get-FileHash -LiteralPath $BuiltExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    $ZippedReadme = Get-Content -LiteralPath (Join-Path $ZipValidationRoot "README.txt") -Raw
    if ($ZippedReadme -ne $ExpectedReadmeText) {
        throw "README.txt in the ZIP does not match the requested release text."
    }

    try {
        $env:TEMP = $RuntimeTempRoot
        $env:TMP = $RuntimeTempRoot
        $ZipSmokeArguments = @{
            FilePath = (Join-Path $ZipValidationRoot $ExecutableName)
            ArgumentList = "--startup-smoke-test"
            WorkingDirectory = $ZipValidationRoot
            PassThru = $true
            WindowStyle = "Hidden"
            Wait = $true
        }
        $ZipSmoke = Start-Process @ZipSmokeArguments
    } finally {
        $env:TEMP = $OriginalTemp
        $env:TMP = $OriginalTmp
    }
    if ($ZipSmoke.ExitCode -ne 0) {
        throw "The .NET launcher extracted from the release ZIP failed its startup/resource smoke test."
    }
    $LauncherTempRoots = @(Get-ChildItem -LiteralPath $RuntimeTempRoot -Directory -Filter "CV2R-DotNetHost-*" -ErrorAction SilentlyContinue)
    if ($LauncherTempRoots.Count -gt 0) {
        throw "The .NET launcher did not clean its extracted application payload after ZIP smoke."
    }

    $ZipHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $ZipSize = (Get-Item -LiteralPath $ZipPath).Length
    $BuiltSize = (Get-Item -LiteralPath $BuiltExecutable).Length
    @(
        "SHA256  $ZipName  $ZipHash",
        "SHA256  $ExecutableName  $BuiltHash"
    ) | Set-Content -LiteralPath $ChecksumsPath -Encoding ASCII

    $SourceRevision = (& git -C $ProjectRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not record the source revision in the build manifest."
    }
    $SourceTreeClean = @(& git -C $ProjectRoot status --porcelain --untracked-files=all).Count -eq 0
    $RuntimeVersions = (& $PythonPath -c "import importlib.metadata as m, json, sys; print(json.dumps({'python': sys.version.split()[0], 'PyInstaller': m.version('PyInstaller'), 'PySide6': m.version('PySide6'), 'platformdirs': m.version('platformdirs'), 'psutil': m.version('psutil')}))" | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not record Python dependency versions in the build manifest."
    }
    $DotnetVersion = (& dotnet --version).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not record the .NET SDK version in the build manifest."
    }
    $Manifest = [ordered]@{
        schema = "cv2-randomizer-build-manifest-v1"
        app_version = $ReleaseVersion
        built_at_utc = [DateTime]::UtcNow.ToString("o")
        source_commit = $SourceRevision
        source_tree_clean = $SourceTreeClean
        target = "Windows x64"
        packaging = "PyInstaller onedir embedded in a self-contained .NET 10 single-file launcher; UPX disabled"
        tools = [ordered]@{
            python = $RuntimeVersions.python
            pyinstaller = $RuntimeVersions.PyInstaller
            pyside6 = $RuntimeVersions.PySide6
            platformdirs = $RuntimeVersions.platformdirs
            psutil = $RuntimeVersions.psutil
            dotnet_sdk = $DotnetVersion
            retoc = "0.1.5"
            uassetapi = "1.1.0"
        }
        inputs_sha256 = [ordered]@{
            cv2_usmap = $SourceUsmapHash
            retoc = (Get-FileHash -LiteralPath $RetocPath -Algorithm SHA256).Hash.ToLowerInvariant()
            uassetapi_helper = (Get-FileHash -LiteralPath $HelperExe -Algorithm SHA256).Hash.ToLowerInvariant()
            oodle = $SourceOodleHash
            launcher_project = (Get-FileHash -LiteralPath $LauncherProject -Algorithm SHA256).Hash.ToLowerInvariant()
            launcher_source = (Get-FileHash -LiteralPath (Join-Path (Split-Path $LauncherProject -Parent) "Program.cs") -Algorithm SHA256).Hash.ToLowerInvariant()
            embedded_app_payload = (Get-FileHash -LiteralPath $PayloadArchive -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        release_files = @(
            [ordered]@{ path = $ZipName; sha256 = $ZipHash; bytes = $ZipSize },
            [ordered]@{ path = $ExecutableName; sha256 = $BuiltHash; bytes = $BuiltSize }
        )
        distribution_files = @($StagedFiles | ForEach-Object {
            $RelativePath = $_.FullName.Substring($StagePrefix.Length).Replace('\', '/')
            [ordered]@{
                path = $RelativePath
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                bytes = $_.Length
            }
        })
    }
    $Manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $BuildManifestPath -Encoding UTF8

    Write-Output "Version: $DisplayVersion"
    Write-Output "Release ZIP: $ZipPath"
    Write-Output "ZIP size: $ZipSize bytes"
    Write-Output "EXE size: $BuiltSize bytes"
    Write-Output "EXE SHA-256: $BuiltHash"
    Write-Output "ZIP SHA-256: $ZipHash"
    Write-Output "Build manifest: $BuildManifestPath"
    Write-Output "ZIP file count: $($Entries.Count)"
    Write-Output "Bundled app file count: $(@(Get-ChildItem -LiteralPath $BuiltAppDirectory -File -Recurse).Count)"
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
