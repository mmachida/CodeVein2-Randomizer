param(
    [string]$Python = ".venv\Scripts\python.exe",
    [string]$SigningCertificateThumbprint = "",
    [string]$SignToolPath = "",
    [string]$TimestampServer = "http://timestamp.digicert.com",
    [switch]$ResumeExistingBuildB
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonPath = Join-Path $ProjectRoot $Python
$CompareRoot = Join-Path $ProjectRoot "release_compare"
$BuildB = Join-Path $CompareRoot "BUILD_B_ONEDIR"
$BuildC = Join-Path $CompareRoot "BUILD_C_SIGNED"
$ZipB = Join-Path $CompareRoot "CV2Randomizer_BUILD_B_ONEDIR.zip"
$ZipC = Join-Path $CompareRoot "CV2Randomizer_BUILD_C_SIGNED.zip"
$Version = (& $PythonPath -c "import tomllib; from pathlib import Path; print(tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])").Trim()
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Could not read a valid project version from pyproject.toml."
}
if (-not (Test-Path -LiteralPath $CompareRoot -PathType Container)) {
    throw "BUILD A comparison folder is missing: $CompareRoot"
}
if ((Test-Path -LiteralPath $BuildB) -or (Test-Path -LiteralPath $ZipB)) {
    if (-not $ResumeExistingBuildB -or -not (Test-Path -LiteralPath $BuildB -PathType Container) -or -not (Test-Path -LiteralPath $ZipB -PathType Leaf)) {
        throw "BUILD B output already exists. Preserve it; use -ResumeExistingBuildB only to validate and finalize a previously created complete B folder and ZIP."
    }
}
if ($SigningCertificateThumbprint -and (Test-Path -LiteralPath $ZipC)) {
    throw "BUILD C ZIP already exists. Preserve it and choose a fresh comparison folder before rebuilding."
}
if ($SigningCertificateThumbprint -and (Test-Path -LiteralPath $BuildC)) {
    $ExistingCFiles = @(Get-ChildItem -LiteralPath $BuildC -Force)
    $Placeholder = Join-Path $BuildC "STATUS.txt"
    if ($ExistingCFiles.Count -ne 1 -or $ExistingCFiles[0].Name -ne "STATUS.txt" -or -not (Get-Content -LiteralPath $Placeholder -Raw).StartsWith("NOT BUILT")) {
        throw "BUILD C output already contains non-placeholder files. Preserve it and choose a fresh comparison folder before rebuilding."
    }
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$ScratchParent = Join-Path $ProjectRoot "workspace\antivirus-build"
$ScratchRoot = Join-Path $ScratchParent $Timestamp
$HelperOutput = Join-Path $ScratchRoot "helper"
$DistRoot = Join-Path $ScratchRoot "dist"
$WorkRoot = Join-Path $ScratchRoot "pyinstaller-work"
$SpecRoot = Join-Path $ScratchRoot "spec"
$VersionFile = Join-Path $ScratchRoot "version_info.txt"
$SmokeCwd = Join-Path $env:TEMP "CV2 Randomizer Smoke $Timestamp"
$DataPath = Join-Path $ProjectRoot "data"
$IconPath = Join-Path $ProjectRoot "ico\icon.ico"
$IconPngPath = Join-Path $ProjectRoot "ico\icon.png"
$MappingPath = Join-Path $ProjectRoot "workspace\research_assets\CV2.usmap"
$OodlePath = Join-Path $ProjectRoot "tools\oo2core_9_win64.dll"
$RetocPath = Join-Path $ProjectRoot "workspace\tools\retoc-v0.1.5\retoc.exe"
$RetocLicense = Join-Path $ProjectRoot "workspace\tools\retoc-v0.1.5\LICENSE"
$HelperProject = Join-Path $ProjectRoot "tools\uassetapi-worldloot\UAssetAPIWorldLoot.csproj"
$EntryPoint = Join-Path $ProjectRoot "src\cv2randomizer\main.py"
$UAssetLicense = Join-Path $env:USERPROFILE ".nuget\packages\uassetapi\1.1.0\LICENSE"
$NewtonsoftLicense = Join-Path $env:USERPROFILE ".nuget\packages\newtonsoft.json\13.0.3\LICENSE.md"
$BuildSucceeded = $false
$BuildBExistedAtStart = Test-Path -LiteralPath $BuildB
$ZipBExistedAtStart = Test-Path -LiteralPath $ZipB
$BuildCExistedAtStart = Test-Path -LiteralPath $BuildC
$ZipCExistedAtStart = Test-Path -LiteralPath $ZipC

foreach ($RequiredFile in @(
    $PythonPath, $IconPath, $IconPngPath, $MappingPath, $OodlePath, $RetocPath,
    $RetocLicense, $HelperProject, $UAssetLicense, $NewtonsoftLicense, $EntryPoint
)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required comparison input is missing: $RequiredFile"
    }
}
if ((Get-Item -LiteralPath $OodlePath).Length -le 0) {
    throw "The local Oodle DLL is empty."
}

# Build Windows version metadata
function New-VersionResource {
    $Parts = @($Version.Split('.') | ForEach-Object { [int]$_ })
    $NumericVersion = "{0}.{1}.{2}.0" -f $Parts[0], $Parts[1], $Parts[2]
    $TupleVersion = "{0}, {1}, {2}, 0" -f $Parts[0], $Parts[1], $Parts[2]
    $Resource = @"
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
        StringStruct('OriginalFilename', 'CodeVein2Randomizer.exe'),
        StringStruct('ProductName', 'Code Vein 2 Randomizer'),
        StringStruct('ProductVersion', '$NumericVersion'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
    Set-Content -LiteralPath $VersionFile -Value $Resource -Encoding UTF8
}

# Stage files for distribution
function New-DistributionPayload {
    param(
        [Parameter(Mandatory = $true)][string]$TargetDirectory,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$Sign
    )

    $ThisHelperOutput = Join-Path $ScratchRoot ("helper-" + $Label)
    $ThisDistRoot = Join-Path $ScratchRoot ("dist-" + $Label)
    $ThisWorkRoot = Join-Path $ScratchRoot ("work-" + $Label)
    $ThisSpecRoot = Join-Path $ScratchRoot ("spec-" + $Label)
    New-Item -ItemType Directory -Force -Path $ThisHelperOutput, $ThisDistRoot, $ThisWorkRoot, $ThisSpecRoot | Out-Null

    & dotnet clean $HelperProject --configuration Release
    if ($LASTEXITCODE -ne 0) { throw "Could not clean the UAssetAPI helper build state for $Label." }
    $DotnetArguments = @(
        "publish", $HelperProject, "--configuration", "Release", "--runtime", "win-x64",
        "--self-contained", "true", "--property:PublishSingleFile=true",
        "--property:IncludeAllContentForSelfExtract=true", "--property:IncludeNativeLibrariesForSelfExtract=true",
        "--property:EnableCompressionInSingleFile=true", "--output", $ThisHelperOutput
    )
    & dotnet @DotnetArguments
    if ($LASTEXITCODE -ne 0) { throw "Could not clean-publish the UAssetAPI helper for $Label." }
    $HelperExe = Join-Path $ThisHelperOutput "UAssetAPIWorldLoot.exe"
    if (-not (Test-Path -LiteralPath $HelperExe -PathType Leaf)) { throw "Published helper is missing for $Label." }

    $SpecArguments = @(
        "--onedir", "--windowed", "--noupx", "--name", "CodeVein2Randomizer",
        "--paths", (Join-Path $ProjectRoot "src"), "--specpath", $ThisSpecRoot,
        "--icon", $IconPath, "--version-file", $VersionFile, "--contents-directory", "_internal",
        "--add-data", "$DataPath;data", "--add-data", "$IconPath;ico", "--add-data", "$IconPngPath;ico",
        "--add-data", "$MappingPath;workspace/research_assets", "--add-binary", "$RetocPath;tools",
        "--add-binary", "$HelperExe;tools", "--add-binary", "$OodlePath;tools", $EntryPoint
    )
    $MakeSpec = Join-Path (Split-Path $PythonPath -Parent) "pyi-makespec.exe"
    & $MakeSpec @SpecArguments
    if ($LASTEXITCODE -ne 0) { throw "Could not generate the onedir spec for $Label." }
    $SpecPath = Join-Path $ThisSpecRoot "CodeVein2Randomizer.spec"
    if (-not (Test-Path -LiteralPath $SpecPath -PathType Leaf)) { throw "Generated onedir spec is missing for $Label." }

    $SpecText = Get-Content -LiteralPath $SpecPath -Raw
    $InsertionPoint = "pyz = PYZ("
    if (-not $SpecText.Contains($InsertionPoint)) { throw "PyInstaller spec layout changed; cannot apply the ICU exclusion safely." }
    $IcuFilter = @(
        "import os", "from PyInstaller.building.datastruct import TOC", "",
        "a.binaries = TOC(entry for entry in a.binaries if not os.path.basename(entry[0]).casefold().startswith('icu'))", ""
    ) -join [Environment]::NewLine
    Set-Content -LiteralPath $SpecPath -Value $SpecText.Replace($InsertionPoint, $IcuFilter + $InsertionPoint) -Encoding UTF8

    if ($SpecText -match '\bupx=True\b') { throw "The generated onedir spec enables UPX; refusing to build $Label." }
    $PyInstallerArguments = @("--noconfirm", "--clean", "--distpath", $ThisDistRoot, "--workpath", $ThisWorkRoot, $SpecPath)
    & $PythonPath -m PyInstaller @PyInstallerArguments
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller onedir build failed for $Label with exit code $LASTEXITCODE." }
    $BuiltRoot = Join-Path $ThisDistRoot "CodeVein2Randomizer"
    $BuiltExe = Join-Path $BuiltRoot "CodeVein2Randomizer.exe"
    if (-not (Test-Path -LiteralPath $BuiltExe -PathType Leaf)) { throw "Onedir executable was not created for $Label." }

    New-Item -ItemType Directory -Force -Path $TargetDirectory | Out-Null
    Get-ChildItem -LiteralPath $BuiltRoot -Force | Copy-Item -Destination $TargetDirectory -Recurse -Force
    $TargetExe = Join-Path $TargetDirectory "CodeVein2Randomizer.exe"
    if ($Sign) {
        $Cert = Get-Item -LiteralPath "Cert:\CurrentUser\My\$SigningCertificateThumbprint" -ErrorAction Stop
        if (-not $Cert.HasPrivateKey) { throw "Signing certificate does not have an accessible private key." }
        $HasCodeSigningEku = $false
        foreach ($Extension in $Cert.Extensions) {
            if ($Extension -is [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]) {
                $HasCodeSigningEku = @($Extension.EnhancedKeyUsages | Where-Object { $_.Value -eq "1.3.6.1.5.5.7.3.3" }).Count -gt 0
            }
        }
        if (-not $HasCodeSigningEku) { throw "The selected certificate does not have the code-signing EKU." }
        if ([string]::IsNullOrWhiteSpace($script:ResolvedSignTool)) { throw "signtool.exe was not found." }
        foreach ($OwnedExe in @($TargetExe, (Join-Path $TargetDirectory "_internal\tools\UAssetAPIWorldLoot.exe"))) {
            if (-not (Test-Path -LiteralPath $OwnedExe -PathType Leaf)) { throw "Project-owned binary to sign is missing: $OwnedExe" }
            & $script:ResolvedSignTool sign /sha1 $SigningCertificateThumbprint /s My /fd SHA256 /tr $TimestampServer /td SHA256 /v $OwnedExe
            if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed for $OwnedExe." }
            & $script:ResolvedSignTool verify /pa /all /v $OwnedExe
            if ($LASTEXITCODE -ne 0) { throw "Authenticode verification failed for $OwnedExe." }
            $Signature = Get-AuthenticodeSignature -LiteralPath $OwnedExe
            if ($Signature.Status -ne "Valid" -or -not $Signature.TimeStamperCertificate) {
                throw "Signature or trusted timestamp verification failed for $OwnedExe."
            }
        }
    }

    $Readme = @(
        "Code Vein 2 Randomizer v$Version", "Windows 10/11, 64-bit", "",
        "1. Extract the complete folder from this ZIP.",
        "2. Keep the _internal folder beside CodeVein2Randomizer.exe.",
        "3. Run CodeVein2Randomizer.exe.",
        "4. Select the Code Vein 2 installation root. For Steam, this is usually",
        "   ...\steamapps\common\CodeVein2, the folder containing CodeVein2.",
        "5. Use Extract Game Data if requested, choose a seed and options, then click Randomize.",
        "", "The game must already be installed. The app reads original game assets and",
        "stores generated cache and output under your Windows user data folders."
    )
    Set-Content -LiteralPath (Join-Path $TargetDirectory "README.txt") -Value $Readme -Encoding UTF8
    $NoticesPath = Join-Path (Join-Path $CompareRoot "BUILD_A_CURRENT") "THIRD-PARTY-NOTICES.txt"
    if (-not (Test-Path -LiteralPath $NoticesPath -PathType Leaf)) { throw "Build A third-party notices are missing." }
    Copy-Item -LiteralPath $NoticesPath -Destination (Join-Path $TargetDirectory "THIRD-PARTY-NOTICES.txt")

    # Run from an unrelated working directory containing spaces to detect CWD assumptions.
    New-Item -ItemType Directory -Force -Path $SmokeCwd | Out-Null
    $Smoke = Start-Process -FilePath $TargetExe -ArgumentList "--startup-smoke-test" -WorkingDirectory $SmokeCwd -PassThru -WindowStyle Hidden -Wait
    if ($Smoke.ExitCode -ne 0) { throw "Packaged startup/resource smoke test failed for $Label (exit $($Smoke.ExitCode))." }
    return $TargetExe
}

New-Item -ItemType Directory -Force -Path $ScratchRoot | Out-Null
try {
    New-VersionResource
    $PyInstallerVersion = (& $PythonPath -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller is not installed in $PythonPath." }
    $Upx = Get-Command "upx.exe" -ErrorAction SilentlyContinue

    $ResolvedSignTool = $SignToolPath
    if (-not $ResolvedSignTool) {
        $SignToolCommand = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
        if ($SignToolCommand) { $ResolvedSignTool = $SignToolCommand.Source }
    }
    $script:ResolvedSignTool = $ResolvedSignTool
    if ($SigningCertificateThumbprint) {
        $NormalizedThumbprint = ($SigningCertificateThumbprint -replace '\s', '').ToUpperInvariant()
        $SigningCertificateThumbprint = $NormalizedThumbprint
        $CandidateCertificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$SigningCertificateThumbprint" -ErrorAction Stop
        if (-not $CandidateCertificate.HasPrivateKey) { throw "Signing certificate has no private key." }
        if (-not $ResolvedSignTool -or -not (Test-Path -LiteralPath $ResolvedSignTool -PathType Leaf)) { throw "Signing was requested but signtool.exe is unavailable." }
    }

    $BuildBExe = Join-Path $BuildB "CodeVein2Randomizer.exe"
    if ($ResumeExistingBuildB) {
        if (-not (Test-Path -LiteralPath $BuildBExe -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $BuildB "_internal\tools\retoc.exe") -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $BuildB "_internal\tools\UAssetAPIWorldLoot.exe") -PathType Leaf)) {
            throw "Existing BUILD B payload is incomplete; it cannot be resumed safely."
        }
        $ExpectedPaths = @(Get-ChildItem -LiteralPath $BuildB -File -Recurse | ForEach-Object { $_.FullName.Substring($BuildB.Length).TrimStart('\').Replace('\', '/') } | Sort-Object)
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $ExistingArchive = [System.IO.Compression.ZipFile]::OpenRead($ZipB)
        try {
            $ArchiveEntries = @($ExistingArchive.Entries | Where-Object { -not $_.FullName.EndsWith('/') })
            $ArchivePaths = @($ArchiveEntries | ForEach-Object { $_.FullName } | Sort-Object)
            if (($ExpectedPaths -join "`n") -ne ($ArchivePaths -join "`n")) { throw "Existing BUILD B ZIP does not match the onedir folder file list." }
            foreach ($Entry in $ArchiveEntries) {
                $ExpectedFile = Join-Path $BuildB ($Entry.FullName.Replace('/', '\'))
                $SourceHash = (Get-FileHash -LiteralPath $ExpectedFile -Algorithm SHA256).Hash
                $EntryStream = $Entry.Open()
                try {
                    $EntryHash = [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($EntryStream))
                } finally { $EntryStream.Dispose() }
                if ($SourceHash -ne $EntryHash) { throw "Existing BUILD B ZIP contents differ from the onedir folder: $($Entry.FullName)" }
            }
        } finally { $ExistingArchive.Dispose() }
        New-Item -ItemType Directory -Force -Path $SmokeCwd | Out-Null
        $ResumeSmoke = Start-Process -FilePath $BuildBExe -ArgumentList "--startup-smoke-test" -WorkingDirectory $SmokeCwd -PassThru -WindowStyle Hidden -Wait
        if ($ResumeSmoke.ExitCode -ne 0) { throw "Existing BUILD B startup/resource smoke test failed (exit $($ResumeSmoke.ExitCode))." }
    } else {
        New-DistributionPayload -TargetDirectory $BuildB -Label "B" | Out-Null
        Compress-Archive -Path (Join-Path $BuildB "*") -DestinationPath $ZipB -CompressionLevel Optimal
        if (-not (Test-Path -LiteralPath $ZipB -PathType Leaf)) { throw "BUILD B ZIP was not created." }
    }

    if ($SigningCertificateThumbprint) {
        New-DistributionPayload -TargetDirectory $BuildC -Label "C" -Sign | Out-Null
        $CertSubject = (Get-Item -LiteralPath "Cert:\CurrentUser\My\$SigningCertificateThumbprint").Subject
        Set-Content -LiteralPath (Join-Path $BuildC "STATUS.txt") -Value @(
            "Authenticode: valid", "Certificate subject: $CertSubject", "Timestamp: valid (RFC 3161)",
            "Signed files: CodeVein2Randomizer.exe and project-owned UAssetAPIWorldLoot.exe"
        ) -Encoding UTF8
        Compress-Archive -Path (Join-Path $BuildC "*") -DestinationPath $ZipC -CompressionLevel Optimal
        if (-not (Test-Path -LiteralPath $ZipC -PathType Leaf)) { throw "BUILD C ZIP was not created." }
    } else {
        New-Item -ItemType Directory -Force -Path $BuildC | Out-Null
        Set-Content -LiteralPath (Join-Path $BuildC "STATUS.txt") -Value @(
            "NOT BUILT — signing certificate unavailable.",
            "No unsigned payload is labeled as signed. BUILD B is the unsigned onedir comparison payload.",
            "To build C later, rerun scripts/build_antivirus_comparison.ps1 with -ResumeExistingBuildB, -SigningCertificateThumbprint, and signtool.exe available.",
            "The script performs a fresh BUILD-B-equivalent onedir build, signs only the app executable and project-owned UAssetAPI helper with SHA-256 and a trusted timestamp, then verifies both signatures before zipping."
        ) -Encoding UTF8
    }

    $BuildBFiles = @(Get-ChildItem -LiteralPath $BuildB -File -Recurse)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $BuildBArchive = [System.IO.Compression.ZipFile]::OpenRead($ZipB)
    try { $BuildBZipEntryCount = $BuildBArchive.Entries.Count } finally { $BuildBArchive.Dispose() }
    $BuildBExeHash = (Get-FileHash -LiteralPath $BuildBExe -Algorithm SHA256).Hash.ToLowerInvariant()
    $BuildBZipHash = (Get-FileHash -LiteralPath $ZipB -Algorithm SHA256).Hash.ToLowerInvariant()
    $VersionInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($BuildBExe)
    $BuildBFileCount = $BuildBFiles.Count
    $BuildBSummary = @(
        "BUILD B: PyInstaller $PyInstallerVersion onedir, --clean, --noupx",
        "UPX executable available: $([bool]$Upx)",
        "EXE: $BuildBExe ($((Get-Item -LiteralPath $BuildBExe).Length) bytes; SHA-256 $BuildBExeHash)",
        "ZIP: $ZipB ($((Get-Item -LiteralPath $ZipB).Length) bytes; SHA-256 $BuildBZipHash)",
        "Files in distribution folder: $BuildBFileCount; files in ZIP: $BuildBZipEntryCount",
        "PE FileDescription: $($VersionInfo.FileDescription); ProductName: $($VersionInfo.ProductName); FileVersion: $($VersionInfo.FileVersion); ProductVersion: $($VersionInfo.ProductVersion); InternalName: $($VersionInfo.InternalName); OriginalFilename: $($VersionInfo.OriginalFilename)",
        "Smoke test from unrelated CWD with spaces: PASS"
    )
    $BuildBSummary | Write-Output
    $BuildSucceeded = $true
} finally {
    if (Test-Path -LiteralPath $SmokeCwd -PathType Container) {
        $ResolvedSmoke = (Resolve-Path -LiteralPath $SmokeCwd).Path
        if ($ResolvedSmoke.StartsWith((Resolve-Path $env:TEMP).Path.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase) -and (Split-Path -Leaf $ResolvedSmoke).StartsWith("CV2 Randomizer Smoke ")) {
            Remove-Item -LiteralPath $ResolvedSmoke -Recurse -Force
        }
    }
    if ($BuildSucceeded -and (Test-Path -LiteralPath $ScratchRoot -PathType Container)) {
        $ResolvedScratch = (Resolve-Path -LiteralPath $ScratchRoot).Path
        $ResolvedParent = (Resolve-Path -LiteralPath $ScratchParent).Path.TrimEnd('\') + '\'
        if ($ResolvedScratch.StartsWith($ResolvedParent, [System.StringComparison]::OrdinalIgnoreCase) -and (Split-Path -Leaf $ResolvedScratch) -eq $Timestamp) {
            Remove-Item -LiteralPath $ResolvedScratch -Recurse -Force
        } else {
            Write-Warning "Kept scratch because its cleanup guard rejected the resolved path: $ResolvedScratch"
        }
    } elseif (Test-Path -LiteralPath $ScratchRoot -PathType Container) {
        Write-Warning "Build did not complete; kept diagnostic scratch at $ScratchRoot"
    }
    if (-not $BuildSucceeded) {
        $ComparePrefix = (Resolve-Path -LiteralPath $CompareRoot).Path.TrimEnd('\') + '\'
        $PartialOutputs = @(
            @{ Path = $BuildB; ExistedAtStart = $BuildBExistedAtStart },
            @{ Path = $ZipB; ExistedAtStart = $ZipBExistedAtStart },
            @{ Path = $BuildC; ExistedAtStart = $BuildCExistedAtStart },
            @{ Path = $ZipC; ExistedAtStart = $ZipCExistedAtStart }
        )
        foreach ($Partial in $PartialOutputs) {
            if ($Partial.ExistedAtStart) { continue }
            $PartialOutput = $Partial.Path
            if (-not (Test-Path -LiteralPath $PartialOutput)) { continue }
            $ResolvedOutput = (Resolve-Path -LiteralPath $PartialOutput).Path
            if (-not $ResolvedOutput.StartsWith($ComparePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                Write-Warning "Kept partial output outside the guarded comparison folder: $ResolvedOutput"
                continue
            }
            Remove-Item -LiteralPath $ResolvedOutput -Recurse -Force
        }
    }
}
