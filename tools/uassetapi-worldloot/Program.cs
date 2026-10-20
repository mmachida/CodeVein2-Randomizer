using System.Text.Json;
using System.Text;
using System.Buffers.Binary;
using System.Diagnostics;
using System.Security.Cryptography;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

if (args.Length == 5 && args[0] == "inspect-enemy-drops")
    return EnemyDropPatcher.Inspect(args);

if (args.Length == 4 && args[0] == "scan-blood-codes")
    return BloodCodePatcher.ScanContent(args);

if (args.Length == 5 && args[0] == "inspect-blood-code-article-identities")
    return BloodCodePatcher.InspectArticleIdentities(args);

if (args.Length == 5 && args[0] == "inspect-blood-code-mastery")
    return BloodCodeMasteryPatcher.Inspect(args);

if (args.Length == 5 && args[0] == "patch-blood-code-mastery")
    return BloodCodeMasteryPatcher.Patch(args);

if (args.Length == 5 && args[0] == "inspect-blood-codes")
    return BloodCodePatcher.InspectPackages(args);

if (args.Length == 5 && args[0] == "patch-blood-codes")
    return BloodCodePatcher.PatchPackages(args);

if (args.Length == 5 && args[0] == "inspect-blood-code-references")
    return BloodCodePatcher.InspectBloodCodeReferences(args);

if (args.Length == 5 && args[0] == "patch-blood-code-references")
    return BloodCodePatcher.PatchBloodCodeReferences(args);

if (args.Length == 5 && args[0] == "inspect-blood-code-buddy-settings")
    return BuddyBloodCodePatcher.Inspect(args);

if (args.Length == 5 && args[0] == "patch-blood-code-buddy-settings")
    return BuddyBloodCodePatcher.Patch(args);

if (args.Length == 5 && args[0] == "inspect-bequeathed-formae")
    return BloodCodePatcher.InspectBequeathedFormaPackages(args);

if (args.Length == 5 && args[0] == "patch-bequeathed-formae")
    return BloodCodePatcher.PatchBequeathedFormaPackages(args);

if (args.Length == 5 && args[0] == "inspect-world-loot-quest-articles")
    return BloodCodePatcher.InspectWorldLootQuestPackages(args);

if (args.Length == 5 && args[0] == "patch-world-loot-quest-articles")
    return BloodCodePatcher.PatchWorldLootQuestPackages(args);

if (args.Length == 5 && args[0] == "inspect-boosters")
    return BoosterPatcher.Inspect(args);

if (args.Length == 5 && args[0] == "patch-boosters")
    return BoosterPatcher.Patch(args);

if (args.Length == 5 && args[0] == "patch-booster-slot-unlocks")
    return BoosterSlotUnlockPatcher.Patch(args);

if (args.Length == 5 && args[0] == "patch-disable-tutorials")
    return TutorialFlagPatcher.Patch(args);

if (args.Length == 6 && args[0] == "patch-dialogue-skip-subtitle-widget")
    return DialogueSkipSubtitleWidgetPatcher.Patch(args);
if (args.Length == 5 && args[0] is "patch-motorcycle-unlock-flag" or "patch-motorcycle-unlock-asset")
    return MotorcycleUnlockPatcher.Patch(args);

if (args.Length == 5 && args[0] == "inspect-extractor")
    return ExtractorPatcher.Inspect(args);

if (args.Length == 5 && args[0] == "patch-extractor")
    return ExtractorPatcher.Patch(args);

if (args.Length == 5 && args[0] == "inspect-initial-inventory")
    return InitialInventoryPatcher.Inspect(args);

if (args.Length == 5 && args[0] == "patch-initial-inventory")
    return InitialInventoryPatcher.Patch(args);

if (args.Length == 4 && args[0] == "scan-defensive-gadget-grants")
    return DefensiveGadgetPatcher.ScanContent(args);

if (args.Length == 5 && args[0] == "inspect-defensive-gadget-grants")
    return DefensiveGadgetPatcher.InspectPackages(args);

if (args.Length == 5 && args[0] == "patch-defensive-gadget-grants")
    return DefensiveGadgetPatcher.PatchPackages(args);

if (args.Length == 5 && args[0] == "inspect-raw-article-grant")
    return RawArticleGrantPatcher.Inspect(args);

if (args.Length == 5 && args[0] == "inspect-raw-article-grant-profile")
    return RawArticleGrantPatcher.InspectCompatibilityProfile(args);

if (args.Length == 5 && args[0] == "patch-raw-article-grant")
    return RawArticleGrantPatcher.Patch(args);

if (args.Length == 5 && args[0] == "patch-raw-article-grant-profile")
    return RawArticleGrantPatcher.PatchCompatibilityProfile(args);

if (args.Length == 1 && args[0] == "test-raw-article-grant-fixtures")
    return RawArticleGrantPatcher.TestFixtures();

if (args.Length == 1 && args[0] == "test-healing-fname-fixtures")
    return HealingUpgradePatcher.TestFNameEncodingFixtures();

if (args.Length == 1 && args[0] == "test-healing-export-type-fixtures")
    return HealingUpgradePatcher.TestExportTypeNormalizationFixtures();

if (args.Length == 1 && args[0] == "test-healing-export-type-fixtures")
    return HealingUpgradePatcher.TestExportTypeNormalizationFixtures();

if (args.Length == 5 && args[0] == "prepare-shop-name-map")
    return ShopPatcher.PrepareNameMaps(args);

if (args.Length == 5 && args[0] == "prepare-world-loot-namemaps")
    return WorldLootNameMapPreparer.Prepare(args);

if (args.Length == 5 && args[0] == "patch-enemy-drops")
    return EnemyDropPatcher.Patch(args);

if (args.Length == 5 && args[0] == "inspect-map-batch")
{
    try
    {
        var workRoot = Path.GetFullPath(args[1]);
        var document = JsonSerializer.Deserialize<MapInspectionBatch>(File.ReadAllText(args[2]),
            new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
            ?? throw new InvalidDataException("Map inspection manifest is empty.");
        if (document.Maps is null || document.Maps.Count == 0 || document.Maps.Any(request => request is null))
            throw new InvalidDataException("Map inspection manifest must contain at least one valid map request.");
        var mappings = new Usmap(Path.GetFullPath(args[3]));
        var engineVersion = Enum.Parse<EngineVersion>(args[4], ignoreCase: false);
        var results = new List<MapInspectionResult>();
        foreach (var request in document.Maps)
        {
            results.Add(InspectMapRequest(workRoot, request, engineVersion, mappings));
            // Keep the worker reusable without retaining the previous map's
            // UAsset object graph while the next World Partition map is read.
            GC.Collect(GC.MaxGeneration, GCCollectionMode.Forced, blocking: true, compacting: false);
            GC.WaitForPendingFinalizers();
        }
        Console.WriteLine(JsonSerializer.Serialize(results, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine(exception.ToString());
        return 1;
    }
}

if (args.Length == 5 && args[0] == "find-map-exports")
{
    try
    {
        var workRoot = Path.GetFullPath(args[1]);
        var document = JsonSerializer.Deserialize<MapExportSearchBatch>(File.ReadAllText(args[2]),
            new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
            ?? throw new InvalidDataException("Map export search manifest is empty.");
        if (document.Maps is null || document.Maps.Count == 0 || document.Maps.Any(request => request is null))
            throw new InvalidDataException("Map export search manifest must contain at least one valid map request.");
        var mappings = new Usmap(Path.GetFullPath(args[3]));
        var engineVersion = Enum.Parse<EngineVersion>(args[4], ignoreCase: false);
        var results = new List<MapExportSearchResult>();
        foreach (var request in document.Maps)
        {
            results.Add(FindMapExports(workRoot, request, engineVersion, mappings));
            GC.Collect(GC.MaxGeneration, GCCollectionMode.Forced, blocking: true, compacting: false);
            GC.WaitForPendingFinalizers();
        }
        Console.WriteLine(JsonSerializer.Serialize(results, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine(exception.ToString());
        return 1;
    }
}

if (args.Length == 5 && args[0] == "prepare-map-binary-descriptors")
{
    try
    {
        var workRoot = Path.GetFullPath(args[1]);
        var document = JsonSerializer.Deserialize<BinaryMapDescriptorBatch>(File.ReadAllText(args[2]),
            new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
            ?? throw new InvalidDataException("Binary descriptor manifest is empty.");
        if (document.Maps is null || document.Maps.Count == 0 || document.Maps.Any(request => request is null))
            throw new InvalidDataException("Binary descriptor manifest must contain at least one map.");
        var mappings = new Usmap(Path.GetFullPath(args[3]));
        var engineVersion = Enum.Parse<EngineVersion>(args[4], ignoreCase: false);
        var results = new List<BinaryMapDescriptorResult>();
        foreach (var request in document.Maps)
        {
            results.Add(PrepareMapBinaryDescriptor(workRoot, request, document.CandidateNameValues, engineVersion, mappings));
            // Each map is independent. Release the UAssetAPI graph before reading the next
            // World Partition package in this helper batch.
            GC.Collect(GC.MaxGeneration, GCCollectionMode.Forced, blocking: true, compacting: true);
            GC.WaitForPendingFinalizers();
        }
        Console.WriteLine(JsonSerializer.Serialize(results, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine(exception.ToString());
        return 1;
    }
}

if (args.Length == 5 && args[0] == "patch-packages")
{
    try
    {
        var workRoot = Path.GetFullPath(args[1]);
        var mappingsPath = Path.GetFullPath(args[2]);
        var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
        var document = JsonSerializer.Deserialize<BatchPatchDocument>(File.ReadAllText(args[4]),
            new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
            ?? throw new InvalidDataException("Patch manifest is empty.");
        if (document.Packages.Count == 0)
            throw new InvalidDataException("World Loot patching requires at least one package per helper process.");
        if (!Directory.Exists(workRoot)) throw new InvalidDataException("Work root does not exist.");
        if (!File.Exists(mappingsPath)) throw new FileNotFoundException("Usmap mapping was not found.");
        var mappings = new Usmap(mappingsPath);
        var results = new List<BatchPackageResult>();
        var registeredBlueprintSchemaPackages = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var request in document.Packages)
        {
            results.Add(PatchOnePackage(
                workRoot, request, engineVersion, mappings, registeredBlueprintSchemaPackages));
            // Release each UAssetAPI graph before loading the next map. This keeps
            // bounded memory while amortizing dotnet/helper startup over a small batch.
            GC.Collect(GC.MaxGeneration, GCCollectionMode.Forced, blocking: true, compacting: true);
            GC.WaitForPendingFinalizers();
        }
        Console.WriteLine(JsonSerializer.Serialize(results, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
        return 1;
    }
}

if (args.Length == 5 && args[0] == "patch-healing-upgrades")
    return HealingUpgradePatcher.Run(args);

if (args.Length == 6 && args[0] == "inspect-class")
{
    try
    {
        var workRoot = Path.GetFullPath(args[1]);
        var inputPath = Path.GetFullPath(args[2]);
        if (!IsWithin(inputPath, workRoot)) throw new InvalidDataException("Input package must stay inside the supplied work root.");
        var mappings = new Usmap(Path.GetFullPath(args[3]));
        var engineVersion = Enum.Parse<EngineVersion>(args[4], ignoreCase: false);
        var exportIndex = int.Parse(args[5]) - 1;
        var asset = new UAsset(inputPath, engineVersion, mappings);
        if (exportIndex < 0 || exportIndex >= asset.Exports.Count) throw new ArgumentOutOfRangeException(nameof(exportIndex));
        var export = asset.Exports[exportIndex];
        var classInfo = ResolveClassImport(asset, export.ClassIndex);
        Console.WriteLine(JsonSerializer.Serialize(new ClassInspection(export.ObjectName.ToString(), exportIndex + 1, classInfo.ClassName, classInfo.PackagePath),
            new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
        return 1;
    }
}

if (args.Length == 5 && args[0] == "blueprint-parent")
{
    try
    {
        var workRoot = Path.GetFullPath(args[1]);
        var inputPath = Path.GetFullPath(args[2]);
        var mappings = new Usmap(Path.GetFullPath(args[3]));
        var engineVersion = Enum.Parse<EngineVersion>(args[4], ignoreCase: false);
        var result = InspectBlueprintParent(workRoot, inputPath, engineVersion, mappings);
        Console.WriteLine(JsonSerializer.Serialize(result,
            new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine(exception.ToString());
        return 1;
    }
}

if (args.Length == 6 && args[0] == "inspect-classes")
{
    try
    {
        var workRoot = Path.GetFullPath(args[1]);
        var inputPath = Path.GetFullPath(args[2]);
        if (!IsWithin(inputPath, workRoot)) throw new InvalidDataException("Input package must stay inside the supplied work root.");
        var mappings = new Usmap(Path.GetFullPath(args[3]));
        var engineVersion = Enum.Parse<EngineVersion>(args[4], ignoreCase: false);
        var indices = args[5].Split(',', StringSplitOptions.RemoveEmptyEntries).Select(int.Parse).ToArray();
        var asset = new UAsset(inputPath, engineVersion, mappings);
        var results = new List<ClassInspection>();
        foreach (var oneBasedIndex in indices)
        {
            var exportIndex = oneBasedIndex - 1;
            if (exportIndex < 0 || exportIndex >= asset.Exports.Count) throw new ArgumentOutOfRangeException(nameof(indices));
            var export = asset.Exports[exportIndex];
            var classInfo = ResolveClassImport(asset, export.ClassIndex);
            results.Add(new ClassInspection(export.ObjectName.ToString(), oneBasedIndex, classInfo.ClassName, classInfo.PackagePath));
        }
        Console.WriteLine(JsonSerializer.Serialize(results, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
        return 1;
    }
}

if (args.Length >= 5 && args[0] == "blueprint-parents")
{
    try
    {
        var workRoot = Path.GetFullPath(args[1]);
        var mappings = new Usmap(Path.GetFullPath(args[2]));
        var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
        var results = new List<BlueprintParent>();
        foreach (var rawPath in args.Skip(4))
        {
            var inputPath = Path.GetFullPath(rawPath);
            results.Add(InspectBlueprintParent(workRoot, inputPath, engineVersion, mappings));
            GC.Collect(GC.MaxGeneration, GCCollectionMode.Forced, blocking: true, compacting: false);
            GC.WaitForPendingFinalizers();
        }
        Console.WriteLine(JsonSerializer.Serialize(results, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
        return 1;
    }
}

if (args.Length == 15 && args[0] == "patch")
{
    try
    {
        var workRoot = Path.GetFullPath(args[1]);
        var inputPath = Path.GetFullPath(args[2]);
        var mappingsPath = Path.GetFullPath(args[3]);
        var engineVersion = Enum.Parse<EngineVersion>(args[4], ignoreCase: false);
        var outputPath = Path.GetFullPath(args[5]);
        var patch = new RewardPatchRequest(
            int.Parse(args[6]), int.Parse(args[7]), args[8],
            new RewardTuple(args[9], int.Parse(args[10]), bool.Parse(args[11])),
            new RewardTuple(args[12], int.Parse(args[13]), bool.Parse(args[14])));
        var mappings = new Usmap(mappingsPath);
        var result = PatchOnePackage(workRoot,
            new PackagePatchRequest(inputPath, outputPath, new List<RewardPatchRequest> { patch }),
            engineVersion, mappings);
        Console.WriteLine(JsonSerializer.Serialize(result.Patches.Single(),
            new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
        return 0;
    }
    catch (Exception exception)
    {
        Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
        return 1;
    }
}

if (args.Length != 15 || args[0] != "patch")
{
    Console.Error.WriteLine("Usage: patch <work-root> <input-asset> <mappings.usmap> <VER_UE5_x> <output-asset> <one-based-export-index> <reward-index> <expected-export-name> <expected-path> <expected-count> <expected-notification> <new-path> <new-count> <new-notification>");
    return 2;
}
throw new InvalidOperationException("The legacy patch dispatcher must route through PatchOnePackage.");

static BatchPackageResult PatchOnePackage(
    string workRoot,
    PackagePatchRequest request,
    EngineVersion engineVersion,
    Usmap mappings,
    HashSet<string>? registeredBlueprintSchemaPackages = null)
{
    registeredBlueprintSchemaPackages ??= new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    var packageTimer = Stopwatch.StartNew();
    var inputPath = Path.GetFullPath(request.InputFile);
    var outputPath = Path.GetFullPath(request.OutputFile);
    if (!IsWithin(inputPath, workRoot) || !IsWithin(outputPath, workRoot))
        throw new InvalidDataException("Input and output packages must stay inside the supplied work root.");
    if (!File.Exists(inputPath)) throw new FileNotFoundException("Input package was not found.", inputPath);
    if (inputPath == outputPath || !String.Equals(Path.GetDirectoryName(inputPath), Path.GetDirectoryName(outputPath), StringComparison.OrdinalIgnoreCase))
        throw new InvalidDataException("Output must be a new sibling package so Blueprint dependencies remain resolvable.");
    if (Path.GetExtension(inputPath).ToLowerInvariant() is not (".umap" or ".uasset") ||
        !String.Equals(Path.GetExtension(inputPath), Path.GetExtension(outputPath), StringComparison.OrdinalIgnoreCase))
        throw new InvalidDataException("Input and output must use the same .umap or .uasset extension.");
    if (request.Patches.Count == 0) throw new InvalidDataException("Package patch request must not be empty.");
    foreach (var target in PackageFiles(outputPath))
        if (File.Exists(target)) throw new IOException("Refusing to overwrite an existing output file.");

    var keys = new HashSet<(int ExportIndex, int RewardIndex)>();
    var inputSize = PackageFiles(inputPath)
        .Where(File.Exists)
        .Sum(path => new FileInfo(path).Length);
    var loadTimer = Stopwatch.StartNew();
    UAsset? asset = new UAsset(inputPath, engineVersion, mappings);
    loadTimer.Stop();
    using var process = Process.GetCurrentProcess();
    process.Refresh();
    var loadWorkingSetMiB = process.WorkingSet64 / (1024d * 1024d);
    var loadManagedMiB = GC.GetTotalMemory(false) / (1024d * 1024d);
    var patchTimer = Stopwatch.StartNew();
    var exportNamesAndTypes = asset.Exports.Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();
    var sourceExportTable = asset.Exports.Select(export => new RawExportTableEntry(export.SerialSize, export.SerialOffset)).ToArray();
    var sourceNameMap = SnapshotNameMap(asset);
    var sourceImports = SnapshotImports(asset);
    var validated = new List<(RewardPatchRequest Patch, RewardTuple Original, string SaveId)>();
    using var exportReader = asset.PathToReader(inputPath, true);
    var normalizedPatches = new List<RewardPatchRequest>(request.Patches.Count);
    if (!String.IsNullOrWhiteSpace(request.BlueprintSchemaPackage))
    {
        var schemaPackage = request.BlueprintSchemaPackage;
        ValidateCanonicalPackagePath(schemaPackage, "Blueprint schema");
        var expectedCdo = "Default__" + schemaPackage[(schemaPackage.LastIndexOf('/') + 1)..] + "_C";
        if (request.Patches.Count != 1 || request.Patches[0].ExportIndex != 0 ||
            request.Patches[0].RewardIndex != 0 || request.Patches[0].ExportName != expectedCdo)
            throw new InvalidDataException("Explicit Blueprint schema packages may target only their single Articles[0] CDO reward.");
        var contentRoot = FindContentRoot(workRoot, inputPath);
        var expectedInput = Path.GetFullPath(Path.Combine(
            contentRoot,
            schemaPackage["/Game/".Length..].Replace('/', Path.DirectorySeparatorChar) + ".uasset"));
        if (!String.Equals(expectedInput, inputPath, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Blueprint schema package must match the staged input CDO package.");
        RegisterBlueprintSchemaPackage(
            workRoot, inputPath, schemaPackage, engineVersion, mappings,
            registeredBlueprintSchemaPackages, new HashSet<string>(StringComparer.OrdinalIgnoreCase));
    }
    foreach (var requestedPatch in request.Patches)
    {
        var patch = requestedPatch;
        if (patch.ExportIndex == 0)
        {
            if (String.IsNullOrWhiteSpace(request.BlueprintSchemaPackage))
                throw new InvalidDataException("Export index zero is reserved for an explicitly schema-registered Blueprint CDO.");
            var matches = asset.Exports
                .Select((export, index) => (export, index))
                .Where(item => String.Equals(item.export.ObjectName.ToString(), patch.ExportName, StringComparison.Ordinal))
                .Select(item => item.index)
                .ToArray();
            if (matches.Length != 1)
                throw new InvalidDataException(
                    $"Named Blueprint CDO '{patch.ExportName}' must match exactly one export; found {matches.Length}.");
            patch = patch with { ExportIndex = checked(matches[0] + 1) };
        }
        normalizedPatches.Add(patch);
        var exportIndex = patch.ExportIndex - 1;
        if (exportIndex < 0 || exportIndex >= asset.Exports.Count)
            throw new ArgumentOutOfRangeException(nameof(patch.ExportIndex), "Catalog export index is outside this package.");
        if (patch.RewardIndex < 0) throw new ArgumentOutOfRangeException(nameof(patch.RewardIndex));
        if (!keys.Add((patch.ExportIndex, patch.RewardIndex))) throw new InvalidDataException("Duplicate actor/reward patch request.");
        ValidateTuple(patch.Expected);
        ValidateTuple(patch.Assigned);
        var targetExport = asset.Exports[exportIndex];
        var targetClass = ResolveClassImport(asset, targetExport.ClassIndex);
        if (targetClass.ClassName == "BlueprintGeneratedClass" &&
            targetClass.PackagePath.StartsWith("/Game/", StringComparison.Ordinal))
        {
            RegisterBlueprintSchemaPackage(
                workRoot, inputPath, targetClass.PackagePath, engineVersion, mappings,
                registeredBlueprintSchemaPackages, new HashSet<string>(StringComparer.OrdinalIgnoreCase));
        }
        if (asset.Exports[exportIndex] is not NormalExport)
            asset.ParseExport(exportReader, exportIndex, true);
        if (asset.Exports[exportIndex] is not NormalExport actor)
            throw new InvalidDataException(
                $"Target export {patch.ExportIndex} parsed as {asset.Exports[exportIndex].GetType().Name} " +
                $"for {targetClass.ClassName} at {targetClass.PackagePath}; required Blueprint schemas could not be loaded.");
        if (!String.Equals(actor.ObjectName.ToString(), patch.ExportName, StringComparison.Ordinal))
            throw new InvalidDataException("Catalog export identity does not match the source package.");
        var original = ReadReward(actor, patch.RewardIndex);
        if (!RewardTupleMatches(original, patch.Expected)) throw new InvalidDataException("Source reward tuple no longer matches the catalog; refusing a stale patch.");
        var saveId = patch.RequireUniqueSaveId ? ReadUniqueSaveId(actor) : string.Empty;
        validated.Add((patch, original, saveId));
    }
    var targetRewardsByExport = normalizedPatches.GroupBy(patch => patch.ExportIndex - 1)
        .ToDictionary(group => group.Key, group => (IReadOnlySet<int>)group.Select(patch => patch.RewardIndex).ToHashSet());
    var untouchedSnapshots = targetRewardsByExport.ToDictionary(
        pair => pair.Key,
        pair => ExportDataSnapshotForRewards(asset, pair.Key, pair.Value));
    foreach (var item in validated)
    {
        var actor = asset.Exports[item.Patch.ExportIndex - 1] as NormalExport
            ?? throw new InvalidDataException("Target export is not parsed as NormalExport.");
        WriteReward(asset, actor, item.Patch.RewardIndex, item.Patch.Assigned);
    }
    patchTimer.Stop();

    var outputDirectory = Path.GetDirectoryName(outputPath)!;
    var tempStem = Path.GetFileNameWithoutExtension(outputPath) + ".cv2-" + Guid.NewGuid().ToString("N");
    var temporaryHeader = Path.Combine(outputDirectory, tempStem + Path.GetExtension(outputPath));
    var movedOutputs = new List<string>();
    try
    {
        var serializeTimer = Stopwatch.StartNew();
        asset.Write(temporaryHeader);
        asset = null;
        var rawPreservation = PreserveWorldLootRawPayloads(
            inputPath, temporaryHeader, sourceExportTable, sourceNameMap, sourceImports,
            targetRewardsByExport, validated, engineVersion, mappings);
        serializeTimer.Stop();
        process.Refresh();
        var serializedWorkingSetMiB = process.WorkingSet64 / (1024d * 1024d);
        var serializedManagedBytes = GC.GetTotalMemory(false);
        var serializedManagedMiB = serializedManagedBytes / (1024d * 1024d);
        const long largePackageHeapThreshold = 512L * 1024L * 1024L;
        var collectedLargeSourceGraph = serializedManagedBytes > largePackageHeapThreshold ||
            process.WorkingSet64 > largePackageHeapThreshold;
        if (collectedLargeSourceGraph)
        {
            // UAssetAPI.UAsset is not IDisposable. Before reopening the just-written
            // output, collect a large unreachable source graph so validation does
            // not keep two full package graphs live at once. Small packages skip GC.
            GC.Collect(GC.MaxGeneration, GCCollectionMode.Forced, blocking: true, compacting: true);
            GC.WaitForPendingFinalizers();
        }
        process.Refresh();
        var releasedWorkingSetMiB = process.WorkingSet64 / (1024d * 1024d);
        var releasedManagedMiB = GC.GetTotalMemory(false) / (1024d * 1024d);
        foreach (var (tempPath, finalPath) in PackageFilePairs(temporaryHeader, outputPath))
        {
            if (!File.Exists(tempPath)) continue;
            File.Move(tempPath, finalPath);
            movedOutputs.Add(finalPath);
        }
        var validateTimer = Stopwatch.StartNew();
        var finalReopened = new UAsset(outputPath, engineVersion, mappings);
        ValidateReopenedPackage(finalReopened, exportNamesAndTypes, validated, untouchedSnapshots,
            targetRewardsByExport, sourceNameMap, sourceImports, inputPath, outputPath, sourceExportTable);
        validateTimer.Stop();
        packageTimer.Stop();
        var relativePackage = Path.GetRelativePath(workRoot, inputPath).Replace('\\', '/');
        process.Refresh();
        var workingSetMiB = process.WorkingSet64 / (1024d * 1024d);
        var peakWorkingSetMiB = process.PeakWorkingSet64 / (1024d * 1024d);
        Console.Error.WriteLine(
            $"World Loot package {request.PackageIndex}/{request.PackageCount} {relativePackage} " +
            $"assignments={request.Patches.Count} input={inputSize / (1024d * 1024d):F1} MiB " +
            $"load={loadTimer.Elapsed.TotalSeconds:F2}s patch={patchTimer.Elapsed.TotalSeconds:F2}s " +
            $"serialize={serializeTimer.Elapsed.TotalSeconds:F2}s validate={validateTimer.Elapsed.TotalSeconds:F2}s " +
            $"raw_preserved_exports={rawPreservation.NonTargetExportCount} patched_exports={rawPreservation.TargetExportCount} " +
            $"registered_field_bytes={rawPreservation.RegisteredFieldByteCount} " +
            $"total={packageTimer.Elapsed.TotalSeconds:F2}s load_working_set={loadWorkingSetMiB:F0} MiB " +
            $"load_managed={loadManagedMiB:F0} MiB serialized_working_set={serializedWorkingSetMiB:F0} MiB " +
            $"serialized_managed={serializedManagedMiB:F0} MiB gc_after_serialize={collectedLargeSourceGraph} " +
            $"released_working_set={releasedWorkingSetMiB:F0} MiB released_managed={releasedManagedMiB:F0} MiB " +
            $"working_set={workingSetMiB:F0} MiB " +
            $"peak_working_set={peakWorkingSetMiB:F0} MiB");
        return new BatchPackageResult(inputPath, outputPath,
            validated.Select(item => new PatchResult(item.Patch.ExportName, item.Patch.ExportIndex,
                item.Patch.RewardIndex, item.SaveId, item.Original, item.Patch.Assigned, outputPath, true)).ToList(), true, true);
    }
    catch
    {
        foreach (var path in movedOutputs)
            if (File.Exists(path)) File.Delete(path);
        throw;
    }
    finally
    {
        foreach (var path in PackageFiles(temporaryHeader))
            if (File.Exists(path)) File.Delete(path);
    }
}

static RawPreservationSummary PreserveWorldLootRawPayloads(
    string sourceHeaderPath,
    string serializedHeaderPath,
    RawExportTableEntry[] sourceExportTable,
    string[] sourceNameMap,
    string sourceImports,
    IReadOnlyDictionary<int, IReadOnlySet<int>> targetRewardsByExport,
    List<(RewardPatchRequest Patch, RewardTuple Original, string SaveId)> validated,
    EngineVersion engineVersion,
    Usmap mappings)
{
    var sourceHeaderLength = new FileInfo(sourceHeaderPath).Length;
    var serializedHeader = File.ReadAllBytes(serializedHeaderPath);
    var serializedHeaderLength = serializedHeader.LongLength;
    var sourceSidecarPath = Path.ChangeExtension(sourceHeaderPath, ".uexp");
    var serializedSidecarPath = Path.ChangeExtension(serializedHeaderPath, ".uexp");
    if (!File.Exists(sourceSidecarPath) || !File.Exists(serializedSidecarPath))
        throw new InvalidDataException("World Loot raw-preserving output requires both original and serialized .uexp sidecars.");
    var sourceSidecarLength = new FileInfo(sourceSidecarPath).Length;
    var serializedSidecarLength = new FileInfo(serializedSidecarPath).Length;
    var serializedAsset = new UAsset(serializedHeaderPath, engineVersion, mappings);
    if (serializedAsset.Exports.Count != sourceExportTable.Length)
        throw new InvalidDataException("UAssetAPI changed the export count before raw payload preservation.");

    var serializedNameMap = SnapshotNameMap(serializedAsset);
    ValidateAppendOnlyWorldLootNameMap(sourceNameMap, serializedNameMap, validated);
    if (!String.Equals(sourceImports, SnapshotImports(serializedAsset), StringComparison.Ordinal))
        throw new InvalidDataException("UAssetAPI changed package imports while preparing World Loot FNames.");

    var rewrittenTargetPayloads = new Dictionary<int, byte[]>();
    var changedFieldByteCount = 0;
    foreach (var target in targetRewardsByExport.OrderBy(pair => pair.Key))
    {
        var exportIndex = target.Key;
        if (exportIndex < 0 || exportIndex >= sourceExportTable.Length)
            throw new InvalidDataException("World Loot target export index is outside the source package.");
        var sourceRecord = sourceExportTable[exportIndex];
        var serializedExport = serializedAsset.Exports[exportIndex];
        var serializedRecord = new RawExportTableEntry(serializedExport.SerialSize, serializedExport.SerialOffset);
        if (sourceRecord.SerialSize != serializedRecord.SerialSize)
            throw new InvalidDataException($"World Loot target export #{exportIndex + 1} changed serialized size; refusing a variable-width raw patch.");
        var sourceStart = GetSidecarOffset(sourceRecord, sourceHeaderLength, sourceSidecarLength, exportIndex + 1, "source");
        var serializedStart = GetSidecarOffset(serializedRecord, serializedHeaderLength, serializedSidecarLength, exportIndex + 1, "serialized");
        var sourcePayload = ReadFileRange(sourceSidecarPath, sourceStart, sourceRecord.SerialSize);
        var serializedPayload = ReadFileRange(serializedSidecarPath, serializedStart, serializedRecord.SerialSize);
        if (serializedExport is not NormalExport normal)
            throw new InvalidDataException($"World Loot target export #{exportIndex + 1} no longer parses as NormalExport after serialization.");

        var patchList = validated.Where(item => item.Patch.ExportIndex - 1 == exportIndex)
            .OrderBy(item => item.Patch.RewardIndex).ToArray();
        if (patchList.Length == 0 || patchList.Length != target.Value.Count)
            throw new InvalidDataException("World Loot target export patch inventory is incomplete or inconsistent.");
        var fields = new List<RawFieldRange>();
        var expectedFieldValues = new List<(RewardTuple Before, RewardTuple After, int PathOffset)>();
        foreach (var item in patchList)
        {
            var actual = ReadReward(normal, item.Patch.RewardIndex);
            if (!RewardTupleMatches(actual, item.Patch.Assigned, assigned: true))
                throw new InvalidDataException("Serialized World Loot target does not contain the exact assigned FName identity and reward values.");
            var beforeIdentity = item.Original.SoftObjectPathIdentity
                ?? throw new InvalidDataException("Source ArticleDataPath lacks its exact FName literal/Number identity.");
            var afterIdentity = actual.SoftObjectPathIdentity
                ?? throw new InvalidDataException("Serialized ArticleDataPath lacks its exact FName literal/Number identity.");
            var sourcePathOffset = FindUniqueSoftObjectPath(sourcePayload, sourceNameMap, beforeIdentity,
                $"source export #{exportIndex + 1}, reward #{item.Patch.RewardIndex}");
            var serializedPathOffset = FindUniqueSoftObjectPath(serializedPayload, serializedNameMap, afterIdentity,
                $"serialized export #{exportIndex + 1}, reward #{item.Patch.RewardIndex}");
            if (sourcePathOffset != serializedPathOffset)
                throw new InvalidDataException("UAssetAPI moved a registered World Loot ArticleDataPath within its export; refusing raw field transfer.");
            fields.Add(new RawFieldRange(sourcePathOffset, 16, "ArticleDataPath"));
            expectedFieldValues.Add((item.Original, actual, sourcePathOffset));
        }

        foreach (var (before, after, pathOffset) in expectedFieldValues)
        {
            if (before.Count != after.Count)
            {
                var offset = FindUniqueChangedInt32(sourcePayload, serializedPayload, before.Count, after.Count, fields,
                    $"Count in export #{exportIndex + 1}");
                fields.Add(new RawFieldRange(offset, sizeof(int), "Count"));
            }
        }
        foreach (var (before, after, _) in expectedFieldValues)
        {
            if (before.Notification != after.Notification)
            {
                var offset = FindUniqueChangedBoolean(sourcePayload, serializedPayload, before.Notification, after.Notification, fields,
                    $"bNotification in export #{exportIndex + 1}");
                fields.Add(new RawFieldRange(offset, 1, "bNotification"));
            }
        }
        ValidateDisjointFieldRanges(fields, exportIndex + 1);
        var changedOffsets = ChangedOffsets(sourcePayload, serializedPayload);
        foreach (var offset in changedOffsets)
            if (!fields.Any(field => offset >= field.Offset && offset < field.Offset + field.Length))
                throw new InvalidDataException($"UAssetAPI changed an unregistered byte at target export #{exportIndex + 1}, payload offset {offset}.");

        var preservedPayload = sourcePayload.ToArray();
        foreach (var field in fields)
        {
            serializedPayload.AsSpan(field.Offset, field.Length).CopyTo(preservedPayload.AsSpan(field.Offset, field.Length));
            for (var offset = field.Offset; offset < field.Offset + field.Length; offset++)
                if (sourcePayload[offset] != preservedPayload[offset]) changedFieldByteCount++;
        }
        rewrittenTargetPayloads.Add(exportIndex, preservedPayload);
    }

    // Start with the original sidecar. Only the exact registered Article fields
    // in target exports are written below; every other export stays byte-identical.
    File.Copy(sourceSidecarPath, serializedSidecarPath, overwrite: true);
    using (var stream = new FileStream(serializedSidecarPath, FileMode.Open, FileAccess.Write, FileShare.None))
    {
        foreach (var target in rewrittenTargetPayloads)
        {
            var sourceRecord = sourceExportTable[target.Key];
            var sourceStart = GetSidecarOffset(sourceRecord, sourceHeaderLength, sourceSidecarLength, target.Key + 1, "source");
            stream.Position = sourceStart;
            stream.Write(target.Value);
        }
        stream.Flush(flushToDisk: true);
    }

    var headerDelta = serializedHeaderLength - sourceHeaderLength;
    var rowPatches = new List<(int Offset, byte[] Bytes)>();
    var claimedHeaderRanges = new HashSet<int>();
    for (var index = 0; index < sourceExportTable.Length; index++)
    {
        var sourceRecord = sourceExportTable[index];
        var serializedExport = serializedAsset.Exports[index];
        var sourceStart = GetSidecarOffset(sourceRecord, sourceHeaderLength, sourceSidecarLength, index + 1, "source");
        _ = sourceStart; // Bounds-check the source export table against the immutable .uexp.
        var desiredOffset = checked(sourceRecord.SerialOffset + headerDelta);
        if (desiredOffset < serializedHeaderLength)
            throw new InvalidDataException($"Adjusted World Loot export #{index + 1} offset points inside the package header.");
        var desiredSize = sourceRecord.SerialSize;
        if (serializedExport.SerialSize == desiredSize && serializedExport.SerialOffset == desiredOffset)
            continue;

        var needle = EncodeExportTablePair(serializedExport.SerialSize, serializedExport.SerialOffset);
        var rowOffset = FindUniqueByteSequence(serializedHeader, needle,
            $"serialized ExportMap row #{index + 1}");
        if (!claimedHeaderRanges.Add(rowOffset))
            throw new InvalidDataException("UAssetAPI serialized two ExportMap rows to the same header byte range.");
        rowPatches.Add((rowOffset, EncodeExportTablePair(desiredSize, desiredOffset)));
    }
    foreach (var (offset, bytes) in rowPatches)
        bytes.CopyTo(serializedHeader.AsSpan(offset, bytes.Length));
    File.WriteAllBytes(serializedHeaderPath, serializedHeader);

    foreach (var extension in new[] { ".ubulk", ".uptnl" })
    {
        var source = Path.ChangeExtension(sourceHeaderPath, extension);
        var output = Path.ChangeExtension(serializedHeaderPath, extension);
        if (File.Exists(source)) File.Copy(source, output, overwrite: true);
        else if (File.Exists(output)) File.Delete(output);
    }

    return new RawPreservationSummary(
        sourceExportTable.Length - targetRewardsByExport.Count,
        targetRewardsByExport.Count,
        changedFieldByteCount);
}

static void ValidateReopenedPackage(UAsset reopened,
    (string Name, string? Type)[] expectedExports,
    List<(RewardPatchRequest Patch, RewardTuple Original, string SaveId)> validated,
    Dictionary<int, string> untouchedSnapshots,
    IReadOnlyDictionary<int, IReadOnlySet<int>> targetRewardsByExport,
    string[] sourceNameMap,
    string sourceImports,
    string sourceHeaderPath,
    string outputHeaderPath,
    RawExportTableEntry[] sourceExportTable)
{
    if (reopened.Exports.Count != expectedExports.Length)
        throw new InvalidDataException("Reopened package export count changed.");
    var actualExports = reopened.Exports.Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();
    if (!expectedExports.SequenceEqual(actualExports))
        throw new InvalidDataException("Reopened package changed export names, types, or order.");
    if (!String.Equals(sourceImports, SnapshotImports(reopened), StringComparison.Ordinal))
        throw new InvalidDataException("Reopened package changed imports while patching World Loot.");
    ValidateAppendOnlyWorldLootNameMap(sourceNameMap, SnapshotNameMap(reopened), validated);
    foreach (var item in validated)
    {
        var actor = reopened.Exports[item.Patch.ExportIndex - 1] as NormalExport
            ?? throw new InvalidDataException("Reopened target export is not parseable.");
        if (!String.Equals(actor.ObjectName.ToString(), item.Patch.ExportName, StringComparison.Ordinal) ||
            !RewardTupleMatches(ReadReward(actor, item.Patch.RewardIndex), item.Patch.Assigned, assigned: true) ||
            (item.Patch.RequireUniqueSaveId && ReadUniqueSaveId(actor) != item.SaveId))
            throw new InvalidDataException("Reopened target identity, reward tuple, or required UniqueSaveID did not validate.");
    }
    var actualSnapshots = ExportDataSnapshots(reopened, targetRewardsByExport);
    foreach (var snapshot in untouchedSnapshots)
        if (!actualSnapshots.TryGetValue(snapshot.Key, out var actual) || actual != snapshot.Value)
            throw new InvalidDataException("Export data outside the assigned reward tuples changed.");
    ValidateRawNonTargetExports(sourceHeaderPath, outputHeaderPath, reopened,
        sourceExportTable, targetRewardsByExport.Keys.ToHashSet());
}

static void ValidateRawNonTargetExports(
    string sourceHeaderPath,
    string outputHeaderPath,
    UAsset reopened,
    RawExportTableEntry[] sourceExportTable,
    IReadOnlySet<int> targetExportIndices)
{
    if (reopened.Exports.Count != sourceExportTable.Length)
        throw new InvalidDataException("Raw preservation validation found a changed export count.");
    var sourceHeaderLength = new FileInfo(sourceHeaderPath).Length;
    var outputHeaderLength = new FileInfo(outputHeaderPath).Length;
    var sourceSidecar = Path.ChangeExtension(sourceHeaderPath, ".uexp");
    var outputSidecar = Path.ChangeExtension(outputHeaderPath, ".uexp");
    if (!File.Exists(sourceSidecar) || !File.Exists(outputSidecar))
        throw new InvalidDataException("Raw preservation validation could not reopen both .uexp sidecars.");
    using var sourceStream = new FileStream(sourceSidecar, FileMode.Open, FileAccess.Read, FileShare.Read);
    using var outputStream = new FileStream(outputSidecar, FileMode.Open, FileAccess.Read, FileShare.Read);
    var sourceBuffer = new byte[64 * 1024];
    var outputBuffer = new byte[sourceBuffer.Length];
    for (var index = 0; index < sourceExportTable.Length; index++)
    {
        if (targetExportIndices.Contains(index)) continue;
        var sourceRecord = sourceExportTable[index];
        var outputExport = reopened.Exports[index];
        if (outputExport.SerialSize != sourceRecord.SerialSize)
            throw new InvalidDataException($"Untouched export #{index + 1} serial size differs from the source package.");
        var sourceStart = GetSidecarOffset(sourceRecord, sourceHeaderLength, sourceStream.Length, index + 1, "source");
        var outputRecord = new RawExportTableEntry(outputExport.SerialSize, outputExport.SerialOffset);
        var outputStart = GetSidecarOffset(outputRecord, outputHeaderLength, outputStream.Length, index + 1, "output");
        sourceStream.Position = sourceStart;
        outputStream.Position = outputStart;
        var remaining = sourceRecord.SerialSize;
        while (remaining > 0)
        {
            var count = checked((int)Math.Min(remaining, sourceBuffer.Length));
            sourceStream.ReadExactly(sourceBuffer.AsSpan(0, count));
            outputStream.ReadExactly(outputBuffer.AsSpan(0, count));
            if (!sourceBuffer.AsSpan(0, count).SequenceEqual(outputBuffer.AsSpan(0, count)))
                throw new InvalidDataException($"Untouched export #{index + 1} payload differs byte-for-byte from the source package.");
            remaining -= count;
        }
    }
}

static int GetSidecarOffset(RawExportTableEntry record, long headerLength, long sidecarLength, int exportIndex, string label)
{
    if (record.SerialOffset < headerLength || record.SerialSize < 0)
        throw new InvalidDataException($"{label} export #{exportIndex} does not point to a valid .uexp payload.");
    var start = checked(record.SerialOffset - headerLength);
    if (start > sidecarLength || record.SerialSize > sidecarLength - start || start > Int32.MaxValue)
        throw new InvalidDataException($"{label} export #{exportIndex} payload is outside its .uexp sidecar.");
    return checked((int)start);
}

static byte[] ReadFileRange(string path, int offset, long length)
{
    if (length < 0 || length > Int32.MaxValue) throw new InvalidDataException("World Loot export payload exceeds the supported raw-patch size.");
    var result = new byte[(int)length];
    using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
    stream.Position = offset;
    stream.ReadExactly(result);
    return result;
}

static int FindUniqueSoftObjectPath(byte[] payload, string[] nameMap, SoftObjectPathIdentityDto identity, string label)
{
    var matches = new List<int>();
    for (var offset = 0; offset <= payload.Length - 16; offset++)
    {
        var packageIndex = BinaryPrimitives.ReadInt32LittleEndian(payload.AsSpan(offset, 4));
        var packageNumber = BinaryPrimitives.ReadInt32LittleEndian(payload.AsSpan(offset + 4, 4));
        var assetIndex = BinaryPrimitives.ReadInt32LittleEndian(payload.AsSpan(offset + 8, 4));
        var assetNumber = BinaryPrimitives.ReadInt32LittleEndian(payload.AsSpan(offset + 12, 4));
        if (packageIndex < 0 || packageIndex >= nameMap.Length || assetIndex < 0 || assetIndex >= nameMap.Length)
            continue;
        if (packageNumber == identity.PackageName.Number && assetNumber == identity.AssetName.Number &&
            String.Equals(nameMap[packageIndex], identity.PackageName.Value, StringComparison.Ordinal) &&
            String.Equals(nameMap[assetIndex], identity.AssetName.Value, StringComparison.Ordinal))
            matches.Add(offset);
    }
    if (matches.Count != 1)
        throw new InvalidDataException($"{label} ArticleDataPath FName tuple is not unique in the raw export ({matches.Count} matches).");
    return matches[0];
}

static int FindUniqueChangedInt32(
    byte[] before, byte[] after, int oldValue, int newValue, IReadOnlyCollection<RawFieldRange> excluded, string label)
{
    var candidates = new List<int>();
    for (var offset = 0; offset <= before.Length - sizeof(int); offset++)
    {
        if (OverlapsAny(offset, sizeof(int), excluded)) continue;
        if (BinaryPrimitives.ReadInt32LittleEndian(before.AsSpan(offset, sizeof(int))) != oldValue ||
            BinaryPrimitives.ReadInt32LittleEndian(after.AsSpan(offset, sizeof(int))) != newValue)
            continue;
        if (before.AsSpan(offset, sizeof(int)).SequenceEqual(after.AsSpan(offset, sizeof(int)))) continue;
        candidates.Add(offset);
    }
    if (candidates.Count != 1)
        throw new InvalidDataException($"Could not identify a unique changed {label} raw range ({candidates.Count} matches).");
    return candidates[0];
}

static int FindUniqueChangedBoolean(
    byte[] before, byte[] after, bool oldValue, bool newValue, IReadOnlyCollection<RawFieldRange> excluded, string label)
{
    var oldByte = oldValue ? (byte)1 : (byte)0;
    var newByte = newValue ? (byte)1 : (byte)0;
    var candidates = Enumerable.Range(0, before.Length)
        .Where(offset => !OverlapsAny(offset, 1, excluded) && before[offset] == oldByte && after[offset] == newByte && before[offset] != after[offset])
        .ToArray();
    if (candidates.Length != 1)
        throw new InvalidDataException($"Could not identify a unique changed {label} raw byte ({candidates.Length} matches).");
    return candidates[0];
}

static bool OverlapsAny(int offset, int length, IEnumerable<RawFieldRange> ranges) =>
    ranges.Any(range => offset < range.Offset + range.Length && range.Offset < offset + length);

static int[] ChangedOffsets(byte[] before, byte[] after)
{
    if (before.Length != after.Length) throw new InvalidDataException("World Loot target export changed serialized size.");
    return Enumerable.Range(0, before.Length).Where(offset => before[offset] != after[offset]).ToArray();
}

static void ValidateDisjointFieldRanges(IReadOnlyList<RawFieldRange> fields, int exportIndex)
{
    var ordered = fields.OrderBy(field => field.Offset).ToArray();
    for (var index = 1; index < ordered.Length; index++)
        if (ordered[index - 1].Offset + ordered[index - 1].Length > ordered[index].Offset)
            throw new InvalidDataException($"Registered World Loot field ranges overlap in export #{exportIndex}.");
}

static byte[] EncodeExportTablePair(long serialSize, long serialOffset)
{
    var result = new byte[16];
    BinaryPrimitives.WriteInt64LittleEndian(result.AsSpan(0, 8), serialSize);
    BinaryPrimitives.WriteInt64LittleEndian(result.AsSpan(8, 8), serialOffset);
    return result;
}

static int FindUniqueByteSequence(byte[] haystack, byte[] needle, string label)
{
    var first = haystack.AsSpan().IndexOf(needle);
    if (first < 0 || haystack.AsSpan(first + 1).IndexOf(needle) >= 0)
        throw new InvalidDataException($"Could not uniquely locate {label} in the serialized package header.");
    return first;
}

static string[] SnapshotNameMap(UAsset asset) =>
    asset.GetNameMapIndexList().Select((_, index) => asset.GetNameReference(index).ToString()).ToArray();

static string SnapshotImports(UAsset asset) => String.Join("\u0000", asset.Imports.Select(item =>
    $"{item.ClassPackage}\u001f{item.ClassName}\u001f{item.ObjectName}\u001f{item.OuterIndex.Index}"));

static void ValidateAppendOnlyWorldLootNameMap(
    string[] sourceNames,
    string[] outputNames,
    IEnumerable<(RewardPatchRequest Patch, RewardTuple Original, string SaveId)> patches)
{
    if (outputNames.Length < sourceNames.Length || !sourceNames.SequenceEqual(outputNames.Take(sourceNames.Length)))
        throw new InvalidDataException("World Loot serialization changed the existing NameMap entries or their indices.");
    var allowedAddedNames = patches.SelectMany(item =>
    {
        var tuple = item.Patch.Assigned;
        var identity = tuple.SoftObjectPathIdentity ?? new SoftObjectPathIdentityDto(
            new FNameIdentityDto(tuple.Path, 0),
            new FNameIdentityDto(tuple.Path[(tuple.Path.LastIndexOf('/') + 1)..], 0));
        return new[] { identity.PackageName.Value, identity.AssetName.Value };
    }).ToHashSet(StringComparer.Ordinal);
    var addedNames = outputNames.Skip(sourceNames.Length);
    if (addedNames.Any(name => !allowedAddedNames.Contains(name)))
        throw new InvalidDataException("World Loot serialization added a NameMap value not required by an assigned ArticleDataPath.");
}

static bool IsWithin(string path, string root)
{
    var relative = Path.GetRelativePath(root, path);
    return relative != "." && relative != ".." &&
        !relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) &&
        !Path.IsPathRooted(relative);
}

static void ValidateTuple(RewardTuple tuple)
{
    var path = tuple.Path;
    var parts = path.Split('/', StringSplitOptions.RemoveEmptyEntries);
    if (!path.StartsWith("/Game/", StringComparison.Ordinal) ||
        path.Contains('\\') || parts.Length < 3 ||
        parts.Any(part => part is "." or "..") ||
        !String.Equals(parts[^1], Path.GetFileName(path), StringComparison.Ordinal))
        throw new InvalidDataException("Reward paths must be package paths under /Game/ without a file extension.");
    if (tuple.Count <= 0) throw new InvalidDataException("Reward count must be positive.");
    if (tuple.SoftObjectPathIdentity is { } identity &&
        (ResolveFName(identity.PackageName) != tuple.Path ||
         ResolveFName(identity.AssetName) != parts[^1]))
        throw new InvalidDataException("Soft object path FName identities do not resolve to the requested Article path.");
}

static string ResolveFName(FNameIdentityDto name) =>
    name.Number == 0 ? name.Value : $"{name.Value}_{name.Number - 1}";

static bool RewardTupleMatches(RewardTuple actual, RewardTuple expected, bool assigned = false)
{
    if (actual.Path != expected.Path || actual.Count != expected.Count || actual.Notification != expected.Notification)
        return false;
    var expectedIdentity = expected.SoftObjectPathIdentity;
    if (expectedIdentity is null && !assigned) return true;
    if (expectedIdentity is null)
    {
        var parts = expected.Path.Split('/', StringSplitOptions.RemoveEmptyEntries);
        var assetName = parts[^1];
        expectedIdentity = new SoftObjectPathIdentityDto(
            new FNameIdentityDto(expected.Path, 0), new FNameIdentityDto(assetName, 0));
    }
    return actual.SoftObjectPathIdentity == expectedIdentity;
}

static IEnumerable<string> PackageFiles(string headerPath)
{
    yield return headerPath;
    foreach (var extension in new[] { ".uexp", ".ubulk", ".uptnl" })
        yield return Path.ChangeExtension(headerPath, extension);
}

static IEnumerable<(string Temp, string Final)> PackageFilePairs(string tempHeader, string finalHeader)
{
    yield return (tempHeader, finalHeader);
    foreach (var extension in new[] { ".uexp", ".ubulk", ".uptnl" })
        yield return (Path.ChangeExtension(tempHeader, extension), Path.ChangeExtension(finalHeader, extension));
}

static RewardTuple ReadReward(NormalExport actor, int rewardIndex)
{
    var articles = actor.Data.OfType<StructPropertyData>()
        .Single(property => property.Name.ToString() == "ArticleBulkInfo")
        .Value.OfType<ArrayPropertyData>()
        .Single(property => property.Name.ToString() == "Articles");
    if (rewardIndex >= articles.Value.Count())
        throw new ArgumentOutOfRangeException(nameof(rewardIndex), "Catalog reward index is outside the Articles array.");
    var article = articles.Value[rewardIndex] as StructPropertyData
        ?? throw new InvalidDataException("Reward entry is not a struct.");
    var path = (FSoftObjectPath)article.Value.OfType<SoftObjectPropertyData>()
        .Single(property => property.Name.ToString() == "ArticleDataPath").Value!;
    var packageName = path.AssetPath.PackageName;
    var assetNameValue = path.AssetPath.AssetName;
    var package = ResolveFName(new FNameIdentityDto(packageName.Value.ToString(), packageName.Number));
    var assetName = ResolveFName(new FNameIdentityDto(assetNameValue.Value.ToString(), assetNameValue.Number));
    if (!String.Equals(assetName, package[(package.LastIndexOf('/') + 1)..], StringComparison.Ordinal))
        throw new InvalidDataException("Reward soft path uses an unexpected subobject or asset name.");
    var count = article.Value.OfType<IntPropertyData>()
        .Single(property => property.Name.ToString() == "Count").Value;
    var notification = article.Value.OfType<BoolPropertyData>()
        .Single(property => property.Name.ToString() == "bNotification").Value;
    return new RewardTuple(package, count, notification,
        new SoftObjectPathIdentityDto(
            new FNameIdentityDto(packageName.Value.ToString(), packageName.Number),
            new FNameIdentityDto(assetNameValue.Value.ToString(), assetNameValue.Number)));
}

static void WriteReward(UAsset asset, NormalExport actor, int rewardIndex, RewardTuple tuple)
{
    var parts = tuple.Path.Split('/', StringSplitOptions.RemoveEmptyEntries);
    var identity = tuple.SoftObjectPathIdentity ?? new SoftObjectPathIdentityDto(
        new FNameIdentityDto(tuple.Path, 0), new FNameIdentityDto(parts[^1], 0));
    var articles = actor.Data.OfType<StructPropertyData>()
        .Single(property => property.Name.ToString() == "ArticleBulkInfo")
        .Value.OfType<ArrayPropertyData>()
        .Single(property => property.Name.ToString() == "Articles");
    var article = articles.Value[rewardIndex] as StructPropertyData
        ?? throw new InvalidDataException("Reward entry is not a struct.");
    var pathProperty = article.Value.OfType<SoftObjectPropertyData>()
        .Single(property => property.Name.ToString() == "ArticleDataPath");
    var pathValue = (FSoftObjectPath)pathProperty.Value!;
    pathValue.AssetPath = new FTopLevelAssetPath
    {
        PackageName = new FName(asset, identity.PackageName.Value, identity.PackageName.Number),
        AssetName = new FName(asset, identity.AssetName.Value, identity.AssetName.Number)
    };
    pathProperty.Value = pathValue;
    article.Value.OfType<IntPropertyData>()
        .Single(property => property.Name.ToString() == "Count").Value = tuple.Count;
    article.Value.OfType<BoolPropertyData>()
        .Single(property => property.Name.ToString() == "bNotification").Value = tuple.Notification;
}

static string ReadUniqueSaveId(NormalExport actor)
{
    var id = actor.Data.OfType<StructPropertyData>()
        .Single(property => property.Name.ToString() == "UniqueSaveID")
        .Value.OfType<GuidPropertyData>()
        .Single().Value;
    return id.ToString();
}

static string ExportDataSnapshotForRewards(UAsset asset, int targetExportIndex, IReadOnlySet<int> targetRewardIndices) =>
    ExportDataSnapshots(asset, new Dictionary<int, IReadOnlySet<int>>
    {
        [targetExportIndex] = targetRewardIndices
    })[targetExportIndex];

static Dictionary<int, string> ExportDataSnapshots(
    UAsset asset,
    IReadOnlyDictionary<int, IReadOnlySet<int>> targetRewardsByExport)
{
    var snapshots = new Dictionary<int, string>();
    foreach (var target in targetRewardsByExport)
    {
        var targetExportIndex = target.Key;
        if (targetExportIndex < 0 || targetExportIndex >= asset.Exports.Count)
            throw new ArgumentOutOfRangeException(nameof(targetRewardsByExport));
        using var document = JsonDocument.Parse(asset.SerializeJsonObject(asset.Exports[targetExportIndex], false));
        var export = document.RootElement;
        var snapshot = new StringBuilder();
        if (export.TryGetProperty("ObjectName", out var objectName))
            snapshot.Append(targetExportIndex).Append('|').Append(objectName.GetRawText()).Append('|');
        if (!export.TryGetProperty("Data", out var data) || data.ValueKind != JsonValueKind.Array)
        {
            snapshot.Append("<no-data>\n");
            snapshots.Add(targetExportIndex, SnapshotDigest(snapshot));
            continue;
        }
        foreach (var property in data.EnumerateArray())
        {
            if (!property.TryGetProperty("Name", out var name) || name.GetString() != "ArticleBulkInfo")
                snapshot.Append(property.GetRawText()).Append('\0');
            else
                snapshot.Append(ArticleBulkSnapshot(property, target.Value)).Append('\0');
        }
        snapshot.Append('\n');
        snapshots.Add(targetExportIndex, SnapshotDigest(snapshot));
    }
    return snapshots;
}

// Retain compact digests rather than full serialized export snapshots between
// the source and reopened-output comparisons.
static string SnapshotDigest(StringBuilder snapshot) =>
    Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(snapshot.ToString())));

static string ArticleBulkSnapshot(JsonElement bulk, IReadOnlySet<int> targetRewardIndices)
{
    var snapshot = new StringBuilder();
    foreach (var bulkField in bulk.EnumerateObject())
    {
        if (bulkField.Name != "Value")
        {
            snapshot.Append(bulkField.Name).Append(':').Append(bulkField.Value.GetRawText()).Append('\0');
            continue;
        }
        foreach (var field in bulkField.Value.EnumerateArray())
        {
            if (!field.TryGetProperty("Name", out var name) || name.GetString() != "Articles")
            {
                snapshot.Append(field.GetRawText()).Append('\0');
                continue;
            }
            foreach (var arrayField in field.EnumerateObject())
            {
                if (arrayField.Name != "Value")
                {
                    snapshot.Append(arrayField.Name).Append(':').Append(arrayField.Value.GetRawText()).Append('\0');
                    continue;
                }
                var rewards = arrayField.Value;
                for (var rewardIndex = 0; rewardIndex < rewards.GetArrayLength(); rewardIndex++)
                {
                    var reward = rewards[rewardIndex];
                    if (!targetRewardIndices.Contains(rewardIndex))
                    {
                        snapshot.Append(reward.GetRawText()).Append('\0');
                        continue;
                    }
                    foreach (var rewardField in reward.EnumerateObject())
                    {
                        if (rewardField.Name != "Value")
                        {
                            snapshot.Append(rewardField.Name).Append(':').Append(rewardField.Value.GetRawText()).Append('\0');
                            continue;
                        }
                        foreach (var rewardProperty in rewardField.Value.EnumerateArray())
                        {
                            var propertyName = rewardProperty.TryGetProperty("Name", out var rewardName)
                                ? rewardName.GetString()
                                : null;
                            if (propertyName is not ("ArticleDataPath" or "Count" or "bNotification"))
                                snapshot.Append(rewardProperty.GetRawText()).Append('\0');
                        }
                    }
                }
            }
        }
    }
    return snapshot.ToString();
}


static bool? ReadBlueprintResetFlag(UAsset asset, ClassExport classExport)
{
    var defaultName = "Default__" + classExport.ObjectName.ToString();
    var defaultObject = asset.Exports.OfType<NormalExport>()
        .FirstOrDefault(export => export.ObjectName.ToString() == defaultName);
    return defaultObject?.Data.OfType<BoolPropertyData>()
        .FirstOrDefault(property => property.Name.ToString() == "bResetOnYadorigiRest")?.Value;
}

static HelperMemoryResult CaptureHelperMemory(long elapsedMilliseconds)
{
    using var process = Process.GetCurrentProcess();
    process.Refresh();
    return new HelperMemoryResult(
        process.WorkingSet64,
        process.PrivateMemorySize64,
        process.PeakWorkingSet64,
        GC.GetTotalMemory(forceFullCollection: false),
        GC.GetGCMemoryInfo().HeapSizeBytes,
        elapsedMilliseconds);
}

static BinaryMapDescriptorResult PrepareMapBinaryDescriptor(
    string workRoot,
    BinaryMapDescriptorRequest request,
    IReadOnlyList<string> candidateNameValues,
    EngineVersion engineVersion,
    Usmap mappings)
{
    var inputPath = Path.GetFullPath(request.InputFile);
    if (!IsWithin(inputPath, workRoot))
        throw new InvalidDataException("Binary descriptor input map must stay inside the supplied work root.");
    if (String.IsNullOrWhiteSpace(request.PackagePath) || !request.PackagePath.StartsWith("/Game/", StringComparison.Ordinal))
        throw new InvalidDataException("Binary descriptor package identity must be a /Game path.");
    var headerSize = new FileInfo(inputPath).Length;
    var sidecarPath = Path.ChangeExtension(inputPath, ".uexp");
    var headerHash = HashFile(inputPath);
    var sidecarSize = File.Exists(sidecarPath) ? new FileInfo(sidecarPath).Length : (long?)null;
    var sidecarHash = File.Exists(sidecarPath) ? HashFile(sidecarPath) : null;
    var fields = new List<BinaryRewardFieldDescriptor>();
    var classes = new List<ClassInspection>();
    var nameMapIndices = new Dictionary<string, int>(StringComparer.Ordinal);
    UAsset asset;
    try
    {
        asset = new UAsset(inputPath, engineVersion, mappings);
    }
    catch (Exception exception)
    {
        fields.AddRange(request.Rewards.Select(fieldRequest => new BinaryRewardFieldDescriptor(
            fieldRequest.ExportIndex, fieldRequest.RewardIndex, fieldRequest.ExportName,
            null, null, null, false, $"{exception.GetType().Name}: {exception.Message}")));
        return new BinaryMapDescriptorResult(
            request.PackagePath, inputPath, 1, headerHash, sidecarHash, headerSize, sidecarSize,
            nameMapIndices, fields, "UAssetAPI could not parse this map; use structural fallback.", classes);
    }
    foreach (var oneBasedIndex in (request.ClassExportIndices ?? new List<int>()).Distinct().Order())
    {
        if (oneBasedIndex < 1 || oneBasedIndex > asset.Exports.Count)
            throw new ArgumentOutOfRangeException(nameof(request.ClassExportIndices));
        var export = asset.Exports[oneBasedIndex - 1];
        var classInfo = ResolveClassImport(asset, export.ClassIndex);
        classes.Add(new ClassInspection(export.ObjectName.ToString(), oneBasedIndex, classInfo.ClassName, classInfo.PackagePath));
    }
    if (!File.Exists(sidecarPath))
    {
        fields.AddRange(request.Rewards.Select(field => new BinaryRewardFieldDescriptor(
            field.ExportIndex, field.RewardIndex, field.ExportName, null, null,
            null, false, "Source package has no .uexp sidecar.")));
        return new BinaryMapDescriptorResult(request.PackagePath, inputPath, 1, headerHash, null,
            headerSize, null, nameMapIndices, fields, "Source package has no .uexp sidecar.", classes);
    }
    var sourceSidecarSize = sidecarSize!.Value;
    var nameMap = SnapshotNameMap(asset);
    foreach (var value in candidateNameValues.Distinct(StringComparer.Ordinal))
    {
        var found = Array.FindIndex(nameMap, name => String.Equals(name, value, StringComparison.Ordinal));
        if (found < 0) continue;
        if (Array.FindIndex(nameMap, found + 1, name => String.Equals(name, value, StringComparison.Ordinal)) >= 0)
            continue; // Ambiguous NameMap literals force structural fallback.
        nameMapIndices[value] = found;
    }

    foreach (var fieldRequest in request.Rewards)
    {
        try
        {
            if (fieldRequest.ExportIndex < 1 || fieldRequest.ExportIndex > asset.Exports.Count)
                throw new InvalidDataException("Catalog export index is outside the package.");
            var record = asset.Exports[fieldRequest.ExportIndex - 1];
            if (!String.Equals(record.ObjectName.ToString(), fieldRequest.ExportName, StringComparison.Ordinal))
                throw new InvalidDataException("Catalog export name differs from the parsed export.");
            ValidateTuple(fieldRequest.Expected);
            SoftObjectPathIdentityDto identity;
            if (record is NormalExport actor)
            {
                var actual = ReadReward(actor, fieldRequest.RewardIndex);
                if (!RewardTupleMatches(actual, fieldRequest.Expected))
                    throw new InvalidDataException(
                        $"Catalog reward tuple differs from parsed source: actual=({actual.Path}, {actual.Count}, {actual.Notification}, {actual.SoftObjectPathIdentity}), " +
                        $"expected=({fieldRequest.Expected.Path}, {fieldRequest.Expected.Count}, {fieldRequest.Expected.Notification}, {fieldRequest.Expected.SoftObjectPathIdentity}).");
                identity = actual.SoftObjectPathIdentity
                    ?? throw new InvalidDataException("Parsed ArticleDataPath has no exact FName identity.");
            }
            else if (record is RawExport)
            {
                // Some World Loot actors are opaque to UAssetAPI. Accept them only when
                // the catalog's exact ArticleDataPath FName identity occurs once in the
                // untouched export payload; the binary patcher will later replace only
                // that fixed-width 16-byte value. Count/notification stay catalog-bound
                // and are never edited on this raw-export path.
                identity = fieldRequest.Expected.SoftObjectPathIdentity
                    ?? throw new InvalidDataException("Raw target export has no exact catalogued ArticleDataPath FName identity.");
            }
            else
            {
                throw new InvalidDataException($"Target export parsed as {record.GetType().Name}, not NormalExport or RawExport.");
            }
            var recordLayout = new RawExportTableEntry(record.SerialSize, record.SerialOffset);
            var sidecarStart = GetSidecarOffset(recordLayout, headerSize, sourceSidecarSize,
                fieldRequest.ExportIndex, "source");
            var payload = ReadFileRange(sidecarPath, sidecarStart, record.SerialSize);
            var fieldOffset = FindUniqueSoftObjectPath(payload, nameMap, identity,
                $"Extract export #{fieldRequest.ExportIndex}, reward #{fieldRequest.RewardIndex}");
            fields.Add(new BinaryRewardFieldDescriptor(
                fieldRequest.ExportIndex,
                fieldRequest.RewardIndex,
                fieldRequest.ExportName,
                checked(sidecarStart + fieldOffset),
                Convert.ToHexString(payload.AsSpan(fieldOffset, 16)),
                identity,
                true,
                null));
        }
        catch (Exception exception)
        {
            fields.Add(new BinaryRewardFieldDescriptor(
                fieldRequest.ExportIndex,
                fieldRequest.RewardIndex,
                fieldRequest.ExportName,
                null,
                null,
                null,
                false,
                $"{exception.GetType().Name}: {exception.Message}"));
        }
    }
    return new BinaryMapDescriptorResult(
        request.PackagePath, inputPath, 1, headerHash, sidecarHash, headerSize, sidecarSize,
        nameMapIndices, fields, null, classes);
}

static string HashFile(string path)
{
    using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
    return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
}

static void RegisterBlueprintSchemaPackage(
    string workRoot,
    string referringPackagePath,
    string blueprintPackagePath,
    EngineVersion engineVersion,
    Usmap mappings,
    HashSet<string> registeredPackages,
    HashSet<string> activePackages)
{
    if (!blueprintPackagePath.StartsWith("/Game/", StringComparison.Ordinal))
        throw new InvalidDataException($"Blueprint schema package is outside /Game/: {blueprintPackagePath}.");

    var packageKey = blueprintPackagePath.TrimEnd('/');
    if (registeredPackages.Contains(packageKey)) return;
    if (!activePackages.Add(packageKey))
        throw new InvalidDataException($"Blueprint schema inheritance contains a cycle at {blueprintPackagePath}.");

    try
    {
        var contentRoot = FindContentRoot(workRoot, referringPackagePath);
        var relativePackage = packageKey["/Game/".Length..]
            .Replace('/', Path.DirectorySeparatorChar);
        var schemaPath = Path.GetFullPath(Path.Combine(contentRoot, relativePackage + ".uasset"));
        if (!IsWithin(schemaPath, contentRoot))
            throw new InvalidDataException($"Blueprint schema package escaped the staged Content directory: {blueprintPackagePath}.");
        if (!File.Exists(schemaPath))
            throw new FileNotFoundException(
                $"Required Blueprint schema package {blueprintPackagePath} was not staged beside the World Loot map.",
                schemaPath);

        var flags = CustomSerializationFlags.SkipParsingExports |
                    CustomSerializationFlags.SkipPreloadDependencyLoading;
        var schemaAsset = new UAsset(schemaPath, true, engineVersion, mappings, flags);
        using var schemaReader = schemaAsset.PathToReader(schemaPath, true);
        var foundClass = false;
        for (var index = 0; index < schemaAsset.Exports.Count; index++)
        {
            if (schemaAsset.Exports[index].GetExportClassType().ToString() != "BlueprintGeneratedClass")
                continue;
            schemaAsset.ParseExport(schemaReader, index, true);
            if (schemaAsset.Exports[index] is not ClassExport classExport)
                throw new InvalidDataException(
                    $"Blueprint class schema export {index + 1} in {blueprintPackagePath} could not be parsed as ClassExport.");

            foundClass = true;
            var parent = ResolveClassImport(schemaAsset, classExport.SuperStruct);
            if (parent.ClassName == "BlueprintGeneratedClass" &&
                parent.PackagePath.StartsWith("/Game/", StringComparison.Ordinal))
            {
                RegisterBlueprintSchemaPackage(
                    workRoot, schemaPath, parent.PackagePath, engineVersion, mappings,
                    registeredPackages, activePackages);
            }

            mappings.Schemas[classExport.ObjectName.ToString()] =
                Usmap.GetSchemaFromStructExport(classExport, mappings.AreFNamesCaseInsensitive);
        }
        if (!foundClass)
            throw new InvalidDataException($"Blueprint schema package {blueprintPackagePath} has no BlueprintGeneratedClass export.");

        registeredPackages.Add(packageKey);
    }
    finally
    {
        activePackages.Remove(packageKey);
    }
}

static void ValidateCanonicalPackagePath(string value, string label)
{
    var parts = value.Split('/', StringSplitOptions.None);
    if (!value.StartsWith("/Game/", StringComparison.Ordinal) || value.Contains('\\') ||
        value.EndsWith("/", StringComparison.Ordinal) || parts.Length < 4 ||
        parts.Skip(1).Any(part => String.IsNullOrWhiteSpace(part) || part is "." or "..") ||
        parts.Any(part => part.Contains('.')))
        throw new InvalidDataException($"{label} package path must be a canonical /Game/... package without an extension.");
}

static string FindContentRoot(string workRoot, string referringPackagePath)
{
    var current = new DirectoryInfo(Path.GetDirectoryName(referringPackagePath) ?? workRoot);
    while (current is not null)
    {
        if (String.Equals(current.Name, "Content", StringComparison.OrdinalIgnoreCase))
            return current.FullName;
        current = current.Parent;
    }

    foreach (var candidate in new[]
    {
        Path.Combine(workRoot, "source", "Content"),
        Path.Combine(workRoot, "staging", "Content"),
        Path.Combine(workRoot, "Content"),
    })
    {
        if (Directory.Exists(candidate)) return Path.GetFullPath(candidate);
    }

    throw new DirectoryNotFoundException("Could not locate a staged Content directory for Blueprint schema loading.");
}

static MapInspectionResult InspectMapRequest(
    string workRoot,
    MapInspectionRequest request,
    EngineVersion engineVersion,
    Usmap mappings)
{
    var inspectionTimer = Stopwatch.StartNew();
    var inputPath = Path.GetFullPath(request.InputFile);
    if (!IsWithin(inputPath, workRoot)) throw new InvalidDataException("Input map must stay inside the supplied work root.");
    var loadTimer = Stopwatch.StartNew();
    UAsset asset;
    try
    {
        asset = new UAsset(inputPath, engineVersion, mappings);
    }
    catch (Exception exception) when (exception is not OutOfMemoryException and not StackOverflowException)
    {
        loadTimer.Stop();
        inspectionTimer.Stop();
        return new MapInspectionResult(
            inputPath, new List<ClassInspection>(), new List<ActorSchemaValidationResult>(),
            CaptureHelperMemory(inspectionTimer.ElapsedMilliseconds), loadTimer.ElapsedMilliseconds,
            0, $"{exception.GetType().Name}: {exception.Message}");
    }
    loadTimer.Stop();
    var analysisTimer = Stopwatch.StartNew();
    var inspections = new List<ClassInspection>();
    foreach (var oneBasedIndex in request.ExportIndices.Distinct().Order())
    {
        var exportIndex = oneBasedIndex - 1;
        if (exportIndex < 0 || exportIndex >= asset.Exports.Count) continue;
        var export = asset.Exports[exportIndex];
        try
        {
            var classInfo = ResolveClassImport(asset, export.ClassIndex);
            inspections.Add(new ClassInspection(
                export.ObjectName.ToString(), oneBasedIndex, classInfo.ClassName, classInfo.PackagePath));
        }
        catch (Exception exception) when (exception is not OutOfMemoryException and not StackOverflowException)
        {
            // Python treats omitted catalog exports as incompatible locations.
        }
    }
    var validations = new List<ActorSchemaValidationResult>();
    var registeredBlueprintSchemaPackages = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    using var exportReader = asset.PathToReader(inputPath, true);
    foreach (var validation in request.ActorValidations ?? new List<ActorSchemaValidationRequest>())
    {
        var exportIndex = validation.ExportIndex - 1;
        if (exportIndex < 0 || exportIndex >= asset.Exports.Count)
        {
            validations.Add(new ActorSchemaValidationResult(
                validation.ExportIndex, validation.ExportName, false, "Catalog export index is not present in this map."));
            continue;
        }

        try
        {
            var targetExport = asset.Exports[exportIndex];
            var targetClass = ResolveClassImport(asset, targetExport.ClassIndex);
            if (!String.Equals(targetExport.ObjectName.ToString(), validation.ExportName, StringComparison.Ordinal))
                throw new InvalidDataException("Catalog export identity does not match the source map.");
            if (targetClass.ClassName == "BlueprintGeneratedClass" &&
                targetClass.PackagePath.StartsWith("/Game/", StringComparison.Ordinal))
            {
                RegisterBlueprintSchemaPackage(
                    workRoot, inputPath, targetClass.PackagePath, engineVersion, mappings,
                    registeredBlueprintSchemaPackages, new HashSet<string>(StringComparer.OrdinalIgnoreCase));
            }
            if (asset.Exports[exportIndex] is not NormalExport)
                asset.ParseExport(exportReader, exportIndex, true);
            if (asset.Exports[exportIndex] is not NormalExport actor)
                throw new InvalidDataException(
                    $"Target export parsed as {asset.Exports[exportIndex].GetType().Name}; required Blueprint schemas could not be loaded.");

            for (var rewardIndex = 0; rewardIndex < validation.ExpectedRewards.Count; rewardIndex++)
            {
                var actual = ReadReward(actor, rewardIndex);
                if (!RewardTupleMatches(actual, validation.ExpectedRewards[rewardIndex]))
                    throw new InvalidDataException($"Reward {rewardIndex} no longer matches the catalog.");
            }
            if (validation.RequireUniqueSaveId &&
                !String.Equals(ReadUniqueSaveId(actor), validation.ExpectedSaveId, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("UniqueSaveID no longer matches the catalog.");

            validations.Add(new ActorSchemaValidationResult(
                validation.ExportIndex, validation.ExportName, true, null));
        }
        catch (Exception exception) when (exception is not OutOfMemoryException and not StackOverflowException)
        {
            validations.Add(new ActorSchemaValidationResult(
                validation.ExportIndex, validation.ExportName, false,
                $"{exception.GetType().Name}: {exception.Message}"));
        }
    }
    analysisTimer.Stop();
    inspectionTimer.Stop();
    return new MapInspectionResult(
        inputPath, inspections, validations, CaptureHelperMemory(inspectionTimer.ElapsedMilliseconds),
        loadTimer.ElapsedMilliseconds, analysisTimer.ElapsedMilliseconds, null);
}

// Find exports by exact object name
static MapExportSearchResult FindMapExports(
    string workRoot,
    MapExportSearchRequest request,
    EngineVersion engineVersion,
    Usmap mappings)
{
    var timer = Stopwatch.StartNew();
    var inputPath = Path.GetFullPath(request.InputFile);
    if (!IsWithin(inputPath, workRoot)) throw new InvalidDataException("Input map must stay inside the supplied work root.");
    if (request.ExportNames is null || request.ExportNames.Length == 0 || request.ExportNames.Length > 20000)
        throw new InvalidDataException("Map export search requires a bounded list of object names.");
    var expectedNames = new HashSet<string>(request.ExportNames, StringComparer.OrdinalIgnoreCase);
    var loadTimer = Stopwatch.StartNew();
    var asset = new UAsset(inputPath, engineVersion, mappings);
    loadTimer.Stop();
    var analysisTimer = Stopwatch.StartNew();
    var matches = new List<NamedMapExport>();
    for (var index = 0; index < asset.Exports.Count; index++)
    {
        var export = asset.Exports[index];
        var exportName = export.ObjectName.ToString();
        if (!expectedNames.Contains(exportName)) continue;
        var classInfo = ResolveClassImport(asset, export.ClassIndex);
        matches.Add(new NamedMapExport(
            exportName, index + 1, classInfo.ClassName, classInfo.PackagePath));
    }
    analysisTimer.Stop();
    timer.Stop();
    return new MapExportSearchResult(
        inputPath, matches, CaptureHelperMemory(timer.ElapsedMilliseconds),
        loadTimer.ElapsedMilliseconds, analysisTimer.ElapsedMilliseconds);
}

static BlueprintParent InspectBlueprintParent(
    string workRoot,
    string inputPath,
    EngineVersion engineVersion,
    Usmap mappings)
{
    var inspectionTimer = Stopwatch.StartNew();
    if (!IsWithin(inputPath, workRoot))
        throw new InvalidDataException("Input package must stay inside the supplied work root.");
    var loadTimer = Stopwatch.StartNew();
    var asset = new UAsset(inputPath, engineVersion, mappings);
    loadTimer.Stop();
    var analysisTimer = Stopwatch.StartNew();
    var classExport = asset.Exports.OfType<ClassExport>().FirstOrDefault()
        ?? throw new InvalidDataException($"Blueprint package has no parsed ClassExport: {Path.GetFileName(inputPath)}");
    var parent = ResolveClassImport(asset, classExport.SuperIndex);
    var parentPackage = parent.ClassName == "BlueprintGeneratedClass"
        && parent.PackagePath.StartsWith("/Game/", StringComparison.Ordinal)
            ? parent.PackagePath
            : null;
    var resetFlag = ReadBlueprintResetFlag(asset, classExport);
    analysisTimer.Stop();
    inspectionTimer.Stop();
    return new BlueprintParent(
        classExport.ObjectName.ToString(), parent.ClassName, parentPackage,
        resetFlag, CaptureHelperMemory(inspectionTimer.ElapsedMilliseconds),
        loadTimer.ElapsedMilliseconds, analysisTimer.ElapsedMilliseconds);
}

static (string ClassName, string PackagePath) ResolveClassImport(UAsset asset, FPackageIndex packageIndex)
{
    if (!packageIndex.IsImport())
        return (packageIndex.IsExport() ? "ExportClass" : "None", string.Empty);
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
    return (className, string.Empty);
}

sealed record MapInspectionBatch(List<MapInspectionRequest> Maps);
sealed record MapInspectionRequest(
    string InputFile,
    int[] ExportIndices,
    List<ActorSchemaValidationRequest>? ActorValidations = null);
sealed record MapInspectionResult(
    string InputFile,
    List<ClassInspection> Classes,
    List<ActorSchemaValidationResult> Validations,
    HelperMemoryResult Memory,
    long LoadMilliseconds,
    long AnalysisMilliseconds,
    string? InspectionError);
sealed record ActorSchemaValidationRequest(
    int ExportIndex,
    string ExportName,
    List<RewardTuple> ExpectedRewards,
    bool RequireUniqueSaveId = false,
    string? ExpectedSaveId = null);
sealed record ActorSchemaValidationResult(int ExportIndex, string ExportName, bool Compatible, string? Reason);
sealed record MapExportSearchBatch(List<MapExportSearchRequest> Maps);
sealed record MapExportSearchRequest(string InputFile, string[] ExportNames);
sealed record MapExportSearchResult(
    string InputFile,
    List<NamedMapExport> Matches,
    HelperMemoryResult Memory,
    long LoadMilliseconds,
    long AnalysisMilliseconds);
sealed record NamedMapExport(string ExportName, int ExportIndex, string ClassName, string PackagePath);
sealed record BinaryMapDescriptorBatch(List<BinaryMapDescriptorRequest> Maps, List<string> CandidateNameValues);
sealed record BinaryMapDescriptorRequest(
    string InputFile, string PackagePath, List<BinaryRewardFieldRequest> Rewards,
    List<int>? ClassExportIndices = null);
sealed record BinaryRewardFieldRequest(int ExportIndex, int RewardIndex, string ExportName, RewardTuple Expected);
sealed record BinaryMapDescriptorResult(
    string PackagePath,
    string InputFile,
    int Version,
    string? HeaderSha256,
    string? UexpSha256,
    long HeaderSize,
    long? UexpSize,
    Dictionary<string, int> NameMapIndices,
    List<BinaryRewardFieldDescriptor> Fields,
    string? UnavailableReason,
    List<ClassInspection> Classes)
{
    // Report unavailable helper operation
    public static BinaryMapDescriptorResult Unavailable(
        BinaryMapDescriptorRequest request, string inputFile, long headerSize,
        string headerSha256, long? uexpSize, string? uexpSha256, string reason) =>
        new(request.PackagePath, inputFile, 1, headerSha256, uexpSha256, headerSize,
            uexpSize, new Dictionary<string, int>(StringComparer.Ordinal),
            request.Rewards.Select(field => new BinaryRewardFieldDescriptor(
                field.ExportIndex, field.RewardIndex, field.ExportName, null, null,
                null, false, reason)).ToList(), reason, new List<ClassInspection>());
}
sealed record BinaryRewardFieldDescriptor(
    int ExportIndex,
    int RewardIndex,
    string ExportName,
    int? Offset,
    string? ExpectedBytesHex,
    SoftObjectPathIdentityDto? ExpectedIdentity,
    bool Available,
    string? UnavailableReason);
sealed record HelperMemoryResult(
    long WorkingSetBytes,
    long PrivateBytes,
    long PeakWorkingSetBytes,
    long ManagedHeapBytes,
    long ManagedHeapSizeBytes,
    long ElapsedMilliseconds);
sealed record BatchPatchDocument(List<PackagePatchRequest> Packages);
sealed record PackagePatchRequest(
    string InputFile,
    string OutputFile,
    List<RewardPatchRequest> Patches,
    int PackageIndex = 1,
    int PackageCount = 1,
    string? BlueprintSchemaPackage = null);
sealed record RewardPatchRequest(
    int ExportIndex,
    int RewardIndex,
    string ExportName,
    RewardTuple Expected,
    RewardTuple Assigned,
    bool RequireUniqueSaveId = true);
sealed record BatchPackageResult(
    string InputFile,
    string OutputFile,
    List<PatchResult> Patches,
    bool ReopenedAndValidated,
    bool RawPayloadPreserved);
sealed record RawExportTableEntry(long SerialSize, long SerialOffset);
sealed record RawFieldRange(int Offset, int Length, string FieldName);
sealed record RawPreservationSummary(int NonTargetExportCount, int TargetExportCount, int RegisteredFieldByteCount);
sealed record ClassInspection(string ExportName, int ExportIndex, string ClassName, string PackagePath);
sealed record BlueprintParent(
    string ClassExportName,
    string ParentClassName,
    string? ParentPackage,
    bool? ResetOnYadorigiRest,
    HelperMemoryResult? Memory = null,
    long LoadMilliseconds = 0,
    long AnalysisMilliseconds = 0);

sealed record FNameIdentityDto(string Value, int Number);
sealed record SoftObjectPathIdentityDto(FNameIdentityDto PackageName, FNameIdentityDto AssetName);
sealed record RewardTuple(string Path, int Count, bool Notification, SoftObjectPathIdentityDto? SoftObjectPathIdentity = null);
sealed record PatchResult(string ExportName, int ExportIndex, int RewardIndex, string UniqueSaveId,
    RewardTuple Original, RewardTuple Assigned, string OutputFile, bool ReopenedAndValidated,
    bool RawPayloadPreserved = true);
