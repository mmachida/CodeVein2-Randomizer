using System.Buffers.Binary;
using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class HealingUpgradePatcher
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };


    // Run

    public static int Run(string[] args)
    {
        var committed = new List<string>();
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappingsPath = Path.GetFullPath(args[2]);
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            if (!Directory.Exists(workRoot)) throw new InvalidDataException("Healing patch work root does not exist.");
            if (!File.Exists(mappingsPath)) throw new FileNotFoundException("CV2.usmap mapping was not found.", mappingsPath);
            var document = JsonSerializer.Deserialize<HealingBatchDocument>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Healing upgrade patch manifest is empty.");
            if (document.Packages.Count == 0)
                throw new InvalidDataException("Healing upgrade patch manifest contains no map packages.");
            var helperStarted = Stopwatch.StartNew();
            var mappings = new Usmap(mappingsPath);
            helperStarted.Stop();
            var results = new List<HealingPackageResult>();
            foreach (var request in document.Packages)
            {
                results.Add(PatchPackage(workRoot, request, engineVersion, mappings, committed));
                GC.Collect(GC.MaxGeneration, GCCollectionMode.Forced, blocking: true, compacting: true);
                GC.WaitForPendingFinalizers();
            }
            Console.WriteLine(JsonSerializer.Serialize(new HealingBatchResult(
                results,
                helperStarted.Elapsed.TotalMilliseconds,
                results.Sum(result => result.Timing.TotalMilliseconds)), JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            foreach (var path in committed)
                try { if (File.Exists(path)) File.Delete(path); } catch (IOException) { }
            Console.Error.WriteLine(exception.ToString());
            return 1;
        }
    }


    // Test FName encoding fixtures

    public static int TestFNameEncodingFixtures()
    {
        try
        {
            var identity = new SoftObjectPathIdentityDto(
                new FNameIdentityDto("/Game/Source", 2),
                new FNameIdentityDto("DA_Source", 3));
            var indices = (Package: new[] { 0 }, Asset: new[] { 6 });
            var assigned = new SoftObjectPathIdentityDto(
                new FNameIdentityDto("/Game/Assigned", 4),
                new FNameIdentityDto("DA_Assigned", 5));

            var wide = new byte[40];
            WriteInt32(wide, 8, 0);
            WriteInt32(wide, 12, 2);
            WriteInt32(wide, 16, 6);
            WriteInt32(wide, 20, 3);
            var widePath = FindUniqueFNamePath(wide, indices, identity);
            if (widePath != new SerializedFNamePath(8))
                throw new InvalidDataException("32-bit FName fixture resolved to the wrong offset or encoding.");
            var widePatched = wide.ToArray();
            WriteFNamePath(widePatched, widePath, 7, 4, 8, 5);
            EnsureOnlyRangeChanged(wide, widePatched, widePath.Offset, widePath.ByteLength,
                "32-bit FName fixture changed bytes outside the ArticleDataPath range.");
            if (ReadInt32(widePatched, 8) != 7 || ReadInt32(widePatched, 12) != 4 ||
                ReadInt32(widePatched, 16) != 8 || ReadInt32(widePatched, 20) != 5)
                throw new InvalidDataException("32-bit FName fixture did not preserve the assigned identity.");

            var unaligned = new byte[32];
            WriteInt32(unaligned, 6, 0);
            WriteInt32(unaligned, 10, 2);
            WriteInt32(unaligned, 14, 6);
            WriteInt32(unaligned, 18, 3);
            var unalignedPath = FindUniqueFNamePath(unaligned, indices, identity);
            if (unalignedPath != new SerializedFNamePath(6))
                throw new InvalidDataException("2-byte-aligned FName fixture resolved to the wrong offset.");
            var unalignedPatched = unaligned.ToArray();
            WriteFNamePath(unalignedPatched, unalignedPath, 7, 4, 8, 5);
            EnsureOnlyRangeChanged(unaligned, unalignedPatched, unalignedPath.Offset, unalignedPath.ByteLength,
                "2-byte-aligned FName fixture changed bytes outside the ArticleDataPath range.");
            if (ReadInt32(unalignedPatched, 6) != 7 || ReadInt32(unalignedPatched, 10) != 4 ||
                ReadInt32(unalignedPatched, 14) != 8 || ReadInt32(unalignedPatched, 18) != 5)
                throw new InvalidDataException("2-byte-aligned FName fixture did not preserve the assigned identity.");

            var ambiguous = new byte[64];
            WriteInt32(ambiguous, 8, 0);
            WriteInt32(ambiguous, 12, 2);
            WriteInt32(ambiguous, 16, 6);
            WriteInt32(ambiguous, 20, 3);
            WriteInt32(ambiguous, 30, 0);
            WriteInt32(ambiguous, 34, 2);
            WriteInt32(ambiguous, 38, 6);
            WriteInt32(ambiguous, 42, 3);
            try
            {
                _ = FindUniqueFNamePath(ambiguous, indices, identity);
                throw new InvalidDataException("Ambiguous FName fixture was accepted.");
            }
            catch (InvalidDataException exception) when (exception.Message.Contains("found 2", StringComparison.Ordinal))
            {
                // Both encodings matched; refusing to guess is the expected result.
            }

            Console.WriteLine("Healing ArticleDataPath FName fixtures passed: 4-byte alignment, 2-byte alignment, and ambiguous-layout rejection.");
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(exception.ToString());
            return 1;
        }
    }


    // Test export type fixtures

    public static int TestExportTypeNormalizationFixtures()
    {
        try
        {
            if (!IsCompatibleExportType("UAssetAPI.ExportTypes.RawExport", "UAssetAPI.ExportTypes.NormalExport"))
                throw new InvalidDataException("RawExport to NormalExport normalization was not accepted.");
            if (!IsCompatibleExportType("UAssetAPI.ExportTypes.NormalExport", "UAssetAPI.ExportTypes.NormalExport"))
                throw new InvalidDataException("An unchanged NormalExport type was rejected.");
            if (IsCompatibleExportType("UAssetAPI.ExportTypes.NormalExport", "UAssetAPI.ExportTypes.RawExport") ||
                IsCompatibleExportType("UAssetAPI.ExportTypes.RawExport", "UAssetAPI.ExportTypes.SomeOtherExport"))
                throw new InvalidDataException("An unsupported export type transition was accepted.");

            var source = new ExportRow(298, 512, "UntouchedExport", 4, 0, 0, -1,
                "UAssetAPI.ExportTypes.NormalExport");
            var normalized = source with { SerialSize = 296, SerialOffset = 510 };
            if (SerializedExportRowDifferences(source, normalized, source.ClassIndex, source.TemplateIndex, 0).Count != 0)
                throw new InvalidDataException("A temporary SerialSize normalization was not recoverable from the original export table.");
            if (SerializedExportRowDifferences(source, normalized with { OuterIndex = -2 },
                    source.ClassIndex, source.TemplateIndex, 0).Count == 0)
                throw new InvalidDataException("An unrelated export identity change was accepted with a SerialSize normalization.");

            Console.WriteLine("Healing map export fixtures passed: supported export types, restored SerialSize normalization, and unrelated-identity rejection.");
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(exception.ToString());
            return 1;
        }
    }


    // Patch one asset package

    private static HealingPackageResult PatchPackage(
        string workRoot,
        HealingPackageRequest request,
        EngineVersion engineVersion,
        Usmap mappings,
        List<string> committed)
    {
        var packageTimer = Stopwatch.StartNew();
        var inputMapInfo = OwnedPath(workRoot, request.InputFile, mustExist: true);
        var outputMapInfo = OwnedPath(workRoot, request.OutputFile, mustExist: false);
        var inputMap = inputMapInfo.FullName;
        var outputMap = outputMapInfo.FullName;
        if (inputMapInfo.FullName == outputMapInfo.FullName || inputMapInfo.DirectoryName != outputMapInfo.DirectoryName ||
            inputMapInfo.Extension != ".umap" || outputMapInfo.Extension != ".umap" ||
            PackageFiles(outputMap).Any(File.Exists) || request.Patches.Count == 0)
            throw new InvalidDataException("Healing map input/output must be new sibling .umap packages with at least one assignment.");

        var loadTimer = Stopwatch.StartNew();
        var asset = new UAsset(inputMap, engineVersion, mappings);
        var sourceExportHeaderLength = new FileInfo(inputMap).Length;
        var originalExportRows = SnapshotExports(asset);
        var originalNames = SnapshotNames(asset);
        var originalImports = SnapshotImports(asset);
        var classInspections = InspectMapClasses(asset, request.ClassExports ?? new List<MapClassExportRequest>());
        var worldLootFields = DescribeWorldLootFields(
            inputMap, asset, request.WorldLootFields ?? new List<MapRewardFieldRequest>(),
            sourceExportHeaderLength, originalNames);
        var requestedMapNames = request.MapNameMapValues ?? new List<string>();
        if (requestedMapNames.Count > 10000 || requestedMapNames.Any(String.IsNullOrWhiteSpace) ||
            requestedMapNames.Distinct(StringComparer.Ordinal).Count() != requestedMapNames.Count)
            throw new InvalidDataException("Healing map NameMap values must be unique, non-empty, and within the 10000-name limit.");
        var expectedMapNamesAdded = requestedMapNames
            .Where(name => !originalNames.Contains(name, StringComparer.Ordinal))
            .Order(StringComparer.Ordinal)
            .ToArray();
        foreach (var name in expectedMapNamesAdded)
            _ = new FName(asset, name, 0);
        var namesBeforeImports = SnapshotNames(asset);
        if (namesBeforeImports.Length < originalNames.Length ||
            !originalNames.SequenceEqual(namesBeforeImports.Take(originalNames.Length)) ||
            !expectedMapNamesAdded.SequenceEqual(namesBeforeImports.Skip(originalNames.Length), StringComparer.Ordinal))
            throw new InvalidDataException("Healing map preparation did not append exactly the sorted requested NameMap values.");
        loadTimer.Stop();
        var validationTimer = Stopwatch.StartNew();
        var patchesByExport = new Dictionary<int, ValidatedHealingPatch>();
        var clonePackagePaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var cloneOutputPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var patch in request.Patches)
        {
            ValidateCanonicalPackagePath(patch.ClonePackagePath, "clone Blueprint");
            ValidateReward(patch.Expected);
            ValidateReward(patch.Assigned);
            if (patch.Expected.Count != patch.Assigned.Count || patch.Expected.Notification != patch.Assigned.Notification)
                throw new InvalidDataException("Healing upgrade clone must preserve Count and bNotification.");
            if (patch.ExportIndex < 1 || patch.ExportIndex > asset.Exports.Count ||
                patchesByExport.ContainsKey(patch.ExportIndex - 1))
                throw new InvalidDataException("Healing actor export indices must be unique and present in the destination map.");
            var exportIndex = patch.ExportIndex - 1;
            var actor = asset.Exports[exportIndex];
            if (actor.ObjectName.ToString() != patch.ExportName)
                throw new InvalidDataException($"Healing actor export #{patch.ExportIndex} no longer matches its catalog identity.");
            if (patch.ExpectedClassName != patch.ExpectedClassPackage[(patch.ExpectedClassPackage.LastIndexOf('/') + 1)..] + "_C")
                throw new InvalidDataException("Healing actor class name does not match its source Blueprint package.");
            var originalClass = ResolveBlueprintClassReference(asset, actor.ClassIndex);
            if (originalClass.PackagePath != patch.ExpectedClassPackage || originalClass.ClassObjectName != patch.ExpectedClassName)
                throw new InvalidDataException("Healing actor ClassIndex no longer resolves to its catalogued Blueprint class.");
            var originalTemplate = ResolveBlueprintDefaultObjectReference(asset, actor.TemplateIndex);
            var expectedCdo = "Default__" + patch.ExpectedClassName;
            if (originalTemplate.PackagePath != patch.ExpectedClassPackage || originalTemplate.ObjectName != expectedCdo ||
                patch.CdoExportName != expectedCdo)
                throw new InvalidDataException("Healing actor TemplateIndex no longer resolves to its source Blueprint CDO.");
            var cdoInputInfo = OwnedPath(workRoot, patch.BlueprintInputFile, mustExist: true);
            var cdoOutputInfo = OwnedPath(workRoot, patch.BlueprintOutputFile, mustExist: false);
            var cdoInput = cdoInputInfo.FullName;
            var cdoOutput = cdoOutputInfo.FullName;
            if (cdoInputInfo.Extension != ".uasset" || cdoOutputInfo.Extension != ".uasset" || cdoInput == cdoOutput ||
                PackageFiles(cdoOutput).Any(File.Exists) || !clonePackagePaths.Add(patch.ClonePackagePath) ||
                !cloneOutputPaths.Add(cdoOutput))
                throw new InvalidDataException("Healing Blueprint clone paths must be unique new .uasset packages.");
            patchesByExport.Add(exportIndex, new ValidatedHealingPatch(
                patch, cdoInput, cdoOutput, actor.ClassIndex, actor.TemplateIndex,
                originalTemplate.ObjectName, "Default__" + patch.ExpectedClassName));
        }
        validationTimer.Stop();

        var cloneResults = new List<HealingCloneResult>();
        var cloneMaterializationTimer = Stopwatch.StartNew();
        try
        {
            foreach (var patch in patchesByExport.Values.OrderBy(item => item.Request.ExportIndex))
            {
                var clone = PatchBlueprintCdo(
                    workRoot, patch, engineVersion, mappings, committed);
                cloneResults.Add(clone);
            }
            cloneMaterializationTimer.Stop();

            var mapMutationTimer = Stopwatch.StartNew();
            var newImportIndices = new Dictionary<int, (FPackageIndex ClassIndex, FPackageIndex TemplateIndex)>();
            foreach (var patch in patchesByExport.Values.OrderBy(item => item.Request.ExportIndex))
            {
                var packageImport = asset.Imports.Count;
                asset.Imports.Add(new Import(
                    new FName(asset, "/Script/CoreUObject"),
                    new FName(asset, "Package"),
                    new FPackageIndex(0),
                    new FName(asset, patch.Request.ClonePackagePath),
                    false));
                var packageIndex = FPackageIndex.FromImport(packageImport);
                var classImport = asset.Imports.Count;
                asset.Imports.Add(new Import(
                    new FName(asset, "/Script/Engine"),
                    new FName(asset, "BlueprintGeneratedClass"),
                    packageIndex,
                    new FName(asset, patch.Request.ExpectedClassName),
                    false));
                var cdoImport = asset.Imports.Count;
                asset.Imports.Add(new Import(
                    new FName(asset, patch.Request.ClonePackagePath),
                    new FName(asset, patch.Request.ExpectedClassName),
                    packageIndex,
                    new FName(asset, patch.AssignedTemplateObject),
                    false));
                var classIndex = FPackageIndex.FromImport(classImport);
                var templateIndex = FPackageIndex.FromImport(cdoImport);
                asset.Exports[patch.Request.ExportIndex - 1].ClassIndex = classIndex;
                asset.Exports[patch.Request.ExportIndex - 1].TemplateIndex = templateIndex;
                if (ResolveBlueprintClassReference(asset, classIndex) !=
                    (patch.Request.ExpectedClassName, patch.Request.ClonePackagePath) ||
                    ResolveBlueprintDefaultObjectReference(asset, templateIndex) !=
                    (patch.AssignedTemplateObject, patch.Request.ClonePackagePath))
                    throw new InvalidDataException("New healing class/CDO imports do not resolve to the isolated clone package.");
                newImportIndices.Add(patch.Request.ExportIndex - 1, (classIndex, templateIndex));
            }
            mapMutationTimer.Stop();

            var temporaryMap = TemporarySibling(outputMap);
            try
            {
                var mapSerializationTimer = Stopwatch.StartNew();
                asset.Write(temporaryMap);
                var serializedReopenTimer = Stopwatch.StartNew();
                var serialized = new UAsset(temporaryMap, engineVersion, mappings);
                serializedReopenTimer.Stop();
                var mapSidecar = PreserveMapPayloadAndPatchHeader(
                    inputMap, temporaryMap, serialized, originalExportRows,
                    newImportIndices, sourceExportHeaderLength, originalNames, expectedMapNamesAdded);
                File.WriteAllBytes(Path.ChangeExtension(temporaryMap, ".uexp"), mapSidecar);
                CopyOpaqueSidecars(inputMap, temporaryMap);
                mapSerializationTimer.Stop();

                var mapValidationTimer = Stopwatch.StartNew();
                var reopened = new UAsset(temporaryMap, engineVersion, mappings);
                ValidateMapOutput(
                    inputMap, temporaryMap, reopened, originalExportRows,
                    originalNames, expectedMapNamesAdded, requestedMapNames,
                    originalImports, patchesByExport, newImportIndices);
                var reopenedNames = SnapshotNames(reopened);
                MovePackage(temporaryMap, outputMap, committed);
                mapValidationTimer.Stop();

                packageTimer.Stop();
                var timing = new HealingPackageTiming(
                    loadTimer.Elapsed.TotalMilliseconds,
                    validationTimer.Elapsed.TotalMilliseconds,
                    cloneMaterializationTimer.Elapsed.TotalMilliseconds,
                    mapMutationTimer.Elapsed.TotalMilliseconds,
                    mapSerializationTimer.Elapsed.TotalMilliseconds,
                    mapValidationTimer.Elapsed.TotalMilliseconds,
                    packageTimer.Elapsed.TotalMilliseconds,
                    0,
                    serializedReopenTimer.Elapsed.TotalMilliseconds);
                var known = timing.LoadMilliseconds + timing.DiscoveryValidationMilliseconds +
                    timing.CloneMaterializationMilliseconds + timing.MapMutationMilliseconds +
                    timing.MapSerializationMilliseconds + timing.MapReopenValidationMilliseconds;
                timing = timing with { OtherMilliseconds = Math.Max(0, timing.TotalMilliseconds - known) };
                var mapNameIndices = new Dictionary<string, int>(StringComparer.Ordinal);
                foreach (var name in reopenedNames.Select((value, index) => (value, index)))
                    if (!mapNameIndices.ContainsKey(name.value)) mapNameIndices.Add(name.value, name.index);
                var mapFileHashes = PackageFiles(outputMap)
                    .Where(File.Exists)
                    .ToDictionary(path => Path.GetExtension(path).ToLowerInvariant(), HashFile, StringComparer.Ordinal);
                return new HealingPackageResult(
                    inputMap, outputMap, request.Patches.Count, cloneResults, true, true, timing,
                    originalNames.Length, reopenedNames.Length, reopenedNames.Skip(originalNames.Length).ToArray(),
                    new FileInfo(outputMap).Length, new FileInfo(Path.ChangeExtension(outputMap, ".uexp")).Length,
                    mapFileHashes, mapNameIndices, classInspections, worldLootFields,
                    1, 0, 1, 1);
            }
            finally
            {
                DeletePackage(temporaryMap);
            }
        }
        catch
        {
            throw;
        }

        throw new InvalidOperationException("Healing package completed without returning its validated timing result.");
    }


    // Patch the Blueprint CDO

    private static HealingCloneResult PatchBlueprintCdo(
        string workRoot,
        ValidatedHealingPatch patch,
        EngineVersion engineVersion,
        Usmap mappings,
        List<string> committed)
    {
        var cloneTimer = Stopwatch.StartNew();
        var loadTimer = Stopwatch.StartNew();
        var request = patch.Request;
        var input = patch.BlueprintInput;
        var output = patch.BlueprintOutput;
        var blueprint = new UAsset(input, engineVersion, mappings);
        var sourceRows = SnapshotExports(blueprint);
        var sourceNames = SnapshotNames(blueprint);
        var sourceImports = SnapshotImports(blueprint);
        var cdoIndex = request.CdoExportIndex - 1;
        if (cdoIndex < 0 || cdoIndex >= blueprint.Exports.Count)
            throw new InvalidDataException($"Healing CDO export index {request.CdoExportIndex} is outside the source Blueprint export table ({blueprint.Exports.Count} exports).");
        var cdoExport = blueprint.Exports[cdoIndex];
        loadTimer.Stop();
        var validationTimer = Stopwatch.StartNew();
        if (cdoExport is not (RawExport or NormalExport))
            throw new InvalidDataException($"Healing CDO '{request.CdoExportName}' parsed as unsupported {cdoExport.GetType().Name}.");
        if (cdoExport.ObjectName.ToString() != request.CdoExportName)
            throw new InvalidDataException($"Healing CDO export #{request.CdoExportIndex} is '{cdoExport.ObjectName}', expected '{request.CdoExportName}'.");
        var expectedIdentity = request.Expected.SoftObjectPathIdentity
            ?? throw new InvalidDataException("Healing CDO source reward has no exact ArticleDataPath FName identity.");
        var assignedIdentity = request.Assigned.SoftObjectPathIdentity
            ?? throw new InvalidDataException("Healing assignment has no exact ArticleDataPath FName identity.");
        var sourcePackageName = blueprint.FolderName.ToString();
        if (String.IsNullOrWhiteSpace(sourcePackageName) || !sourcePackageName.StartsWith("/Game/", StringComparison.Ordinal))
            throw new InvalidDataException($"Healing source Blueprint '{input}' has an invalid UE5 package name '{sourcePackageName}'.");
        // Retoc derives the Zen package ID from the legacy summary's package name.
        // A physical clone under a new path still collides with every other clone
        // unless this serialized identity matches the import path in the map.
        blueprint.FolderName = new FString(request.ClonePackagePath);
        var sourceHeaderLength = new FileInfo(input).Length;
        var sourceSidecarPath = Path.ChangeExtension(input, ".uexp");
        if (!File.Exists(sourceSidecarPath)) throw new FileNotFoundException("Healing Blueprint CDO is missing its .uexp sidecar.", sourceSidecarPath);
        var sourceSidecar = File.ReadAllBytes(sourceSidecarPath);
        var sourceCdoRow = sourceRows[cdoIndex];
        var cdoStart = checked((int)(sourceCdoRow.SerialOffset - sourceHeaderLength));
        var cdoSize = checked((int)sourceCdoRow.SerialSize);
        if (cdoStart < 0 || cdoSize <= 0 || cdoStart + cdoSize > sourceSidecar.Length)
            throw new InvalidDataException("Healing CDO ExportMap serial range is outside the source .uexp sidecar.");
        var sourceCdoData = sourceSidecar.AsSpan(cdoStart, cdoSize).ToArray();
        if (cdoExport is RawExport rawCdo &&
            (rawCdo.Data.Length != sourceCdoData.Length || !rawCdo.Data.AsSpan().SequenceEqual(sourceCdoData)))
            throw new InvalidDataException("Healing CDO RawExport bytes do not match the source .uexp range.");
        if (cdoExport is NormalExport normalCdo)
            ValidateCdoArticleDataPath(normalCdo, request.Expected, request.CdoExportName);
        var oldMapIndices = FindNameIndices(sourceNames, expectedIdentity);
        var serializedPath = FindUniqueFNamePath(sourceCdoData, oldMapIndices, expectedIdentity);
        validationTimer.Stop();

        var patchTimer = Stopwatch.StartNew();
        var nameMapValues = request.NameMapValues ?? new List<string>();
        if (nameMapValues.Count > 10000 || nameMapValues.Any(String.IsNullOrWhiteSpace) ||
            nameMapValues.Distinct(StringComparer.Ordinal).Count() != nameMapValues.Count)
            throw new InvalidDataException("Healing clone NameMap preparation values must be unique, non-empty, and within the 10000-name limit.");
        var requestedNameValues = nameMapValues
            .Concat(new[] { assignedIdentity.PackageName.Value, assignedIdentity.AssetName.Value })
            .Distinct(StringComparer.Ordinal)
            .ToArray();
        foreach (var name in requestedNameValues)
            _ = new FName(blueprint, name, 0);
        var outputNamesBeforeWrite = SnapshotNames(blueprint);
        if (outputNamesBeforeWrite.Length < sourceNames.Length ||
            !sourceNames.SequenceEqual(outputNamesBeforeWrite.Take(sourceNames.Length)))
            throw new InvalidDataException("Healing clone preparation changed source NameMap entries or indices before serialization.");
        var expectedNamesAdded = requestedNameValues
            .Where(name => !sourceNames.Contains(name, StringComparer.Ordinal))
            .ToArray();
        if (!expectedNamesAdded.SequenceEqual(outputNamesBeforeWrite.Skip(sourceNames.Length), StringComparer.Ordinal))
            throw new InvalidDataException("Healing clone preparation did not append exactly the requested NameMap values in order.");
        var packageNameIndex = FindNameIndex(outputNamesBeforeWrite, assignedIdentity.PackageName.Value);
        var assetNameIndex = FindNameIndex(outputNamesBeforeWrite, assignedIdentity.AssetName.Value);
        var patchedCdoData = sourceCdoData.ToArray();
        WriteFNamePath(patchedCdoData, serializedPath, packageNameIndex, assignedIdentity.PackageName.Number,
            assetNameIndex, assignedIdentity.AssetName.Number);
        if (cdoExport is RawExport mutableRawCdo) mutableRawCdo.Data = patchedCdoData;
        patchTimer.Stop();

        var temporary = TemporarySibling(output);
        var serializationTimer = Stopwatch.StartNew();
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(output)!);
            blueprint.Write(temporary);
            var serialized = new UAsset(temporary, engineVersion, mappings);
            if (!String.Equals(serialized.FolderName.ToString(), request.ClonePackagePath, StringComparison.Ordinal))
                throw new InvalidDataException("Healing Blueprint clone package name was not serialized to its unique /Game path.");
            var headerBytes = File.ReadAllBytes(temporary);
            var headerDelta = headerBytes.LongLength - sourceHeaderLength;
            PatchExportRows(temporary, serialized, sourceRows, headerDelta, null);
            var cdoSidecar = sourceSidecar.ToArray();
            patchedCdoData.AsSpan(serializedPath.Offset, serializedPath.ByteLength)
                .CopyTo(cdoSidecar.AsSpan(cdoStart + serializedPath.Offset, serializedPath.ByteLength));
            EnsureOnlyRangeChanged(sourceSidecar, cdoSidecar, cdoStart + serializedPath.Offset, serializedPath.ByteLength,
                "Healing CDO sidecar changed outside ArticleDataPath.");
            File.WriteAllBytes(Path.ChangeExtension(temporary, ".uexp"), cdoSidecar);
            CopyOpaqueSidecars(input, temporary);
            serializationTimer.Stop();

            var reopenValidationTimer = Stopwatch.StartNew();
            var reopened = new UAsset(temporary, engineVersion, mappings);
            if (reopened.Exports.Count != sourceRows.Length)
                throw new InvalidDataException("Reopened isolated Healing Blueprint changed its export count.");
            if (!String.Equals(reopened.FolderName.ToString(), request.ClonePackagePath, StringComparison.Ordinal))
                throw new InvalidDataException("Reopened Healing Blueprint clone package identity does not match its unique import path.");
            var reopenedCdoRow = reopened.Exports[cdoIndex];
            if (reopenedCdoRow.ObjectName.ToString() != request.CdoExportName ||
                reopenedCdoRow.SerialSize != sourceCdoRow.SerialSize)
                throw new InvalidDataException("Reopened isolated Healing Blueprint changed the target CDO export identity or payload size.");
            var reopenedHeaderLength = new FileInfo(temporary).Length;
            var reopenedSidecar = File.ReadAllBytes(Path.ChangeExtension(temporary, ".uexp"));
            var reopenedCdoStart = checked((int)(reopenedCdoRow.SerialOffset - reopenedHeaderLength));
            if (reopenedCdoStart < 0 || reopenedCdoStart + patchedCdoData.Length > reopenedSidecar.Length ||
                !reopenedSidecar.AsSpan(reopenedCdoStart, patchedCdoData.Length).SequenceEqual(patchedCdoData))
                throw new InvalidDataException("Reopened isolated Healing Blueprint CDO sidecar does not contain the exact assigned ArticleDataPath bytes.");
            if (reopenedCdoRow is NormalExport reopenedNormalCdo)
                ValidateCdoArticleDataPath(reopenedNormalCdo, request.Assigned, request.CdoExportName);
            else if (reopenedCdoRow is RawExport reopenedRawCdo &&
                (reopenedRawCdo.Data.Length != patchedCdoData.Length ||
                 !reopenedRawCdo.Data.AsSpan().SequenceEqual(patchedCdoData)))
                throw new InvalidDataException("Reopened isolated Healing RawExport CDO differs from the assigned ArticleDataPath bytes.");
            else if (reopenedCdoRow is not (NormalExport or RawExport))
                throw new InvalidDataException("Reopened isolated Healing Blueprint CDO parsed as an unsupported export type.");
            if (SnapshotImports(reopened) != sourceImports)
                throw new InvalidDataException("Healing Blueprint clone changed external package imports.");
            var reopenedNames = SnapshotNames(reopened);
            ValidateAppendOnlyNames(sourceNames, reopenedNames, new HashSet<string>(requestedNameValues, StringComparer.Ordinal));
            if (!expectedNamesAdded.SequenceEqual(reopenedNames.Skip(sourceNames.Length), StringComparer.Ordinal))
                throw new InvalidDataException("Reopened Healing clone changed the requested NameMap append order or values.");
            ValidateBlueprintExports(sourceRows, reopened, cdoIndex);
            MovePackage(temporary, output, committed);
            reopenValidationTimer.Stop();
            cloneTimer.Stop();
            var timing = new HealingCloneTiming(
                loadTimer.Elapsed.TotalMilliseconds,
                validationTimer.Elapsed.TotalMilliseconds,
                patchTimer.Elapsed.TotalMilliseconds,
                serializationTimer.Elapsed.TotalMilliseconds,
                reopenValidationTimer.Elapsed.TotalMilliseconds,
                cloneTimer.Elapsed.TotalMilliseconds);
            var known = timing.LoadMilliseconds + timing.DiscoveryValidationMilliseconds +
                timing.BinaryFieldPatchMilliseconds + timing.SerializationMilliseconds +
                timing.ReopenValidationMilliseconds;
            timing = timing with { OtherMilliseconds = Math.Max(0, timing.TotalMilliseconds - known) };
            var outputNames = SnapshotNames(reopened);
            var nameIndices = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (var name in outputNames.Select((value, index) => (value, index)))
                if (!nameIndices.ContainsKey(name.value)) nameIndices.Add(name.value, name.index);
            var sourceHeaderSize = new FileInfo(output).Length;
            var outputSidecar = Path.ChangeExtension(output, ".uexp");
            var outputUexpSize = new FileInfo(outputSidecar).Length;
            var outputSidecarBytes = File.ReadAllBytes(outputSidecar);
            var outputRows = SnapshotExports(reopened);
            var outputCdoRow = outputRows[cdoIndex];
            var fieldOffset = checked((int)(outputCdoRow.SerialOffset - sourceHeaderSize) + serializedPath.Offset);
            if (fieldOffset < 0 || fieldOffset + 16 > outputSidecarBytes.Length)
                throw new InvalidDataException("Prepared Healing ArticleDataPath offset is outside the clone .uexp.");
            var outputNamesAdded = outputNames.Skip(sourceNames.Length).ToArray();
            var packageFileHashes = PackageFiles(output)
                .Where(File.Exists)
                .ToDictionary(path => Path.GetExtension(path).ToLowerInvariant(), HashFile, StringComparer.Ordinal);
            return new HealingCloneResult(
                input, output, request.ClonePackagePath,
                request.Expected, request.Assigned, serializedPath.Offset, true, true,
                timing, sourceNames.Length, outputNames.Length, outputNamesAdded,
                fieldOffset, Convert.ToHexString(outputSidecarBytes.AsSpan(fieldOffset, 16)),
                new FileInfo(output).Length, outputUexpSize, packageFileHashes, nameIndices);
        }
        finally
        {
            DeletePackage(temporary);
        }

        throw new InvalidOperationException("Healing clone completed without returning its validated timing result.");
    }


    // Preserve map data and update header

    private static byte[] PreserveMapPayloadAndPatchHeader(
        string inputMap,
        string temporaryMap,
        UAsset serialized,
        ExportRow[] sourceRows,
        IReadOnlyDictionary<int, (FPackageIndex ClassIndex, FPackageIndex TemplateIndex)> assignedIndices,
        long sourceHeaderLength,
        string[] originalNames,
        string[] expectedMapNamesAdded)
    {
        var sourceSidecarPath = Path.ChangeExtension(inputMap, ".uexp");
        var serializedSidecarPath = Path.ChangeExtension(temporaryMap, ".uexp");
        if (!File.Exists(sourceSidecarPath) || !File.Exists(serializedSidecarPath))
            throw new InvalidDataException("Healing map rewrite requires both source and serialized .uexp sidecars.");
        if (serialized.Exports.Count != sourceRows.Length)
            throw new InvalidDataException("UAssetAPI changed map export count while appending healing imports.");
        var sourceSidecar = File.ReadAllBytes(sourceSidecarPath);
        var writtenHeaderLength = new FileInfo(temporaryMap).Length;
        var expectedHeaderDelta = writtenHeaderLength - sourceHeaderLength;
        var serializedNames = SnapshotNames(serialized);
        if (serializedNames.Length < originalNames.Length ||
            !originalNames.SequenceEqual(serializedNames.Take(originalNames.Length)))
            throw new InvalidDataException("Healing map rewrite did not preserve the original NameMap prefix.");
        if (!expectedMapNamesAdded.SequenceEqual(
                serializedNames.Skip(originalNames.Length).Take(expectedMapNamesAdded.Length), StringComparer.Ordinal))
            throw new InvalidDataException("Healing map rewrite changed the requested NameMap append order or values.");
        var expectedAdded = assignedIndices.Count;
        var patchPaths = serialized.Imports
            .Where(item => item.ClassName.ToString() == "Package" &&
                assignedIndices.Values.Any(value => value.ClassIndex.IsImport() &&
                    String.Equals(item.ObjectName.ToString(), ResolvePackagePath(serialized, value.ClassIndex), StringComparison.Ordinal)))
            .Select(item => item.ObjectName.ToString()).ToHashSet(StringComparer.Ordinal);
        if (patchPaths.Count != expectedAdded)
            throw new InvalidDataException("Healing map imports do not contain one isolated clone package per physical location.");
        var allowedAppendedNames = new HashSet<string>(patchPaths, StringComparer.Ordinal);
        allowedAppendedNames.UnionWith(expectedMapNamesAdded);
        ValidateAppendOnlyNames(originalNames, serializedNames, allowedAppendedNames);
        if (serializedNames.Skip(originalNames.Length + expectedMapNamesAdded.Length).Any(name => !patchPaths.Contains(name)))
            throw new InvalidDataException("Healing map imports appended an unexpected NameMap value.");

        PatchExportRows(temporaryMap, serialized, sourceRows, expectedHeaderDelta, assignedIndices);
        return sourceSidecar;
    }


    // Check generated map output

    private static void ValidateMapOutput(
        string inputMap,
        string outputMap,
        UAsset reopened,
        ExportRow[] originalRows,
        string[] originalNames,
        string[] expectedMapNamesAdded,
        List<string> requestedMapNames,
        string originalImports,
        IReadOnlyDictionary<int, ValidatedHealingPatch> patches,
        IReadOnlyDictionary<int, (FPackageIndex ClassIndex, FPackageIndex TemplateIndex)> expectedIndices)
    {
        if (reopened.Exports.Count != originalRows.Length)
            throw new InvalidDataException("Reopened healing map export count changed.");
        var headerDelta = new FileInfo(outputMap).Length - new FileInfo(inputMap).Length;
        for (var index = 0; index < reopened.Exports.Count; index++)
        {
            var export = reopened.Exports[index];
            var original = originalRows[index];
            var expectedClass = original.ClassIndex;
            var expectedTemplate = original.TemplateIndex;
            if (expectedIndices.TryGetValue(index, out var assignedIndicesForExport))
            {
                expectedClass = assignedIndicesForExport.ClassIndex.Index;
                expectedTemplate = assignedIndicesForExport.TemplateIndex.Index;
            }
            if (export.ObjectName.ToString() != original.ObjectName ||
                export.SuperIndex.Index != original.SuperIndex ||
                export.OuterIndex.Index != original.OuterIndex ||
                !IsCompatibleExportType(original.ExportType, export.GetType().FullName ?? "") ||
                export.ClassIndex.Index != expectedClass ||
                export.TemplateIndex.Index != expectedTemplate ||
                export.SerialSize != original.SerialSize ||
                export.SerialOffset != checked(original.SerialOffset + headerDelta))
                throw new InvalidDataException($"Reopened healing map changed unrelated export metadata at map export #{index + 1}.");
        }
        var outputImports = SnapshotImports(reopened);
        var appendedImportCount = expectedIndices.Count * 3;
        var sourceImportLines = originalImports.Split('\0');
        var outputImportLines = outputImports.Split('\0');
        if (outputImportLines.Length != sourceImportLines.Length + appendedImportCount ||
            !sourceImportLines.SequenceEqual(outputImportLines.Take(sourceImportLines.Length)))
            throw new InvalidDataException("Reopened healing map did not preserve its original Imports prefix and append exactly three imports per location.");
        var reopenedNames = SnapshotNames(reopened);
        if (!originalNames.SequenceEqual(reopenedNames.Take(originalNames.Length)))
            throw new InvalidDataException("Reopened healing map changed existing NameMap entries or indices.");
        if (!expectedMapNamesAdded.SequenceEqual(
                reopenedNames.Skip(originalNames.Length).Take(expectedMapNamesAdded.Length), StringComparer.Ordinal))
            throw new InvalidDataException("Reopened healing map changed the requested NameMap append order or values.");
        var allowedNames = new HashSet<string>(expectedMapNamesAdded, StringComparer.Ordinal);
        allowedNames.UnionWith(patches.Values.Select(value => value.Request.ClonePackagePath));
        ValidateAppendOnlyNames(originalNames, reopenedNames, allowedNames);
        var expectedMapNameSet = requestedMapNames.ToHashSet(StringComparer.Ordinal);
        if (expectedMapNameSet.Any(name => !reopenedNames.Contains(name, StringComparer.Ordinal)))
            throw new InvalidDataException("Reopened healing map omitted a requested World Loot NameMap value.");
        foreach (var pair in patches)
        {
            var patch = pair.Value.Request;
            var actor = reopened.Exports[pair.Key];
            var indices = expectedIndices[pair.Key];
            if (actor.ObjectName.ToString() != patch.ExportName ||
                actor.ClassIndex.Index != indices.ClassIndex.Index ||
                actor.TemplateIndex.Index != indices.TemplateIndex.Index ||
                ResolveBlueprintClassReference(reopened, actor.ClassIndex) !=
                    (patch.ExpectedClassName, patch.ClonePackagePath) ||
                ResolveBlueprintDefaultObjectReference(reopened, actor.TemplateIndex) !=
                    ("Default__" + patch.ExpectedClassName, patch.ClonePackagePath))
                throw new InvalidDataException("Reopened healing actor does not point to its isolated Blueprint class and CDO clone.");
        }
        var sourceSidecar = File.ReadAllBytes(Path.ChangeExtension(inputMap, ".uexp"));
        var outputSidecar = File.ReadAllBytes(Path.ChangeExtension(outputMap, ".uexp"));
        if (!sourceSidecar.AsSpan().SequenceEqual(outputSidecar))
            throw new InvalidDataException("Healing map .uexp changed; this patch is limited to its import/export header references.");
    }


    // Patch selected export rows

    private static void PatchExportRows(
        string headerPath,
        UAsset serialized,
        ExportRow[] sourceRows,
        long headerDelta,
        IReadOnlyDictionary<int, (FPackageIndex ClassIndex, FPackageIndex TemplateIndex)>? assignedIndices)
    {
        if (serialized.Exports.Count != sourceRows.Length)
            throw new InvalidDataException("Cannot preserve raw payloads after a package export inventory change.");
        var header = File.ReadAllBytes(headerPath);
        var offsets = FindExportMapOffsets(headerPath, serialized);
        for (var index = 0; index < serialized.Exports.Count; index++)
        {
            var export = serialized.Exports[index];
            var row = sourceRows[index];
            var expectedClassIndex = row.ClassIndex;
            var expectedTemplateIndex = row.TemplateIndex;
            if (assignedIndices is not null && assignedIndices.TryGetValue(index, out var expectedAssignment))
            {
                expectedClassIndex = expectedAssignment.ClassIndex.Index;
                expectedTemplateIndex = expectedAssignment.TemplateIndex.Index;
            }
            var serializedRow = new ExportRow(
                export.SerialSize,
                export.SerialOffset,
                export.ObjectName.ToString(),
                export.ClassIndex.Index,
                export.SuperIndex.Index,
                export.TemplateIndex.Index,
                export.OuterIndex.Index,
                export.GetType().FullName ?? "");
            var identityDifferences = SerializedExportRowDifferences(
                row, serializedRow, expectedClassIndex, expectedTemplateIndex, headerDelta);
            if (identityDifferences.Count > 0)
                throw new InvalidDataException(
                    $"UAssetAPI changed map export identity outside the patched class/template fields at map export #{index + 1}: " +
                    String.Join(", ", identityDifferences) + ".");
            var classIndex = assignedIndices is not null && assignedIndices.TryGetValue(index, out var assigned)
                ? assigned.ClassIndex.Index : row.ClassIndex;
            var templateIndex = assignedIndices is not null && assignedIndices.TryGetValue(index, out assigned)
                ? assigned.TemplateIndex.Index : row.TemplateIndex;
            BinaryPrimitives.WriteInt32LittleEndian(header.AsSpan(checked((int)offsets[index].ClassIndex), 4), classIndex);
            BinaryPrimitives.WriteInt32LittleEndian(header.AsSpan(checked((int)offsets[index].TemplateIndex), 4), templateIndex);
            // UAssetAPI may normalize temporary SerialSize/SerialOffset values while rewriting the
            // .uasset header. The generated payload is discarded: the caller restores the original
            // .uexp sidecar, so restore the source table's exact size/offset pair here. Reopening
            // then validates every final pair against the source package.
            var serialSize = row.SerialSize;
            var serialOffset = checked(row.SerialOffset + headerDelta);
            EncodePair(serialSize, serialOffset).CopyTo(header.AsSpan(checked((int)offsets[index].SerialPair), 16));
        }
        File.WriteAllBytes(headerPath, header);
    }


    // Find export Map offsets

    private static ExportMapOffsets[] FindExportMapOffsets(string headerPath, UAsset asset)
    {
        var exportOffsetField = typeof(UAsset).GetField("ExportOffset", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            ?? throw new InvalidDataException("Pinned UAssetAPI does not expose the export-map offset needed for raw preservation.");
        var exportOffset = Convert.ToInt64(exportOffsetField.GetValue(asset));
        var header = File.ReadAllBytes(headerPath);
        using var stream = File.OpenRead(headerPath);
        using var reader = new AssetBinaryReader(stream, asset);
        reader.BaseStream.Position = exportOffset;
        var offsets = new ExportMapOffsets[asset.Exports.Count];
        for (var index = 0; index < asset.Exports.Count; index++)
        {
            var entryStart = reader.BaseStream.Position;
            var classIndex = reader.ReadInt32();
            var superIndex = reader.ReadInt32();
            var templateIndex = reader.ReadInt32();
            if (classIndex != asset.Exports[index].ClassIndex.Index ||
                superIndex != asset.Exports[index].SuperIndex.Index ||
                templateIndex != asset.Exports[index].TemplateIndex.Index)
                throw new InvalidDataException("Export-map class/super/template offsets do not match the pinned UAssetAPI parser.");
            reader.BaseStream.Position = entryStart;
            asset.Exports[index].ReadExportMapEntry(reader);
            var entryEnd = reader.BaseStream.Position;
            var pairBytes = EncodePair(asset.Exports[index].SerialSize, asset.Exports[index].SerialOffset);
            var entry = header.AsSpan(checked((int)entryStart), checked((int)(entryEnd - entryStart)));
            var pairOffset = FindUnique(entry, pairBytes, $"export #{index + 1} serial-size/offset pair");
            offsets[index] = new ExportMapOffsets(entryStart, entryStart + 8, entryStart + pairOffset);
        }
        return offsets;
    }


    // Find unique f name path

    private static SerializedFNamePath FindUniqueFNamePath(
        byte[] data,
        (int[] Package, int[] Asset) indices,
        SoftObjectPathIdentityDto identity)
    {
        var matches = new List<SerializedFNamePath>();
        for (var offset = 0; offset <= data.Length - 16; offset += 2)
        {
            var packageIndex = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset, 4));
            var packageNumber = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset + 4, 4));
            var assetIndex = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset + 8, 4));
            var assetNumber = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset + 12, 4));
            if (indices.Package.Contains(packageIndex) && packageNumber == identity.PackageName.Number &&
                indices.Asset.Contains(assetIndex) && assetNumber == identity.AssetName.Number)
                matches.Add(new SerializedFNamePath(offset));
        }
        if (matches.Count != 1)
            throw new InvalidDataException($"Expected one serialized ArticleDataPath FName pair in healing CDO; found {matches.Count}.");
        return matches[0];
    }


    // Write f name path

    private static void WriteFNamePath(
        byte[] data,
        SerializedFNamePath path,
        int packageIndex,
        int packageNumber,
        int assetIndex,
        int assetNumber)
    {
        WriteInt32(data, path.Offset, packageIndex);
        WriteInt32(data, path.Offset + 4, packageNumber);
        WriteInt32(data, path.Offset + 8, assetIndex);
        WriteInt32(data, path.Offset + 12, assetNumber);
    }

    private static (int[] Package, int[] Asset) FindNameIndices(string[] names, SoftObjectPathIdentityDto identity)
    {
        var package = names.Select((name, index) => (name, index))
            .Where(item => item.name == identity.PackageName.Value).Select(item => item.index).ToArray();
        var asset = names.Select((name, index) => (name, index))
            .Where(item => item.name == identity.AssetName.Value).Select(item => item.index).ToArray();
        if (package.Length == 0 || asset.Length == 0)
            throw new InvalidDataException("Catalogued healing ArticleDataPath literal is absent from its source CDO NameMap.");
        return (package, asset);
    }


    // Find name index

    private static int FindNameIndex(string[] names, string requested)
    {
        var matches = names.Select((name, index) => (name, index))
            .Where(item => String.Equals(item.name, requested, StringComparison.Ordinal))
            .Select(item => item.index).ToArray();
        if (matches.Length == 0) throw new InvalidDataException($"Healing CDO NameMap omitted required literal '{requested}'.");
        return matches[0];
    }

    private static (string ClassObjectName, string PackagePath) ResolveBlueprintClassReference(UAsset asset, FPackageIndex index)
    {
        var reference = ResolveImportReference(asset, index);
        if (reference.ImportClassName != "BlueprintGeneratedClass")
            throw new InvalidDataException("Healing map class reference is not a BlueprintGeneratedClass import.");
        return (reference.ObjectName, reference.PackagePath);
    }

    private static (string ObjectName, string PackagePath) ResolveBlueprintDefaultObjectReference(UAsset asset, FPackageIndex index)
    {
        var reference = ResolveImportReference(asset, index);
        if (!reference.ObjectName.StartsWith("Default__", StringComparison.Ordinal))
            throw new InvalidDataException("Healing map TemplateIndex is not a Blueprint Default__ CDO import.");
        return (reference.ObjectName, reference.PackagePath);
    }

    private static (string ObjectName, string PackagePath, string ImportClassName) ResolveImportReference(UAsset asset, FPackageIndex index)
    {
        if (!index.IsImport()) throw new InvalidDataException("Healing Blueprint reference is not an import.");
        var first = index.ToImport(asset);
        var current = index;
        var visited = new HashSet<int>();
        while (current.IsImport() && visited.Add(current.Index))
        {
            var import = current.ToImport(asset);
            if (import.ClassName.ToString() == "Package")
                return (first.ObjectName.ToString(), import.ObjectName.ToString(), first.ClassName.ToString());
            current = import.OuterIndex;
        }
        throw new InvalidDataException("Healing Blueprint import chain has no package outer.");
    }


    // Resolve the package path

    private static string ResolvePackagePath(UAsset asset, FPackageIndex index)
    {
        var current = index;
        var visited = new HashSet<int>();
        while (current.IsImport() && visited.Add(current.Index))
        {
            var import = current.ToImport(asset);
            if (import.ClassName.ToString() == "Package") return import.ObjectName.ToString();
            current = import.OuterIndex;
        }
        return String.Empty;
    }


    // Check the CDO Article path

    private static void ValidateCdoArticleDataPath(NormalExport export, RewardTuple expected, string cdoName)
    {
        var bulkMatches = export.Data.OfType<StructPropertyData>()
            .Where(property => property.Name.ToString() == "ArticleBulkInfo" || property.StructType.ToString() == "ArticleBulkInfo")
            .ToArray();
        if (bulkMatches.Length != 1)
            throw new InvalidDataException($"Healing CDO '{cdoName}' must have exactly one parsed ArticleBulkInfo.");
        var arrays = bulkMatches[0].Value.OfType<ArrayPropertyData>()
            .Where(property => property.Name.ToString() == "Articles").ToArray();
        if (arrays.Length != 1)
            throw new InvalidDataException($"Healing CDO '{cdoName}' must have exactly one ArticleBulkInfo.Articles array.");
        var articles = arrays[0].Value.OfType<StructPropertyData>().ToArray();
        if (articles.Length != 1)
            throw new InvalidDataException($"Healing CDO '{cdoName}' must contain exactly one ArticleDataPath reward.");
        var pathProperties = articles[0].Value.OfType<SoftObjectPropertyData>()
            .Where(property => property.Name.ToString() == "ArticleDataPath").ToArray();
        var counts = articles[0].Value.OfType<IntPropertyData>()
            .Where(property => property.Name.ToString() == "Count").ToArray();
        var notifications = articles[0].Value.OfType<BoolPropertyData>()
            .Where(property => property.Name.ToString() == "bNotification").ToArray();
        if (pathProperties.Length != 1 || counts.Length != 1 || notifications.Length != 1)
            throw new InvalidDataException($"Healing CDO '{cdoName}' ArticleBulkInfo reward fields are incomplete or ambiguous.");
        var path = (FSoftObjectPath)pathProperties[0].Value!;

        var packageName = path.AssetPath.PackageName;
        var assetName = path.AssetPath.AssetName;
        var package = ResolveFName(new FNameIdentityDto(packageName.Value.ToString(), packageName.Number));
        var asset = ResolveFName(new FNameIdentityDto(assetName.Value.ToString(), assetName.Number));
        if (!String.Equals(package, expected.Path, StringComparison.Ordinal) ||
            !String.Equals(asset, expected.Path[(expected.Path.LastIndexOf('/') + 1)..], StringComparison.Ordinal) ||
            counts[0].Value != expected.Count || notifications[0].Value != expected.Notification)
            throw new InvalidDataException($"Healing CDO '{cdoName}' parsed ArticleDataPath/Count/bNotification differs from the expected reward '{expected.Path}'.");
        var expectedIdentity = expected.SoftObjectPathIdentity
            ?? throw new InvalidDataException("Healing expected reward has no exact ArticleDataPath FName identity.");
        if (packageName.Value.ToString() != expectedIdentity.PackageName.Value ||
            packageName.Number != expectedIdentity.PackageName.Number ||
            assetName.Value.ToString() != expectedIdentity.AssetName.Value ||
            assetName.Number != expectedIdentity.AssetName.Number)
            throw new InvalidDataException($"Healing CDO '{cdoName}' parsed ArticleDataPath FName identity differs from the expected source identity.");
    }


    // Check Blueprint exports

    private static void ValidateBlueprintExports(ExportRow[] sourceRows, UAsset reopened, int cdoIndex)
    {
        for (var index = 0; index < sourceRows.Length; index++)
        {
            var export = reopened.Exports[index];
            var source = sourceRows[index];
            if (export.ObjectName.ToString() != source.ObjectName || export.GetType().FullName != source.ExportType ||
                export.ClassIndex.Index != source.ClassIndex || export.SuperIndex.Index != source.SuperIndex ||
                export.TemplateIndex.Index != source.TemplateIndex || export.OuterIndex.Index != source.OuterIndex ||
                export.SerialSize != source.SerialSize)
                throw new InvalidDataException($"Healing Blueprint clone changed unrelated export metadata at export #{index + 1}.");
            if (index == cdoIndex && export.ObjectName.ToString() != source.ObjectName)
                throw new InvalidDataException("Healing Blueprint CDO export identity changed after serialization.");
        }
    }


    // Capture package exports

    private static ExportRow[] SnapshotExports(UAsset asset) => asset.Exports.Select(item => new ExportRow(
        item.SerialSize, item.SerialOffset, item.ObjectName.ToString(), item.ClassIndex.Index,
        item.SuperIndex.Index, item.TemplateIndex.Index, item.OuterIndex.Index, item.GetType().FullName ?? "")).ToArray();


    // Check compatible export types

    private static bool IsCompatibleExportType(string sourceType, string serializedType) =>
        String.Equals(sourceType, serializedType, StringComparison.Ordinal) ||
        (String.Equals(sourceType, "UAssetAPI.ExportTypes.RawExport", StringComparison.Ordinal) &&
         String.Equals(serializedType, "UAssetAPI.ExportTypes.NormalExport", StringComparison.Ordinal));


    // Find changed export fields

    private static List<string> SerializedExportRowDifferences(
        ExportRow source,
        ExportRow serialized,
        int expectedClassIndex,
        int expectedTemplateIndex,
        long headerDelta)
    {
        var differences = new List<string>();
        if (serialized.ObjectName != source.ObjectName)
            differences.Add($"objectName '{source.ObjectName}' -> '{serialized.ObjectName}'");
        if (serialized.SuperIndex != source.SuperIndex)
            differences.Add($"superIndex {source.SuperIndex} -> {serialized.SuperIndex}");
        if (serialized.OuterIndex != source.OuterIndex)
            differences.Add($"outerIndex {source.OuterIndex} -> {serialized.OuterIndex}");
        if (!IsCompatibleExportType(source.ExportType, serialized.ExportType))
            differences.Add($"exportType '{source.ExportType}' -> '{serialized.ExportType}'");
        if (serialized.ClassIndex != expectedClassIndex)
            differences.Add($"classIndex {serialized.ClassIndex} != expected {expectedClassIndex}");
        if (serialized.TemplateIndex != expectedTemplateIndex)
            differences.Add($"templateIndex {serialized.TemplateIndex} != expected {expectedTemplateIndex}");
        // SerialSize/SerialOffset are deliberately not compared here. PatchExportRows restores the
        // source size and offset pair, and ValidateMapOutput rejects output whose header cannot reopen.
        return differences;
    }


    // Capture names

    private static string[] SnapshotNames(UAsset asset) => asset.GetNameMapIndexList()
        .Select((_, index) => asset.GetNameReference(index).ToString()).ToArray();


    // Inspect map actor classes

    private static List<MapClassInspection> InspectMapClasses(
        UAsset asset, IReadOnlyList<MapClassExportRequest> requests)
    {
        var results = new List<MapClassInspection>();
        foreach (var request in requests.OrderBy(item => item.ExportIndex))
        {
            if (request.ExportIndex < 1 || request.ExportIndex > asset.Exports.Count)
                throw new InvalidDataException("Healing map class-discovery export index is outside the package.");
            var export = asset.Exports[request.ExportIndex - 1];
            if (!String.Equals(export.ObjectName.ToString(), request.ExportName, StringComparison.Ordinal))
                throw new InvalidDataException("Healing map class-discovery export identity differs from the catalog.");
            var (className, packagePath) = ResolveClassImport(asset, export.ClassIndex);
            results.Add(new MapClassInspection(export.ObjectName.ToString(), request.ExportIndex, className, packagePath));
        }
        return results;
    }


    // Describe World Loot fields

    private static List<HealingMapFieldDescriptor> DescribeWorldLootFields(
        string inputMap,
        UAsset asset,
        IReadOnlyList<MapRewardFieldRequest> requests,
        long headerSize,
        string[] nameMap)
    {
        var results = new List<HealingMapFieldDescriptor>();
        var sidecar = Path.ChangeExtension(inputMap, ".uexp");
        foreach (var request in requests.OrderBy(item => item.ExportIndex).ThenBy(item => item.RewardIndex))
        {
            try
            {
                if (request.ExportIndex < 1 || request.ExportIndex > asset.Exports.Count)
                    throw new InvalidDataException("World Loot export index is outside the Healing map.");
                var export = asset.Exports[request.ExportIndex - 1];
                if (!String.Equals(export.ObjectName.ToString(), request.ExportName, StringComparison.Ordinal))
                    throw new InvalidDataException("World Loot export identity differs from the Healing map catalog.");
                ValidateReward(request.Expected);
                var identity = request.Expected.SoftObjectPathIdentity
                    ?? throw new InvalidDataException("World Loot ArticleDataPath has no exact FName identity.");
                if (export is NormalExport normal)
                    ValidateMapArticleReward(normal, request.RewardIndex, request.Expected);
                else if (export is not RawExport)
                    throw new InvalidDataException($"World Loot export parsed as unsupported {export.GetType().Name}.");
                if (!File.Exists(sidecar))
                    throw new InvalidDataException("World Loot source map has no .uexp sidecar.");
                var record = asset.Exports[request.ExportIndex - 1];
                var payloadStart = checked((int)(record.SerialOffset - headerSize));
                var payload = ReadRange(sidecar, payloadStart, record.SerialSize);
                var field = FindUniqueFNamePath(payload, FindNameIndices(nameMap, identity), identity);
                results.Add(new HealingMapFieldDescriptor(
                    request.ExportIndex, request.RewardIndex, request.ExportName,
                    checked(payloadStart + field.Offset), Convert.ToHexString(payload.AsSpan(field.Offset, 16)),
                    identity, true, null));
            }
            catch (Exception exception)
            {
                results.Add(new HealingMapFieldDescriptor(
                    request.ExportIndex, request.RewardIndex, request.ExportName,
                    null, null, request.Expected.SoftObjectPathIdentity, false,
                    $"{exception.GetType().Name}: {exception.Message}"));
            }
        }
        return results;
    }


    // Check the map Article reward

    private static void ValidateMapArticleReward(NormalExport export, int rewardIndex, RewardTuple expected)
    {
        var articles = export.Data.OfType<StructPropertyData>()
            .Single(property => property.Name.ToString() == "ArticleBulkInfo")
            .Value.OfType<ArrayPropertyData>()
            .Single(property => property.Name.ToString() == "Articles");
        if (rewardIndex < 0 || rewardIndex >= articles.Value.Count())
            throw new InvalidDataException("World Loot reward index is outside ArticleBulkInfo.Articles.");
        var article = articles.Value.ElementAt(rewardIndex) as StructPropertyData
            ?? throw new InvalidDataException("World Loot Article entry is not a struct.");
        var path = (FSoftObjectPath)article.Value.OfType<SoftObjectPropertyData>()
            .Single(property => property.Name.ToString() == "ArticleDataPath").Value!;
        var packageName = path.AssetPath.PackageName;
        var assetName = path.AssetPath.AssetName;
        var actualIdentity = new SoftObjectPathIdentityDto(
            new FNameIdentityDto(packageName.Value.ToString(), packageName.Number),
            new FNameIdentityDto(assetName.Value.ToString(), assetName.Number));
        var actualPath = ResolveFName(actualIdentity.PackageName);
        if (ResolveFName(actualIdentity.AssetName) != actualPath[(actualPath.LastIndexOf('/') + 1)..] ||
            actualPath != expected.Path || actualIdentity != expected.SoftObjectPathIdentity)
            throw new InvalidDataException("World Loot ArticleDataPath differs from the expected source reward.");
        var count = article.Value.OfType<IntPropertyData>()
            .Single(property => property.Name.ToString() == "Count").Value;
        var notification = article.Value.OfType<BoolPropertyData>()
            .Single(property => property.Name.ToString() == "bNotification").Value;
        if (count != expected.Count || notification != expected.Notification)
            throw new InvalidDataException("World Loot Count or notification differs from the expected source reward.");
    }

    private static (string ClassName, string PackagePath) ResolveClassImport(UAsset asset, FPackageIndex packageIndex)
    {
        if (!packageIndex.IsImport())
            return (packageIndex.IsExport() ? "ExportClass" : "None", String.Empty);
        var first = packageIndex.ToImport(asset);
        var className = first.ClassName.ToString();
        var current = packageIndex;
        var visited = new HashSet<int>();
        while (current.IsImport() && visited.Add(current.Index))
        {
            var import = current.ToImport(asset);
            if (import.ClassName.ToString() == "Package")
                return (className, import.ObjectName.ToString());
            current = import.OuterIndex;
        }
        return (className, String.Empty);
    }


    // Capture package imports

    private static string SnapshotImports(UAsset asset) => String.Join("\0", asset.Imports.Select(item =>
        $"{item.ClassPackage}\u001f{item.ClassName}\u001f{item.ObjectName}\u001f{item.OuterIndex.Index}\u001f{item.PackageName}"));


    // Check append only names

    private static void ValidateAppendOnlyNames(string[] source, string[] output, IReadOnlySet<string> allowed)
    {
        if (output.Length < source.Length || !source.SequenceEqual(output.Take(source.Length)))
            throw new InvalidDataException("Healing package serialization changed existing NameMap values or indices.");
        if (output.Skip(source.Length).Any(name => !allowed.Contains(name)))
            throw new InvalidDataException("Healing package serialization appended an unrelated NameMap value.");
    }


    // Check the randomized reward

    private static void ValidateReward(RewardTuple reward)
    {
        if (reward.SoftObjectPathIdentity is not { } identity ||
            ResolveFName(identity.PackageName) != reward.Path ||
            ResolveFName(identity.AssetName) != reward.Path[(reward.Path.LastIndexOf('/') + 1)..] ||
            reward.Count <= 0)
            throw new InvalidDataException("Healing reward tuple has an invalid ArticleDataPath identity.");
    }


    // Resolve a NameMap FName

    private static string ResolveFName(FNameIdentityDto name) => name.Number == 0 ? name.Value : $"{name.Value}_{name.Number - 1}";


    // Check canonical package paths

    private static void ValidateCanonicalPackagePath(string path, string label)
    {
        var parts = path.Split('/', StringSplitOptions.RemoveEmptyEntries);
        if (!path.StartsWith("/Game/", StringComparison.Ordinal) || path.Contains('\\') || path.EndsWith('/') ||
            parts.Length < 3 || parts.Any(part => part is "." or ".."))
            throw new InvalidDataException($"Healing {label} virtual path must be canonical under /Game/.");
    }


    // Resolve a Randomizer-owned path

    private static FileInfo OwnedPath(string root, string value, bool mustExist)
    {
        var resolvedRoot = Path.GetFullPath(root);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(resolvedRoot, path);
        if (relative is "." or ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new InvalidDataException("Healing patch path escaped its generation work root.");
        if (mustExist && !File.Exists(path)) throw new FileNotFoundException("Healing patch input package was not found.", path);
        return new FileInfo(path);
    }


    // Create a temporary sibling path

    private static string TemporarySibling(string target) => Path.Combine(
        Path.GetDirectoryName(target)!, Path.GetFileNameWithoutExtension(target) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(target));


    // Encode the export offset pair

    private static byte[] EncodePair(long size, long offset)
    {
        var bytes = new byte[16];
        BinaryPrimitives.WriteInt64LittleEndian(bytes.AsSpan(0, 8), size);
        BinaryPrimitives.WriteInt64LittleEndian(bytes.AsSpan(8, 8), offset);
        return bytes;
    }


    // Find one exact match

    private static int FindUnique(ReadOnlySpan<byte> bytes, ReadOnlySpan<byte> needle, string label)
    {
        var first = bytes.IndexOf(needle);
        if (first < 0 || bytes[(first + 1)..].IndexOf(needle) >= 0)
            throw new InvalidDataException($"Could not uniquely locate {label} in its ExportMap row.");
        return first;
    }


    // Write a 32-bit integer

    private static void WriteInt32(byte[] data, int offset, int value) =>
        BinaryPrimitives.WriteInt32LittleEndian(data.AsSpan(offset, 4), value);


    // Read int 32

    private static int ReadInt32(byte[] data, int offset) => BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset, 4));


    // Check only range changed

    private static void EnsureOnlyRangeChanged(byte[] before, byte[] after, int start, int length, string error)
    {
        if (before.Length != after.Length) throw new InvalidDataException(error);
        for (var index = 0; index < before.Length; index++)
            if ((index < start || index >= start + length) && before[index] != after[index])
                throw new InvalidDataException(error);
    }


    // Copy opaque sidecars

    private static void CopyOpaqueSidecars(string input, string temporaryOutput)
    {
        foreach (var extension in new[] { ".ubulk", ".uptnl" })
        {
            var source = Path.ChangeExtension(input, extension);
            var temporary = Path.ChangeExtension(temporaryOutput, extension);
            if (File.Exists(source))
            {
                File.Copy(source, temporary, overwrite: true);
                if (!File.ReadAllBytes(source).AsSpan().SequenceEqual(File.ReadAllBytes(temporary)))
                    throw new InvalidDataException($"Healing package failed to preserve its opaque {extension} sidecar.");
            }
            else if (File.Exists(temporary))
                throw new InvalidDataException($"UAssetAPI unexpectedly created an unrequested {extension} sidecar.");
        }
    }


    // Move the generated package

    private static void MovePackage(string temporary, string output, List<string> committed)
    {
        var files = PackageFiles(temporary).Where(File.Exists).ToArray();
        if (!files.Contains(temporary, StringComparer.OrdinalIgnoreCase) || !File.Exists(Path.ChangeExtension(temporary, ".uexp")))
            throw new InvalidDataException("Healing package output is missing its header or .uexp sidecar.");
        var destinations = files.Select(file => Path.GetExtension(file) == Path.GetExtension(temporary)
            ? output : Path.ChangeExtension(output, Path.GetExtension(file))).ToArray();
        if (files.Length != destinations.Length || destinations.Any(File.Exists))
            throw new IOException("Healing package output would overwrite an existing package file.");
        for (var index = 0; index < files.Length; index++)
        {
            var destination = destinations[index];
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            File.Move(files[index], destination);
            committed.Add(destination);
        }
    }


    // Delete the generated package

    private static void DeletePackage(string header)
    {
        foreach (var path in PackageFiles(header))
            try { if (File.Exists(path)) File.Delete(path); } catch (IOException) { }
    }


    // Collect package files

    private static string[] PackageFiles(string header) =>
        new[] { header, Path.ChangeExtension(header, ".uexp"), Path.ChangeExtension(header, ".ubulk"), Path.ChangeExtension(header, ".uptnl") };


    // Hash file

    private static string HashFile(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }


    // Read range

    private static byte[] ReadRange(string path, int offset, long length)
    {
        if (offset < 0 || length < 0 || length > Int32.MaxValue)
            throw new InvalidDataException("Serialized map export range is invalid.");
        var result = new byte[checked((int)length)];
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        if ((long)offset + length > stream.Length)
            throw new InvalidDataException("Serialized map export range is outside its .uexp sidecar.");
        stream.Position = offset;
        stream.ReadExactly(result);
        return result;
    }


    // Describe healing batch document

    private sealed record HealingBatchDocument(List<HealingPackageRequest> Packages);
    // Describe healing batch result
    private sealed record HealingBatchResult(
        List<HealingPackageResult> Packages,
        double HelperStartupAndMappingsMilliseconds,
        double PackageTotalMilliseconds);
    // Describe healing package request
    private sealed record HealingPackageRequest(
        string InputFile,
        string OutputFile,
        List<HealingPatchRequest> Patches,
        List<string>? MapNameMapValues = null,
        List<MapClassExportRequest>? ClassExports = null,
        List<MapRewardFieldRequest>? WorldLootFields = null);
    // Map class export request
    private sealed record MapClassExportRequest(int ExportIndex, string ExportName);
    // Map reward field request
    private sealed record MapRewardFieldRequest(int ExportIndex, int RewardIndex, string ExportName, RewardTuple Expected);
    // Map class inspection
    private sealed record MapClassInspection(string ExportName, int ExportIndex, string ClassName, string PackagePath);
    // Describe healing Map field descriptor
    private sealed record HealingMapFieldDescriptor(
        int ExportIndex, int RewardIndex, string ExportName, int? Offset,
        string? ExpectedBytesHex, SoftObjectPathIdentityDto? ExpectedIdentity,
        bool Available, string? UnavailableReason);
    // Describe healing patch request
    private sealed record HealingPatchRequest(
        int ExportIndex,
        string ExportName,
        string ExpectedClassPackage,
        string ExpectedClassName,
        int CdoExportIndex,
        string CdoExportName,
        string BlueprintInputFile,
        string BlueprintOutputFile,
        string ClonePackagePath,
        RewardTuple Expected,
        RewardTuple Assigned,
        List<string>? NameMapValues = null);
    // Describe healing package result
    private sealed record HealingPackageResult(
        string InputFile,
        string OutputFile,
        int PatchCount,
        List<HealingCloneResult> Clones,
        bool ReopenedAndValidated,
        bool RawPayloadPreserved,
        HealingPackageTiming Timing,
        int NameMapCountBefore,
        int NameMapCountAfter,
        string[] NamesAdded,
        long HeaderSize,
        long UexpSize,
        Dictionary<string, string> PackageFileHashes,
        Dictionary<string, int> NameMapIndices,
        List<MapClassInspection> ClassInspections,
        List<HealingMapFieldDescriptor> WorldLootFields,
        int DiscoveryOpenCount,
        int PreparationReloadCount,
        int SerializationReopenCount,
        int ValidationReopenCount);
    // Describe healing clone result
    private sealed record HealingCloneResult(
        string InputFile,
        string OutputFile,
        string ClonePackagePath,
        RewardTuple Original,
        RewardTuple Assigned,
        int ArticleDataPathOffset,
        bool ReopenedAndValidated,
        bool RawPayloadPreserved,
        HealingCloneTiming Timing,
        int NameMapCountBefore,
        int NameMapCountAfter,
        string[] NamesAdded,
        int FieldOffset,
        string ExpectedBytesHex,
        long HeaderSize,
        long UexpSize,
        Dictionary<string, string> PackageFileHashes,
        Dictionary<string, int> NameMapIndices);
    // Describe healing package timing
    private sealed record HealingPackageTiming(
        double LoadMilliseconds,
        double DiscoveryValidationMilliseconds,
        double CloneMaterializationMilliseconds,
        double MapMutationMilliseconds,
        double MapSerializationMilliseconds,
        double MapReopenValidationMilliseconds,
        double TotalMilliseconds,
        double OtherMilliseconds = 0,
        double SerializationMetadataReopenMilliseconds = 0);
    // Describe healing clone timing
    private sealed record HealingCloneTiming(
        double LoadMilliseconds,
        double DiscoveryValidationMilliseconds,
        double BinaryFieldPatchMilliseconds,
        double SerializationMilliseconds,
        double ReopenValidationMilliseconds,
        double TotalMilliseconds,
        double OtherMilliseconds = 0);
    // Read validated healing patch
    private sealed record ValidatedHealingPatch(
        HealingPatchRequest Request,
        string BlueprintInput,
        string BlueprintOutput,
        FPackageIndex OriginalClassIndex,
        FPackageIndex OriginalTemplateIndex,
        string OriginalTemplateObject,
        string AssignedTemplateObject);
    // Read export row
    private sealed record ExportRow(
        long SerialSize,
        long SerialOffset,
        string ObjectName,
        int ClassIndex,
        int SuperIndex,
        int TemplateIndex,
        int OuterIndex,
        string ExportType);
    // Read export Map offsets
    private sealed record ExportMapOffsets(long ClassIndex, long TemplateIndex, long SerialPair);
    // Read serialized f name path
    private sealed record SerializedFNamePath(int Offset)
    {
        public int ByteLength => 16;
    }
}
