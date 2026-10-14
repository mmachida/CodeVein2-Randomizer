using System.Buffers.Binary;
using System.Collections;
using System.Diagnostics;
using System.Reflection;
using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class BloodCodePatcher
{
    private const string BloodCodePrefix = "/Game/Blueprint/Article/BloodCode/";
    private const string BequeathedFormaPrefix = "/Game/Blueprint/Article/Gadget/";
    private const string WorldArticlePrefix = "/Game/Blueprint/Article/";
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };


    // Scan installed game content

    public static int ScanContent(string[] args)
    {
        try
        {
            var contentRoot = Path.GetFullPath(args[1]);
            var mappingsPath = Path.GetFullPath(args[2]);
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var questRoot = Path.Combine(contentRoot, "Quest");
            if (!Directory.Exists(questRoot))
                throw new DirectoryNotFoundException("Content root does not contain a Quest directory.");
            if (!File.Exists(mappingsPath))
                throw new FileNotFoundException("CV2.usmap mapping was not found.", mappingsPath);

            var mappings = new Usmap(mappingsPath);
            var assets = Directory.EnumerateFiles(questRoot, "*.uasset", SearchOption.AllDirectories)
                .OrderBy(path => path, StringComparer.OrdinalIgnoreCase)
                .ToArray();
            var hits = new List<GrantHit>();
            var failures = new List<ScanFailure>();
            var parsedTasks = 0;

            foreach (var inputPath in assets)
            {
                var relative = Path.GetRelativePath(contentRoot, inputPath).Replace(Path.DirectorySeparatorChar, '/');
                var virtualPackage = "/Game/" + Path.ChangeExtension(relative, null);
                try
                {
                    var flags = CustomSerializationFlags.SkipParsingExports |
                                CustomSerializationFlags.SkipPreloadDependencyLoading;
                    var asset = new UAsset(inputPath, true, engineVersion, mappings, flags);
                    var candidateIndices = asset.Exports
                        .Select((export, index) => (export, index))
                        .Where(item => IsGainItem(item.export.ObjectName.ToString()))
                        .Select(item => item.index)
                        .ToArray();
                    if (candidateIndices.Length == 0) continue;

                    using var reader = asset.PathToReader(inputPath, true);
                    foreach (var exportIndex in candidateIndices)
                    {
                        try
                        {
                            if (asset.Exports[exportIndex] is not NormalExport normal)
                                asset.ParseExport(reader, exportIndex, true);
                            if (asset.Exports[exportIndex] is not NormalExport parsed)
                                continue;
                            parsedTasks++;
                            var bulkIndex = 0;
                            foreach (var bulk in Walk(parsed.Data).OfType<StructPropertyData>().Where(IsArticleBulkInfo))
                            {
                                var articles = bulk.Value.OfType<ArrayPropertyData>()
                                    .Where(property => String.Equals(property.Name.ToString(), "Articles", StringComparison.Ordinal))
                                    .ToArray();
                                if (articles.Length != 1)
                                    throw new InvalidDataException($"{parsed.ObjectName} has an ArticleBulkInfo with {articles.Length} Articles arrays.");
                                var articleIndex = 0;
                                foreach (var rawArticle in articles[0].Value)
                                {
                                    if (rawArticle is not StructPropertyData article)
                                        throw new InvalidDataException($"{parsed.ObjectName} Articles[{articleIndex}] is not a struct.");
                                    var paths = article.Value.OfType<SoftObjectPropertyData>()
                                        .Where(property => String.Equals(property.Name.ToString(), "ArticleDataPath", StringComparison.Ordinal))
                                        .ToArray();
                                    if (paths.Length != 1)
                                        throw new InvalidDataException($"{parsed.ObjectName} Articles[{articleIndex}] must have one ArticleDataPath.");
                                    var path = ReadRawSoftPath(paths[0]);
                                    if (path.PackagePath.StartsWith(BloodCodePrefix, StringComparison.Ordinal))
                                    {
                                        ValidateBloodCodePath(path);
                                        var count = article.Value.OfType<IntPropertyData>()
                                            .SingleOrDefault(property => String.Equals(property.Name.ToString(), "Count", StringComparison.Ordinal))
                                            ?? throw new InvalidDataException($"{parsed.ObjectName} Blood Code article has no integer Count.");
                                        var notification = article.Value.OfType<BoolPropertyData>()
                                            .SingleOrDefault(property => String.Equals(property.Name.ToString(), "bNotification", StringComparison.Ordinal))
                                            ?? throw new InvalidDataException($"{parsed.ObjectName} Blood Code article has no bNotification.");
                                        hits.Add(new GrantHit(
                                            virtualPackage,
                                            exportIndex + 1,
                                            parsed.ObjectName.ToString(),
                                            bulkIndex,
                                            articleIndex,
                                            path.PackagePath,
                                            path.AssetName,
                                            count.Value,
                                            notification.Value,
                                            path.PackageNameValue,
                                            path.PackageNameNumber,
                                            path.AssetNameValue,
                                            path.AssetNameNumber,
                                            path.SubPathString,
                                            articles[0].Value.Count()));
                                    }
                                    articleIndex++;
                                }
                                bulkIndex++;
                            }
                        }
                        catch (Exception exception)
                        {
                            failures.Add(new ScanFailure(virtualPackage, exportIndex + 1, exception.GetType().Name, exception.Message));
                        }
                    }
                }
                catch (Exception exception)
                {
                    failures.Add(new ScanFailure(virtualPackage, null, exception.GetType().Name, exception.Message));
                }
            }

            var report = new ScanReport(assets.Length, parsedTasks, hits.Count, failures.Count, hits, failures);
            Console.WriteLine(JsonSerializer.Serialize(report, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Inspect packages

    public static int InspectPackages(string[] args)
        => InspectPackages(args, BloodCodePrefix, "Blood Code");


    // Inspect Article identities

    public static int InspectArticleIdentities(string[] args)
    {
        try
        {
            var root = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var version = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<InspectManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Blood Code Article inventory manifest is empty.");
            if (manifest.Packages.Count == 0)
                throw new InvalidDataException("Blood Code Article inventory must contain at least one package.");
            var results = new List<ArticleIdentity>();
            foreach (var request in manifest.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase))
            {
                if (!request.VirtualPackage.StartsWith(BloodCodePrefix, StringComparison.Ordinal))
                    throw new InvalidDataException("Article inventory package is outside the Blood Code Article folder.");
                var input = ResolveOwnedPath(root, request.InputFile, mustExist: true);
                var asset = new UAsset(input, true, version, mappings,
                    CustomSerializationFlags.SkipParsingExports | CustomSerializationFlags.SkipPreloadDependencyLoading);
                var expectedName = request.VirtualPackage[(request.VirtualPackage.LastIndexOf('/') + 1)..];
                var matches = asset.Exports.Select((export, index) => (export, index))
                    .Where(item => item.export.ObjectName.ToString() == expectedName).ToArray();
                if (matches.Length != 1)
                    throw new InvalidDataException($"{request.VirtualPackage} has {matches.Length} exact export-name matches.");
                var (export, index) = matches[0];
                results.Add(new ArticleIdentity(
                    request.VirtualPackage, input, index + 1, export.ObjectName.ToString(),
                    export.ObjectName.Number, export.ClassIndex.Index));
            }
            Console.WriteLine(JsonSerializer.Serialize(results, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Inspect Forma reward packages

    public static int InspectBequeathedFormaPackages(string[] args)
        => InspectPackages(args, BequeathedFormaPrefix, "Bequeathed Forma");


    // Inspect quest reward packages

    public static int InspectWorldLootQuestPackages(string[] args)
        => InspectPackages(args, WorldArticlePrefix, "World Loot quest Article");


    // Inspect packages

    private static int InspectPackages(string[] args, string rewardPrefix, string featureName)
    {
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<InspectManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException($"{featureName} inspection manifest is empty.");
            if (manifest.Packages.Count == 0 || manifest.Packages.Select(item => item.VirtualPackage).Distinct(StringComparer.OrdinalIgnoreCase).Count() != manifest.Packages.Count)
                throw new InvalidDataException($"{featureName} inspection must contain unique source packages.");
            var results = manifest.Packages
                .OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase)
                .Select(item => InspectPackage(workRoot, item, engineVersion, mappings, rewardPrefix))
                .ToArray();
            Console.WriteLine(JsonSerializer.Serialize(results, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Patch asset packages

    public static int PatchPackages(string[] args)
        => PatchPackages(args, BloodCodePrefix, "Blood Code");


    // Inspect Blood Code references

    public static int InspectBloodCodeReferences(string[] args)
        => InspectReferencePackages(args);


    // Patch Blood Code references

    public static int PatchBloodCodeReferences(string[] args)
        => PatchReferencePackages(args);


    // Patch Forma quest rewards

    public static int PatchBequeathedFormaPackages(string[] args)
        => PatchPackages(args, BequeathedFormaPrefix, "Bequeathed Forma");


    // Patch quest reward packages

    public static int PatchWorldLootQuestPackages(string[] args)
        => PatchPackages(args, WorldArticlePrefix, "World Loot quest Article");


    // Patch asset packages

    private static int PatchPackages(string[] args, string rewardPrefix, string featureName)
    {
        var committed = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<PatchManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException($"{featureName} patch manifest is empty.");
            if (manifest.Packages.Count == 0 || manifest.Packages.Select(item => item.VirtualPackage).Distinct(StringComparer.OrdinalIgnoreCase).Count() != manifest.Packages.Count)
                throw new InvalidDataException($"{featureName} patch must contain unique source packages.");
            var results = new List<PackagePatchResult>();
            foreach (var request in manifest.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase))
            {
                var result = PatchPackage(workRoot, request, engineVersion, mappings, rewardPrefix, featureName);
                foreach (var path in result.OutputFiles) committed.Add(path);
                results.Add(result);
            }
            Console.WriteLine(JsonSerializer.Serialize(results, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            foreach (var path in committed)
                try { File.Delete(path); } catch (IOException) { }
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Inspect one asset package

    private static PackageInspection InspectPackage(
        string workRoot,
        PackageInspectionRequest request,
        EngineVersion engineVersion,
        Usmap mappings,
        string rewardPrefix)
    {
        var inputPath = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
        ValidateVirtualPackage(request.VirtualPackage);
        var asset = LoadPackage(inputPath, engineVersion, mappings);
        var grants = ReadPackageGrants(asset, inputPath, request.VirtualPackage, rewardPrefix);
        return new PackageInspection(request.VirtualPackage, inputPath, grants.Count, grants.ToArray());
    }


    // Patch one asset package

    private static PackagePatchResult PatchPackage(
        string workRoot,
        PackagePatchRequest request,
        EngineVersion engineVersion,
        Usmap mappings,
        string rewardPrefix,
        string featureName)
    {
        var packageTimer = Stopwatch.StartNew();
        if (request.Assignments.Count == 0)
            throw new InvalidDataException($"{request.VirtualPackage} has no changed {featureName} assignments.");
        ValidateVirtualPackage(request.VirtualPackage);
        var inputPath = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
        var outputPath = ResolveOwnedPath(workRoot, request.OutputFile, mustExist: false);
        if (String.Equals(inputPath, outputPath, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"{featureName} staged output must be separate from its source package.");
        if (File.Exists(outputPath))
            throw new IOException($"Refusing to overwrite staged {featureName} output {Path.GetFileName(outputPath)}.");

        var loadTimer = Stopwatch.StartNew();
        var asset = LoadPackage(inputPath, engineVersion, mappings);
        loadTimer.Stop();
        var inspectTimer = Stopwatch.StartNew();
        var grants = ReadPackageGrants(asset, inputPath, request.VirtualPackage, rewardPrefix);
        ParsePreservedExports(asset, inputPath, request.Assignments);
        var exportIdentities = asset.Exports.Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();
        var grantMap = grants.ToDictionary(grant => (grant.ExportIndex, grant.BulkIndex, grant.RewardIndex));
        var seen = new HashSet<(int ExportIndex, int BulkIndex, int RewardIndex)>();
        var masksByExport = new Dictionary<int, HashSet<(int BulkIndex, int RewardIndex)>>();
        var repeatableGainExports = new HashSet<int>();
        foreach (var patch in request.Assignments)
        {
            if (patch.ExportIndex < 1 || patch.BulkIndex < 0 || patch.RewardIndex < 0 ||
                !seen.Add((patch.ExportIndex, patch.BulkIndex, patch.RewardIndex)))
                throw new InvalidDataException($"{featureName} patch contains an invalid or duplicate destination identity.");
            ValidateRewardPath(patch.AssignedPackage, patch.AssignedAsset, rewardPrefix, featureName);
            var key = (patch.ExportIndex, patch.BulkIndex, patch.RewardIndex);
            if (!grantMap.TryGetValue(key, out var actual) ||
                !String.Equals(actual.ExportName, patch.ExportName, StringComparison.Ordinal) ||
                !String.Equals(actual.RewardPackage, patch.ExpectedPackage, StringComparison.Ordinal) ||
                !String.Equals(actual.RewardAsset, patch.ExpectedAsset, StringComparison.Ordinal) ||
                (patch.ExpectedPackageNameValue is not null && actual.PackageNameValue != patch.ExpectedPackageNameValue) ||
                (patch.ExpectedPackageNameNumber is not null && actual.PackageNameNumber != patch.ExpectedPackageNameNumber) ||
                (patch.ExpectedAssetNameValue is not null && actual.AssetNameValue != patch.ExpectedAssetNameValue) ||
                (patch.ExpectedAssetNameNumber is not null && actual.AssetNameNumber != patch.ExpectedAssetNameNumber) ||
                (patch.ExpectedSubPathString is not null && actual.SubPathString != patch.ExpectedSubPathString) ||
                (patch.ExpectedArticlesCount is not null && actual.ArticlesCount != patch.ExpectedArticlesCount) ||
                actual.Count != patch.ExpectedCount || actual.Notification != patch.ExpectedNotification)
                throw new InvalidDataException($"{request.VirtualPackage} {featureName} source no longer matches {patch.ExportName}[{patch.BulkIndex}:{patch.RewardIndex}].");
            if (!patch.PatchArticlePath &&
                (!String.Equals(patch.ExpectedPackage, patch.AssignedPackage, StringComparison.Ordinal) ||
                 !String.Equals(patch.ExpectedAsset, patch.AssignedAsset, StringComparison.Ordinal)))
                throw new InvalidDataException("ArticleDataPath cannot be left untouched when its assigned reward differs from the source.");
            if (patch.AllowMultipleGain)
            {
                var targetExport = asset.Exports[patch.ExportIndex - 1] as NormalExport
                    ?? throw new InvalidDataException($"{patch.ExportName} is not a parsed GainItem export.");
                if (!ReadNoMultipleGain(targetExport).Value)
                    throw new InvalidDataException($"{request.VirtualPackage} {patch.ExportName} no longer has bNoMultipleGain=true.");
                repeatableGainExports.Add(patch.ExportIndex - 1);
            }
            if (!masksByExport.TryGetValue(patch.ExportIndex - 1, out var mask))
                masksByExport[patch.ExportIndex - 1] = mask = new HashSet<(int, int)>();
            if (patch.PatchArticlePath)
                mask.Add((patch.BulkIndex, patch.RewardIndex));
        }
        var strictExportSnapshots = request.Assignments.Any(
            patch => patch.PreservedExportIndex is not null || patch.RequireStrictExportSnapshots);
        var beforeSnapshots = SnapshotExports(asset, masksByExport, strictExportSnapshots, repeatableGainExports);
        inspectTimer.Stop();
        var mutationTimer = Stopwatch.StartNew();
        foreach (var patch in request.Assignments)
        {
            var exportIndex = patch.ExportIndex - 1;
            if (asset.Exports[exportIndex] is not NormalExport actor)
                throw new InvalidDataException($"{patch.ExportName} is not a parsed GainItem export.");
            if (patch.PatchArticlePath)
            {
                var article = FindArticle(actor, patch.BulkIndex, patch.RewardIndex);
                SetSoftPath(asset, article.PathProperty, patch.AssignedPackage, patch.AssignedAsset);
            }
        }
        foreach (var exportIndex in repeatableGainExports)
        {
            var actor = asset.Exports[exportIndex] as NormalExport
                ?? throw new InvalidDataException("Repeatable reward task is not a parsed GainItem export.");
            ReadNoMultipleGain(actor).Value = false;
        }
        mutationTimer.Stop();

        var outputDirectory = Path.GetDirectoryName(outputPath)!;
        Directory.CreateDirectory(outputDirectory);
        var tempHeader = Path.Combine(outputDirectory, Path.GetFileNameWithoutExtension(outputPath) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(outputPath));
        var moved = new List<string>();
        try
        {
            var serializeTimer = Stopwatch.StartNew();
            asset.Write(tempHeader);
            CopyAdditionalSidecars(inputPath, tempHeader);
            ValidateHeaderAndSidecar(inputPath, tempHeader);
            serializeTimer.Stop();
            var reopenTimer = Stopwatch.StartNew();
            var reopened = LoadPackage(tempHeader, engineVersion, mappings);
            _ = ReadPackageGrants(reopened, tempHeader, request.VirtualPackage, rewardPrefix);
            ParsePreservedExports(reopened, tempHeader, request.Assignments);
            ValidateExports(exportIdentities, reopened.Exports);
            var afterSnapshots = SnapshotExports(reopened, masksByExport, strictExportSnapshots, repeatableGainExports);
            ValidateSnapshots(beforeSnapshots, afterSnapshots, masksByExport.Keys, featureName);
            foreach (var exportIndex in repeatableGainExports)
            {
                var actor = reopened.Exports[exportIndex] as NormalExport
                    ?? throw new InvalidDataException("Reopened repeatable reward task is not a parsed GainItem export.");
                if (ReadNoMultipleGain(actor).Value)
                    throw new InvalidDataException("Reopened Hot Spring task still has bNoMultipleGain=true.");
            }
            foreach (var patch in request.Assignments)
            {
                var actor = reopened.Exports[patch.ExportIndex - 1] as NormalExport
                    ?? throw new InvalidDataException("Reopened Blood Code export is not parseable.");
                var article = FindArticle(actor, patch.BulkIndex, patch.RewardIndex);
                var actual = ReadArticleReward(article);
                if (actual.RewardPackage != patch.AssignedPackage || actual.RewardAsset != patch.AssignedAsset ||
                    (patch.PatchArticlePath && (actual.PackageNameValue != patch.AssignedPackage || actual.PackageNameNumber != 0 ||
                    actual.AssetNameValue != patch.AssignedAsset || actual.AssetNameNumber != 0)) ||
                    (patch.ExpectedSubPathString is not null && actual.SubPathString != patch.ExpectedSubPathString) ||
                    (patch.ExpectedArticlesCount is not null && actual.ArticlesCount != patch.ExpectedArticlesCount) ||
                    actual.Count != patch.ExpectedCount || actual.Notification != patch.ExpectedNotification)
                    throw new InvalidDataException($"Reopened {featureName} grant does not match its requested path and preserved fields.");
            }
            reopenTimer.Stop();
            var outputCopyTimer = Stopwatch.StartNew();
            var tempFiles = PackageFiles(tempHeader).Where(File.Exists).ToArray();
            var outputs = tempFiles.Select(path => Path.ChangeExtension(outputPath, Path.GetExtension(path))).ToArray();
            if (outputs.Any(File.Exists)) throw new IOException($"Refusing to overwrite a {featureName} output sidecar.");
            for (var index = 0; index < tempFiles.Length; index++)
            {
                File.Move(tempFiles[index], outputs[index]);
                moved.Add(outputs[index]);
            }
            outputCopyTimer.Stop();
            // The package was fully reopened and validated before the sidecars
            // were moved. They are moved within this same directory, so another
            // UAssetAPI load here repeats the same validation without changing
            // any bytes.
            packageTimer.Stop();
            return new PackagePatchResult(request.VirtualPackage, inputPath, outputPath, request.Assignments.Count,
                moved.ToArray(), 2, loadTimer.Elapsed.TotalSeconds, inspectTimer.Elapsed.TotalSeconds,
                mutationTimer.Elapsed.TotalSeconds, serializeTimer.Elapsed.TotalSeconds,
                reopenTimer.Elapsed.TotalSeconds, outputCopyTimer.Elapsed.TotalSeconds);
        }
        catch
        {
            foreach (var path in moved)
                try { File.Delete(path); } catch (IOException) { }
            throw;
        }
        finally
        {
            foreach (var path in PackageFiles(tempHeader))
                try { File.Delete(path); } catch (IOException) { }
        }
    }


    // Inspect referenced packages

    private static int InspectReferencePackages(string[] args)
    {
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            RegisterCompanionSchemas(workRoot, engineVersion, mappings);
            var manifest = JsonSerializer.Deserialize<ReferenceInspectManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Blood Code reference inspection manifest is empty.");
            ValidateReferencePackageRequests(manifest.Packages.Select(item => item.VirtualPackage));
            var result = manifest.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase)
                .Select(item => InspectReferencePackage(workRoot, item, engineVersion, mappings)).ToArray();
            Console.WriteLine(JsonSerializer.Serialize(result, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Inspect one referenced package

    private static ReferencePackageInspection InspectReferencePackage(
        string workRoot,
        ReferencePackageInspectRequest request,
        EngineVersion engineVersion,
        Usmap mappings)
    {
        var inputPath = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
        ValidateVirtualPackage(request.VirtualPackage, allowScenario: true);
        var asset = LoadPackage(inputPath, engineVersion, mappings);
        var references = ReadReferenceTargets(asset, inputPath, request.VirtualPackage, request.References);
        return new ReferencePackageInspection(request.VirtualPackage, inputPath, references.Count, references);
    }


    // Patch referenced packages

    private static int PatchReferencePackages(string[] args)
    {
        var committed = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            RegisterCompanionSchemas(workRoot, engineVersion, mappings);
            var manifest = JsonSerializer.Deserialize<ReferencePatchManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Blood Code reference patch manifest is empty.");
            ValidateReferencePackageRequests(manifest.Packages.Select(item => item.VirtualPackage));
            var results = new List<ReferencePackagePatchResult>();
            foreach (var request in manifest.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase))
            {
                var result = PatchReferencePackage(workRoot, request, engineVersion, mappings);
                foreach (var path in result.OutputFiles) committed.Add(path);
                results.Add(result);
            }
            Console.WriteLine(JsonSerializer.Serialize(results, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            foreach (var path in committed)
                try { File.Delete(path); } catch (IOException) { }
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Patch one referenced package

    private static ReferencePackagePatchResult PatchReferencePackage(
        string workRoot,
        ReferencePackagePatchRequest request,
        EngineVersion engineVersion,
        Usmap mappings)
    {
        if (IsBloodCodeScenarioPackage(request.VirtualPackage))
            return PatchScenarioRewardReferencePackage(workRoot, request, engineVersion, mappings);

        if (request.Assignments.Count == 0)
            throw new InvalidDataException($"{request.VirtualPackage} has no changed Blood Code references.");
        ValidateVirtualPackage(request.VirtualPackage, allowScenario: true);
        var inputPath = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
        var outputPath = ResolveOwnedPath(workRoot, request.OutputFile, mustExist: false);
        if (String.Equals(inputPath, outputPath, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Blood Code reference output must be separate from its source package.");
        if (File.Exists(outputPath))
            throw new IOException($"Refusing to overwrite staged Blood Code package {Path.GetFileName(outputPath)}.");

        var asset = LoadPackage(inputPath, engineVersion, mappings);
        var referenceMap = ReadReferenceTargets(asset, inputPath, request.VirtualPackage, request.Assignments)
            .ToDictionary(item => (item.ExportIndex, item.ReferenceProperty, item.ReferenceIndex));
        var masksByExport = new Dictionary<int, HashSet<(string Property, int Index, int Length)>>();
        var seen = new HashSet<(int ExportIndex, string Property, int ArrayIndex)>();
        foreach (var patch in request.Assignments)
        {
            var key = (patch.ExportIndex, patch.ReferenceProperty, patch.ReferenceIndex);
            if (!seen.Add(key))
                throw new InvalidDataException("Blood Code reference patch contains a duplicate destination.");
            ValidateBloodCodePath(patch.AssignedPackage, patch.AssignedAsset);
            if (!referenceMap.TryGetValue(key, out var actual) ||
                actual.ExportName != patch.ExportName || actual.ArrayCount != patch.ExpectedArrayCount ||
                actual.RewardPackage != patch.ExpectedPackage || actual.RewardAsset != patch.ExpectedAsset ||
                actual.PackageNameValue != patch.ExpectedPackageNameValue || actual.PackageNameNumber != patch.ExpectedPackageNameNumber ||
                actual.AssetNameValue != patch.ExpectedAssetNameValue || actual.AssetNameNumber != patch.ExpectedAssetNameNumber ||
                actual.SubPathString != String.Empty)
                throw new InvalidDataException($"{request.VirtualPackage} {patch.ExportName}.{patch.ReferenceProperty}[{patch.ReferenceIndex}] no longer matches its catalogued vanilla value.");
            var exportIndex = patch.ExportIndex - 1;
            var export = asset.Exports[exportIndex] as NormalExport
                ?? throw new InvalidDataException($"{patch.ExportName} is not a parsed Blood Code proficiency task.");
            _ = FindReference(export, patch.ReferenceProperty, patch.ReferenceIndex, patch.ExpectedArrayCount);
            if (!masksByExport.TryGetValue(exportIndex, out var mask))
                masksByExport[exportIndex] = mask = new HashSet<(string, int, int)>();
            mask.Add((patch.ReferenceProperty, patch.ReferenceIndex, patch.ExpectedArrayCount));
        }

        var exportIdentities = asset.Exports.Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();
        var beforeSnapshots = SnapshotReferenceExports(asset, masksByExport);
        foreach (var patch in request.Assignments)
        {
            var export = asset.Exports[patch.ExportIndex - 1] as NormalExport
                ?? throw new InvalidDataException("Blood Code proficiency task was not parsed before patching.");
            var location = FindReference(export, patch.ReferenceProperty, patch.ReferenceIndex, patch.ExpectedArrayCount);
            SetSoftPath(asset, location.PathProperty, patch.AssignedPackage, patch.AssignedAsset);
        }

        var outputDirectory = Path.GetDirectoryName(outputPath)!;
        Directory.CreateDirectory(outputDirectory);
        var tempHeader = Path.Combine(outputDirectory, Path.GetFileNameWithoutExtension(outputPath) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(outputPath));
        var moved = new List<string>();
        try
        {
            asset.Write(tempHeader);
            CopyAdditionalSidecars(inputPath, tempHeader);
            ValidateHeaderAndSidecar(inputPath, tempHeader);
            var reopened = LoadPackage(tempHeader, engineVersion, mappings);
            var reopenedTargets = ReadReferenceTargets(reopened, tempHeader, request.VirtualPackage, request.Assignments)
                .ToDictionary(item => (item.ExportIndex, item.ReferenceProperty, item.ReferenceIndex));
            ValidateExports(exportIdentities, reopened.Exports);
            var afterSnapshots = SnapshotReferenceExports(reopened, masksByExport);
            ValidateSnapshots(beforeSnapshots, afterSnapshots, masksByExport.Keys, "Blood Code proficiency reference");
            foreach (var patch in request.Assignments)
            {
                var key = (patch.ExportIndex, patch.ReferenceProperty, patch.ReferenceIndex);
                if (!reopenedTargets.TryGetValue(key, out var actual) ||
                    actual.RewardPackage != patch.AssignedPackage || actual.RewardAsset != patch.AssignedAsset ||
                    actual.PackageNameValue != patch.AssignedPackage || actual.PackageNameNumber != 0 ||
                    actual.AssetNameValue != patch.AssignedAsset || actual.AssetNameNumber != 0 ||
                    actual.PackageNameMapIndex < 0 || actual.AssetNameMapIndex < 0 ||
                    actual.ArrayCount != patch.ExpectedArrayCount || actual.SubPathString != String.Empty)
                    throw new InvalidDataException($"Reopened {patch.ExportName}.{patch.ReferenceProperty}[{patch.ReferenceIndex}] did not preserve the assigned literal FName path and NameMap entries.");
            }

            foreach (var extension in new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" })
            {
                var sourceFile = Path.ChangeExtension(tempHeader, extension);
                if (!File.Exists(sourceFile)) continue;
                var finalFile = Path.ChangeExtension(outputPath, extension);
                if (File.Exists(finalFile)) throw new IOException($"Refusing to overwrite staged Blood Code sidecar {Path.GetFileName(finalFile)}.");
                File.Move(sourceFile, finalFile);
                moved.Add(finalFile);
            }
            var outputFiles = moved.OrderBy(path => path, StringComparer.OrdinalIgnoreCase).ToArray();
            if (!outputFiles.Contains(outputPath, StringComparer.OrdinalIgnoreCase))
                throw new InvalidDataException("Blood Code reference patch omitted its .uasset output.");
            return new ReferencePackagePatchResult(
                request.VirtualPackage, inputPath, outputPath, request.Assignments.Count, outputFiles);
        }
        catch
        {
            foreach (var path in moved)
                try { File.Delete(path); } catch (IOException) { }
            throw;
        }
        finally
        {
            foreach (var path in PackageFiles(tempHeader))
                try { File.Delete(path); } catch (IOException) { }
        }
    }


    // Patch the quest reward package

    private static ReferencePackagePatchResult PatchScenarioRewardReferencePackage(
        string workRoot,
        ReferencePackagePatchRequest request,
        EngineVersion engineVersion,
        Usmap mappings)
    {
        if (request.Assignments.Count == 0 ||
            request.Assignments.Any(item => item.ReferenceProperty != "ArticleBulkInfo.Articles"))
            throw new InvalidDataException("SmallScenario Blood Code patches must target ArticleBulkInfo.Articles.");
        ValidateVirtualPackage(request.VirtualPackage, allowScenario: true);

        var inputPath = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
        var outputPath = ResolveOwnedPath(workRoot, request.OutputFile, mustExist: false);
        if (String.Equals(inputPath, outputPath, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("SmallScenario Blood Code output must be separate from its source.");
        if (PackageFiles(outputPath).Any(File.Exists))
            throw new IOException($"Refusing to overwrite staged SmallScenario package {Path.GetFileName(outputPath)}.");

        // Read the CDO through the base Blueprint schema to validate the catalogued
        // ArticleDataPath, then patch only its two serialized FName indices. A full
        // UAssetAPI property rewrite expands these derived CDOs from 83 to 167 bytes;
        // the game reads the original 83-byte layout and crashes on that mismatch.
        var inspected = LoadPackage(inputPath, engineVersion, mappings);
        var sourceReferences = ReadReferenceTargets(inspected, inputPath, request.VirtualPackage, request.Assignments)
            .ToDictionary(item => (item.ExportIndex, item.ReferenceProperty, item.ReferenceIndex));
        foreach (var patch in request.Assignments)
        {
            var key = (patch.ExportIndex, patch.ReferenceProperty, patch.ReferenceIndex);
            if (!sourceReferences.TryGetValue(key, out var actual) ||
                actual.ExportName != patch.ExportName || actual.ArrayCount != patch.ExpectedArrayCount ||
                actual.RewardPackage != patch.ExpectedPackage || actual.RewardAsset != patch.ExpectedAsset ||
                actual.PackageNameValue != patch.ExpectedPackageNameValue || actual.PackageNameNumber != patch.ExpectedPackageNameNumber ||
                actual.AssetNameValue != patch.ExpectedAssetNameValue || actual.AssetNameNumber != patch.ExpectedAssetNameNumber ||
                actual.SubPathString != String.Empty ||
                actual.PackageNameMapIndex < 0 || actual.AssetNameMapIndex < 0)
                throw new InvalidDataException(
                    $"{request.VirtualPackage} {patch.ExportName}.{patch.ReferenceProperty}[{patch.ReferenceIndex}] no longer matches its catalogued vanilla value.");
            ValidateBloodCodePath(patch.AssignedPackage, patch.AssignedAsset);
        }

        var asset = LoadPackage(inputPath, engineVersion, mappings);
        var sourceHeaderLength = new FileInfo(inputPath).Length;
        var sourceRows = SnapshotRawExportRows(asset);
        var sourceNames = SnapshotNames(asset);
        var sourceImports = SnapshotImports(asset);
        var sourceSidecarPath = Path.ChangeExtension(inputPath, ".uexp");
        if (!File.Exists(sourceSidecarPath))
            throw new FileNotFoundException("SmallScenario Blood Code source is missing its .uexp sidecar.", sourceSidecarPath);
        var sourceSidecar = File.ReadAllBytes(sourceSidecarPath);
        var patches = new List<RawScenarioPatch>();

        foreach (var patch in request.Assignments)
        {
            var exportIndex = patch.ExportIndex - 1;
            if (exportIndex < 0 || exportIndex >= asset.Exports.Count ||
                asset.Exports[exportIndex] is not RawExport raw)
                throw new InvalidDataException(
                    $"{patch.ExportName} must remain an opaque RawExport during a SmallScenario Blood Code patch.");
            var sourceRow = sourceRows[exportIndex];
            if (sourceRow.ObjectName != patch.ExportName ||
                raw.SerialSize != sourceRow.SerialSize ||
                raw.Data.Length != sourceRow.SerialSize)
                throw new InvalidDataException(
                    $"{patch.ExportName} RawExport identity or payload size differs from its source ExportMap row.");

            var sourceDataStart = checked((int)(sourceRow.SerialOffset - sourceHeaderLength));
            var sourceDataSize = checked((int)sourceRow.SerialSize);
            if (sourceDataStart < 0 || sourceDataSize <= 0 ||
                sourceDataStart + sourceDataSize > sourceSidecar.Length)
                throw new InvalidDataException($"{patch.ExportName} serial range is outside the source .uexp sidecar.");
            var sourceData = sourceSidecar.AsSpan(sourceDataStart, sourceDataSize).ToArray();
            if (!raw.Data.AsSpan().SequenceEqual(sourceData))
                throw new InvalidDataException($"{patch.ExportName} RawExport bytes differ from its source .uexp range.");

            var expectedNames = FindNameIndices(
                sourceNames, patch.ExpectedPackageNameValue, patch.ExpectedAssetNameValue);
            var serializedPath = FindUniqueFNamePath(
                sourceData,
                expectedNames.Package,
                patch.ExpectedPackageNameNumber,
                expectedNames.Asset,
                patch.ExpectedAssetNameNumber,
                patch.ExportName);

            _ = new FName(asset, patch.AssignedPackage, 0);
            _ = new FName(asset, patch.AssignedAsset, 0);
            var namesAfterAdd = SnapshotNames(asset);
            var assignedPackageIndex = FindNameIndex(namesAfterAdd, patch.AssignedPackage);
            var assignedAssetIndex = FindNameIndex(namesAfterAdd, patch.AssignedAsset);
            var patchedData = sourceData.ToArray();
            WriteFNamePath(patchedData, serializedPath, assignedPackageIndex, 0, assignedAssetIndex, 0);
            EnsureOnlyRangeChanged(
                sourceData, patchedData, serializedPath.Offset, serializedPath.ByteLength,
                $"{patch.ExportName} changed outside its ArticleDataPath FName pair.");
            raw.Data = patchedData;
            patches.Add(new RawScenarioPatch(exportIndex, sourceDataStart, serializedPath, patchedData));
        }

        var outputDirectory = Path.GetDirectoryName(outputPath)!;
        Directory.CreateDirectory(outputDirectory);
        var temporary = Path.Combine(
            outputDirectory,
            Path.GetFileNameWithoutExtension(outputPath) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(outputPath));
        var moved = new List<string>();
        try
        {
            asset.Write(temporary);
            var serialized = LoadPackage(temporary, engineVersion, mappings);
            var headerDelta = new FileInfo(temporary).Length - sourceHeaderLength;
            PatchScenarioExportMapRows(temporary, serialized, sourceRows, headerDelta);

            var serializedSidecarPath = Path.ChangeExtension(temporary, ".uexp");
            if (!File.Exists(serializedSidecarPath))
                throw new InvalidDataException("UAssetAPI omitted the SmallScenario .uexp sidecar.");
            var patchedSidecar = sourceSidecar.ToArray();
            foreach (var patch in patches)
            {
                patch.PatchedData.AsSpan(patch.Path.Offset, patch.Path.ByteLength)
                    .CopyTo(patchedSidecar.AsSpan(
                        patch.SourceDataStart + patch.Path.Offset, patch.Path.ByteLength));
            }
            EnsureOnlyRangesChanged(
                sourceSidecar,
                patchedSidecar,
                patches.Select(item => (item.SourceDataStart + item.Path.Offset, item.Path.ByteLength)).ToArray(),
                "SmallScenario .uexp changed outside the assigned ArticleDataPath FName pair(s).");
            File.WriteAllBytes(serializedSidecarPath, patchedSidecar);
            CopyScenarioOpaqueSidecars(inputPath, temporary);

            var reopenedRaw = LoadPackage(temporary, engineVersion, mappings);
            ValidateScenarioExportRows(sourceRows, reopenedRaw, headerDelta);
            if (SnapshotImports(reopenedRaw) != sourceImports)
                throw new InvalidDataException("SmallScenario Blood Code patch changed package Imports.");
            ValidateAppendOnlyNames(
                sourceNames, SnapshotNames(reopenedRaw),
                request.Assignments.SelectMany(item => new[] { item.AssignedPackage, item.AssignedAsset })
                    .ToHashSet(StringComparer.Ordinal));
            foreach (var patch in patches)
            {
                if (reopenedRaw.Exports[patch.ExportIndex] is not RawExport reopenedRawExport ||
                    !reopenedRawExport.Data.AsSpan().SequenceEqual(patch.PatchedData))
                    throw new InvalidDataException("Reopened SmallScenario CDO does not retain the exact patched RawExport bytes.");
            }

            var reopenedParsed = LoadPackage(temporary, engineVersion, mappings);
            var reopenedReferences = ReadReferenceTargets(
                    reopenedParsed, temporary, request.VirtualPackage, request.Assignments)
                .ToDictionary(item => (item.ExportIndex, item.ReferenceProperty, item.ReferenceIndex));
            foreach (var patch in request.Assignments)
            {
                var key = (patch.ExportIndex, patch.ReferenceProperty, patch.ReferenceIndex);
                if (!reopenedReferences.TryGetValue(key, out var actual) ||
                    actual.RewardPackage != patch.AssignedPackage || actual.RewardAsset != patch.AssignedAsset ||
                    actual.PackageNameValue != patch.AssignedPackage || actual.PackageNameNumber != 0 ||
                    actual.AssetNameValue != patch.AssignedAsset || actual.AssetNameNumber != 0 ||
                    actual.PackageNameMapIndex < 0 || actual.AssetNameMapIndex < 0 ||
                    actual.ArrayCount != patch.ExpectedArrayCount || actual.SubPathString != String.Empty)
                    throw new InvalidDataException(
                        $"Reopened {patch.ExportName}.{patch.ReferenceProperty}[{patch.ReferenceIndex}] did not preserve the assigned literal path.");
            }

            foreach (var extension in new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" })
            {
                var sourceFile = Path.ChangeExtension(temporary, extension);
                if (!File.Exists(sourceFile)) continue;
                var finalFile = Path.ChangeExtension(outputPath, extension);
                if (File.Exists(finalFile))
                    throw new IOException($"Refusing to overwrite staged SmallScenario sidecar {Path.GetFileName(finalFile)}.");
                File.Move(sourceFile, finalFile);
                moved.Add(finalFile);
            }
            if (!moved.Contains(outputPath, StringComparer.OrdinalIgnoreCase))
                throw new InvalidDataException("SmallScenario Blood Code output omitted its .uasset header.");
            return new ReferencePackagePatchResult(
                request.VirtualPackage, inputPath, outputPath, request.Assignments.Count,
                moved.OrderBy(path => path, StringComparer.OrdinalIgnoreCase).ToArray());
        }
        catch
        {
            foreach (var path in moved)
                try { File.Delete(path); } catch (IOException) { }
            throw;
        }
        finally
        {
            foreach (var path in PackageFiles(temporary))
                try { File.Delete(path); } catch (IOException) { }
        }
    }


    // Capture raw export rows

    private static ExportRow[] SnapshotRawExportRows(UAsset asset) =>
        asset.Exports.Select(item => new ExportRow(
            item.SerialSize, item.SerialOffset, item.ObjectName.ToString(), item.ClassIndex.Index,
            item.SuperIndex.Index, item.TemplateIndex.Index, item.OuterIndex.Index,
            item.GetType().FullName ?? String.Empty)).ToArray();


    // Update serialized export offsets

    private static void PatchScenarioExportMapRows(
        string headerPath,
        UAsset serialized,
        ExportRow[] sourceRows,
        long headerDelta)
    {
        if (serialized.Exports.Count != sourceRows.Length)
            throw new InvalidDataException("Cannot preserve SmallScenario payloads after an ExportMap inventory change.");
        var header = File.ReadAllBytes(headerPath);
        var offsets = FindExportMapOffsets(headerPath, serialized);
        for (var index = 0; index < serialized.Exports.Count; index++)
        {
            var export = serialized.Exports[index];
            var row = sourceRows[index];
            if (export.ObjectName.ToString() != row.ObjectName ||
                export.SuperIndex.Index != row.SuperIndex ||
                export.OuterIndex.Index != row.OuterIndex ||
                export.GetType().FullName != row.ExportType)
                throw new InvalidDataException($"UAssetAPI changed SmallScenario export identity at export #{index + 1}.");
            BinaryPrimitives.WriteInt32LittleEndian(header.AsSpan(checked((int)offsets[index].ClassIndex), 4), row.ClassIndex);
            BinaryPrimitives.WriteInt32LittleEndian(header.AsSpan(checked((int)offsets[index].TemplateIndex), 4), row.TemplateIndex);
            EncodeExportSerialPair(row.SerialSize, checked(row.SerialOffset + headerDelta))
                .CopyTo(header.AsSpan(checked((int)offsets[index].SerialPair), 16));
        }
        File.WriteAllBytes(headerPath, header);
    }


    // Find export Map offsets

    private static ExportMapOffsets[] FindExportMapOffsets(string headerPath, UAsset asset)
    {
        var exportOffsetField = typeof(UAsset).GetField(
            "ExportOffset", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            ?? throw new InvalidDataException("Pinned UAssetAPI does not expose the ExportMap offset needed for raw preservation.");
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
                throw new InvalidDataException("ExportMap offsets do not match the pinned UAssetAPI parser.");
            reader.BaseStream.Position = entryStart;
            asset.Exports[index].ReadExportMapEntry(reader);
            var entryEnd = reader.BaseStream.Position;
            var serialPair = EncodeExportSerialPair(asset.Exports[index].SerialSize, asset.Exports[index].SerialOffset);
            var entry = header.AsSpan(checked((int)entryStart), checked((int)(entryEnd - entryStart)));
            var pairOffset = FindUniqueBytes(entry, serialPair, $"export #{index + 1} SerialSize/SerialOffset pair");
            offsets[index] = new ExportMapOffsets(entryStart, entryStart + 8, entryStart + pairOffset);
        }
        return offsets;
    }


    // Find unique f name path

    private static SerializedFNamePath FindUniqueFNamePath(
        byte[] data,
        int[] packageNameIndices,
        int packageNameNumber,
        int[] assetNameIndices,
        int assetNameNumber,
        string exportName)
    {
        var matches = new List<SerializedFNamePath>();
        // ArticleDataPath payloads can begin at an odd byte offset after
        // variable-length strings in these CDOs. Search bytewise; the unique
        // match check below still fails closed if the same FName tuple occurs
        // more than once.
        for (var offset = 0; offset <= data.Length - 16; offset++)
        {
            var packageIndex = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset, 4));
            var packageNumber = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset + 4, 4));
            var assetIndex = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset + 8, 4));
            var assetNumber = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset + 12, 4));
            if (packageNameIndices.Contains(packageIndex) && packageNumber == packageNameNumber &&
                assetNameIndices.Contains(assetIndex) && assetNumber == assetNameNumber)
                matches.Add(new SerializedFNamePath(offset));
        }
        if (matches.Count != 1)
            throw new InvalidDataException(
                $"Expected one serialized ArticleDataPath FName pair in {exportName}; found {matches.Count}.");
        return matches[0];
    }

    private static (int[] Package, int[] Asset) FindNameIndices(
        string[] names,
        string packageName,
        string assetName)
    {
        var package = names.Select((name, index) => (name, index))
            .Where(item => item.name == packageName).Select(item => item.index).ToArray();
        var asset = names.Select((name, index) => (name, index))
            .Where(item => item.name == assetName).Select(item => item.index).ToArray();
        if (package.Length == 0 || asset.Length == 0)
            throw new InvalidDataException("Catalogued ArticleDataPath names are absent from the source CDO NameMap.");
        return (package, asset);
    }


    // Find name index

    private static int FindNameIndex(string[] names, string requested)
    {
        var index = Array.FindIndex(names, item => String.Equals(item, requested, StringComparison.Ordinal));
        if (index < 0) throw new InvalidDataException($"SmallScenario NameMap omitted required literal '{requested}'.");
        return index;
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
        BinaryPrimitives.WriteInt32LittleEndian(data.AsSpan(path.Offset, 4), packageIndex);
        BinaryPrimitives.WriteInt32LittleEndian(data.AsSpan(path.Offset + 4, 4), packageNumber);
        BinaryPrimitives.WriteInt32LittleEndian(data.AsSpan(path.Offset + 8, 4), assetIndex);
        BinaryPrimitives.WriteInt32LittleEndian(data.AsSpan(path.Offset + 12, 4), assetNumber);
    }


    // Check patched export rows

    private static void ValidateScenarioExportRows(ExportRow[] sourceRows, UAsset reopened, long headerDelta)
    {
        if (reopened.Exports.Count != sourceRows.Length)
            throw new InvalidDataException("Reopened SmallScenario package changed its ExportMap count.");
        for (var index = 0; index < sourceRows.Length; index++)
        {
            var actual = reopened.Exports[index];
            var expected = sourceRows[index];
            if (actual.SerialSize != expected.SerialSize ||
                actual.SerialOffset != expected.SerialOffset + headerDelta ||
                actual.ObjectName.ToString() != expected.ObjectName ||
                actual.ClassIndex.Index != expected.ClassIndex ||
                actual.SuperIndex.Index != expected.SuperIndex ||
                actual.TemplateIndex.Index != expected.TemplateIndex ||
                actual.OuterIndex.Index != expected.OuterIndex ||
                actual.GetType().FullName != expected.ExportType)
                throw new InvalidDataException(
                    $"SmallScenario export #{index + 1} metadata changed outside the required header offset adjustment.");
        }
    }


    // Capture names

    private static string[] SnapshotNames(UAsset asset) =>
        asset.GetNameMapIndexList()
            .Select((_, index) => asset.GetNameReference(index).ToString()).ToArray();


    // Capture package imports

    private static string SnapshotImports(UAsset asset) => String.Join("\0", asset.Imports.Select(item =>
        $"{item.ClassPackage}\u001f{item.ClassName}\u001f{item.ObjectName}\u001f{item.OuterIndex.Index}\u001f{item.PackageName}"));


    // Check append only names

    private static void ValidateAppendOnlyNames(string[] source, string[] output, IReadOnlySet<string> allowed)
    {
        if (output.Length < source.Length || !source.SequenceEqual(output.Take(source.Length)))
            throw new InvalidDataException("SmallScenario package serialization changed existing NameMap values or indices.");
        if (output.Skip(source.Length).Any(name => !allowed.Contains(name)))
            throw new InvalidDataException("SmallScenario package serialization appended an unrelated NameMap value.");
    }


    // Encode export offsets

    private static byte[] EncodeExportSerialPair(long size, long offset)
    {
        var bytes = new byte[16];
        BinaryPrimitives.WriteInt64LittleEndian(bytes.AsSpan(0, 8), size);
        BinaryPrimitives.WriteInt64LittleEndian(bytes.AsSpan(8, 8), offset);
        return bytes;
    }


    // Find an exact byte sequence

    private static int FindUniqueBytes(ReadOnlySpan<byte> data, ReadOnlySpan<byte> expected, string label)
    {
        var first = data.IndexOf(expected);
        if (first < 0 || data[(first + 1)..].IndexOf(expected) >= 0)
            throw new InvalidDataException($"Could not uniquely locate {label}.");
        return first;
    }


    // Check only range changed

    private static void EnsureOnlyRangeChanged(byte[] before, byte[] after, int start, int length, string message)
    {
        if (before.Length != after.Length || start < 0 || length < 0 || start + length > before.Length)
            throw new InvalidDataException(message);
        for (var index = 0; index < before.Length; index++)
            if ((index < start || index >= start + length) && before[index] != after[index])
                throw new InvalidDataException(message);
    }


    // Check only approved bytes changed

    private static void EnsureOnlyRangesChanged(
        byte[] before,
        byte[] after,
        IReadOnlyList<(int Start, int Length)> ranges,
        string message)
    {
        if (before.Length != after.Length || ranges.Any(range =>
                range.Start < 0 || range.Length < 0 || range.Start + range.Length > before.Length))
            throw new InvalidDataException(message);
        for (var index = 0; index < before.Length; index++)
            if (before[index] != after[index] &&
                !ranges.Any(range => index >= range.Start && index < range.Start + range.Length))
                throw new InvalidDataException(message);
    }


    // Copy opaque package sidecars

    private static void CopyScenarioOpaqueSidecars(string input, string temporaryOutput)
    {
        foreach (var extension in new[] { ".ubulk", ".uptnl" })
        {
            var source = Path.ChangeExtension(input, extension);
            var temporary = Path.ChangeExtension(temporaryOutput, extension);
            if (File.Exists(source))
            {
                File.Copy(source, temporary, overwrite: true);
                if (!File.ReadAllBytes(source).AsSpan().SequenceEqual(File.ReadAllBytes(temporary)))
                    throw new InvalidDataException($"SmallScenario patch failed to preserve its opaque {extension} sidecar.");
            }
            else if (File.Exists(temporary))
                throw new InvalidDataException($"UAssetAPI unexpectedly created an unrequested {extension} sidecar.");
        }
    }

    private static List<ReferenceHit> ReadReferenceTargets<TRequest>(
        UAsset asset,
        string inputPath,
        string virtualPackage,
        IReadOnlyList<TRequest> requests)
    {
        var results = new List<ReferenceHit>();
        using var reader = asset.PathToReader(inputPath, true);
        foreach (var request in requests)
        {
            var target = request switch
            {
                ReferenceLocationRequest inspect => new ReferenceTargetIdentity(
                    inspect.ExportIndex, inspect.ExportName, inspect.ReferenceProperty,
                    inspect.ReferenceIndex, inspect.ExpectedArrayCount, inspect.ExpectedSuperClass),
                ReferencePatchRequest patch => new ReferenceTargetIdentity(
                    patch.ExportIndex, patch.ExportName, patch.ReferenceProperty,
                    patch.ReferenceIndex, patch.ExpectedArrayCount, patch.ExpectedSuperClass),
                _ => throw new InvalidDataException("Unknown Blood Code reference request type."),
            };
            if (target.ExportIndex < 1 || target.ExportIndex > asset.Exports.Count)
                throw new InvalidDataException($"Blood Code reference export {target.ExportIndex} is outside the package export table.");
            var exportIndex = target.ExportIndex - 1;
            if (!String.Equals(asset.Exports[exportIndex].ObjectName.ToString(), target.ExportName, StringComparison.Ordinal))
                throw new InvalidDataException($"Export {target.ExportIndex} no longer has the expected task name {target.ExportName}.");
            if (target.ReferenceProperty == "ArticleBulkInfo.Articles")
            {
                var expectedSuperClass = target.ExpectedSuperClass ?? "BP_SmallScenarioGainBloodCodeBase_C";
                if (!IsValidatedScenarioSuperClass(expectedSuperClass))
                    throw new InvalidDataException($"{target.ExportName} uses an unvalidated SmallScenario reward parent {expectedSuperClass}.");
                var classIndex = asset.Exports[exportIndex].ClassIndex.Index - 1;
                if (classIndex < 0 || classIndex >= asset.Exports.Count)
                    throw new InvalidDataException("Companion grant must have a local Blueprint class.");
                asset.ParseExport(reader, classIndex, true);
                if (asset.Exports[classIndex] is not ClassExport cls ||
                    !cls.SuperStruct.IsImport() || cls.SuperStruct.ToImport(asset).ObjectName.ToString() != expectedSuperClass)
                    throw new InvalidDataException("Companion reward does not inherit the validated grant mechanism.");
                asset.Mappings.Schemas[cls.ObjectName.ToString()] = Usmap.GetSchemaFromStructExport(cls, asset.Mappings.AreFNamesCaseInsensitive);
            }
            if (asset.Exports[exportIndex] is not NormalExport)
                asset.ParseExport(reader, exportIndex, true);
            if (target.ReferenceProperty == "ArticleBulkInfo.Articles" && asset.Exports[exportIndex] is RawExport)
            {
                var normal = asset.Exports[exportIndex].ConvertToChildExport<NormalExport>();
                reader.BaseStream.Position = normal.SerialOffset;
                normal.Read(reader, checked((int)(normal.SerialOffset + normal.SerialSize)));
                asset.Exports[exportIndex] = normal;
            }
            if (asset.Exports[exportIndex] is not NormalExport export)
                throw new InvalidDataException($"Blood Code task {target.ExportName} could not be parsed.");
            var location = FindReference(export, target.ReferenceProperty, target.ReferenceIndex, target.ExpectedArrayCount);
            var path = ReadRawSoftPath(location.PathProperty);
            ValidateBloodCodePath(path);
            var nameMap = asset.GetNameMapIndexList().Select(name => name.ToString()).ToArray();
            results.Add(new ReferenceHit(
                virtualPackage, target.ExportIndex, export.ObjectName.ToString(), target.ReferenceProperty,
                target.ReferenceIndex, location.ArrayCount, path.PackagePath, path.AssetName,
                path.PackageNameValue, path.PackageNameNumber, path.AssetNameValue,
                path.AssetNameNumber, path.SubPathString,
                Array.IndexOf(nameMap, path.PackageNameValue), Array.IndexOf(nameMap, path.AssetNameValue)));
        }
        return results.OrderBy(item => item.ExportIndex).ThenBy(item => item.ReferenceIndex).ToList();
    }


    // Resolve the package reference

    private static ReferenceView FindReference(NormalExport export, string propertyName, int arrayIndex, int expectedArrayCount)
    {
        if (propertyName == "ArticleBulkInfo.Articles")
        {
            var reward = FindArticle(export, 0, arrayIndex);
            if (reward.ArticlesCount != expectedArrayCount)
                throw new InvalidDataException("Companion reward Articles count changed.");
            var bulk = export.Data.OfType<StructPropertyData>().Single(IsArticleBulkInfo);
            var articles = bulk.Value.OfType<ArrayPropertyData>().Single(property => property.Name.ToString() == "Articles");
            return new ReferenceView(articles, arrayIndex, reward.PathProperty, reward.ArticlesCount);
        }
        var arrays = export.Data.OfType<ArrayPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), propertyName, StringComparison.Ordinal)).ToArray();
        if (arrays.Length != 1)
            throw new InvalidDataException($"{export.ObjectName} must contain exactly one {propertyName} array.");
        var array = arrays[0];
        var values = array.Value.ToArray();
        if (values.Length != expectedArrayCount || arrayIndex < 0 || arrayIndex >= values.Length ||
            values[arrayIndex] is not SoftObjectPropertyData path)
            throw new InvalidDataException($"{export.ObjectName}.{propertyName} has an unexpected length or element type.");
        return new ReferenceView(array, arrayIndex, path, values.Length);
    }


    // Capture referenced exports

    private static Dictionary<int, string> SnapshotReferenceExports(
        UAsset asset,
        IReadOnlyDictionary<int, HashSet<(string Property, int Index, int Length)>> masks)
    {
        var result = new Dictionary<int, string>();
        for (var index = 0; index < asset.Exports.Count; index++)
        {
            var export = asset.Exports[index];
            if (export is RawExport raw)
            {
                result[index] = JsonSerializer.Serialize(new
                {
                    type = export.GetType().FullName,
                    name = export.ObjectName.ToString(),
                    serialSize = raw.SerialSize,
                    data = Convert.ToBase64String(raw.Data),
                });
                continue;
            }
            if (export is NormalExport normal)
            {
                var mask = new HashSet<PropertyData>(ReferenceEqualityComparer.Instance);
                if (masks.TryGetValue(index, out var coordinates))
                    foreach (var coordinate in coordinates)
                        mask.Add(FindReference(normal, coordinate.Property, coordinate.Index, coordinate.Length).PathProperty);
                result[index] = JsonSerializer.Serialize(normal.Data.Select(property => Canonicalize(property, mask)).ToArray());
                continue;
            }
            throw new InvalidDataException($"Quest export {index + 1} has an unsupported opaque type and cannot be preserved safely.");
        }
        return result;
    }


    // Validate reference patch requests

    private static void ValidateReferencePackageRequests(IEnumerable<string> virtualPackages)
    {
        var packages = virtualPackages.ToArray();
        if (packages.Length == 0 || packages.Distinct(StringComparer.OrdinalIgnoreCase).Count() != packages.Length)
            throw new InvalidDataException("Blood Code reference requests must contain unique packages.");
    }


    // Load the Unreal package

    private static UAsset LoadPackage(string inputPath, EngineVersion engineVersion, Usmap mappings)
    {
        var flags = CustomSerializationFlags.SkipParsingExports | CustomSerializationFlags.SkipPreloadDependencyLoading;
        return new UAsset(inputPath, true, engineVersion, mappings, flags);
    }


    // Register companion package schemas

    private static void RegisterCompanionSchemas(string workRoot, EngineVersion version, Usmap mappings)
    {
        foreach (var name in new[] { "BP_SmallScenarioBase", "BP_SmallScenarioGainBloodCodeBase", "BP_SmallScenarioGainBoosterBase" })
        {
            var input = Path.Combine(workRoot, "source", "Content", "Cinematics", "CinematicScene", "Scenario", "SmallScenario", "Blueprint", "Base", name + ".uasset");
            if (!File.Exists(input)) continue;
            var schemaAsset = LoadPackage(input, version, mappings);
            using var reader = schemaAsset.PathToReader(input, true);
            for (var index = 0; index < schemaAsset.Exports.Count; index++)
            {
                if (schemaAsset.Exports[index].GetExportClassType().ToString() != "BlueprintGeneratedClass") continue;
                schemaAsset.ParseExport(reader, index, true);
                if (schemaAsset.Exports[index] is not ClassExport cls)
                    throw new InvalidDataException("Companion Blueprint schema could not be parsed.");
                mappings.Schemas[cls.ObjectName.ToString()] = Usmap.GetSchemaFromStructExport(cls, mappings.AreFNamesCaseInsensitive);
            }
        }
    }


    // Read preserved package exports

    private static void ParsePreservedExports(
        UAsset asset,
        string packagePath,
        IReadOnlyList<BloodCodePatchRequest> assignments)
    {
        var expectedExports = assignments
            .Where(patch => patch.PreservedExportIndex is not null || patch.PreservedExportName is not null)
            .Select(patch =>
            {
                if (patch.PreservedExportIndex is null || String.IsNullOrWhiteSpace(patch.PreservedExportName))
                    throw new InvalidDataException("Preserved quest export identity must include its index and name.");
                return (Index: patch.PreservedExportIndex.Value, Name: patch.PreservedExportName);
            })
            .Distinct()
            .ToArray();
        if (expectedExports.Length == 0) return;

        using var reader = asset.PathToReader(packagePath, true);
        foreach (var expected in expectedExports)
        {
            if (expected.Index < 1 || expected.Index > asset.Exports.Count)
                throw new InvalidDataException($"Preserved quest export index {expected.Index} is outside the package export table.");
            var index = expected.Index - 1;
            var actualName = asset.Exports[index].ObjectName.ToString();
            if (!String.Equals(actualName, expected.Name, StringComparison.Ordinal))
                throw new InvalidDataException($"Quest export {expected.Index} is {actualName}, not the expected preserved task {expected.Name}.");
            if (asset.Exports[index] is RawExport)
                continue;
            if (asset.Exports[index] is not NormalExport)
                asset.ParseExport(reader, index, true);
            if (asset.Exports[index] is not NormalExport)
                throw new InvalidDataException($"Preserved quest export {expected.Name} could not be parsed for snapshot validation (type {asset.Exports[index].GetType().Name}).");
        }
    }


    // Read quest reward grants

    private static List<GrantHit> ReadPackageGrants(UAsset asset, string inputPath, string virtualPackage, string rewardPrefix)
    {
        var results = new List<GrantHit>();
        using var reader = asset.PathToReader(inputPath, true);
        for (var exportIndex = 0; exportIndex < asset.Exports.Count; exportIndex++)
        {
            if (!IsGainItem(asset.Exports[exportIndex].ObjectName.ToString())) continue;
            if (asset.Exports[exportIndex] is not NormalExport)
                asset.ParseExport(reader, exportIndex, true);
            if (asset.Exports[exportIndex] is not NormalExport export) continue;
            var bulkIndex = 0;
            foreach (var bulk in Walk(export.Data).OfType<StructPropertyData>().Where(IsArticleBulkInfo))
            {
                var articles = bulk.Value.OfType<ArrayPropertyData>()
                    .Where(property => String.Equals(property.Name.ToString(), "Articles", StringComparison.Ordinal))
                    .ToArray();
                if (articles.Length != 1)
                    throw new InvalidDataException($"{export.ObjectName} ArticleBulkInfo must contain one Articles array.");
                var articleValues = articles[0].Value.ToArray();
                for (var articleIndex = 0; articleIndex < articleValues.Length; articleIndex++)
                {
                    if (articleValues[articleIndex] is not StructPropertyData article)
                        throw new InvalidDataException($"{export.ObjectName} Articles[{articleIndex}] is not a struct.");
                    var paths = article.Value.OfType<SoftObjectPropertyData>()
                        .Where(property => String.Equals(property.Name.ToString(), "ArticleDataPath", StringComparison.Ordinal))
                        .ToArray();
                    if (paths.Length != 1)
                        throw new InvalidDataException($"{export.ObjectName} Articles[{articleIndex}] must have one ArticleDataPath.");
                    var softPath = ReadRawSoftPath(paths[0]);
                    if (!softPath.PackagePath.StartsWith(rewardPrefix, StringComparison.Ordinal)) continue;
                    ValidateRewardPath(softPath.PackagePath, softPath.AssetName, rewardPrefix, "quest reward");
                    var reward = ReadArticleReward(new ArticleView(bulkIndex, articleIndex, paths[0], article, articleValues.Length));
                    results.Add(new GrantHit(
                        virtualPackage,
                        exportIndex + 1,
                        export.ObjectName.ToString(),
                        bulkIndex,
                        articleIndex,
                        reward.RewardPackage,
                        reward.RewardAsset,
                        reward.Count,
                        reward.Notification,
                        softPath.PackageNameValue,
                        softPath.PackageNameNumber,
                        softPath.AssetNameValue,
                        softPath.AssetNameNumber,
                        softPath.SubPathString,
                        articleValues.Length));
                }
                bulkIndex++;
            }
        }
        return results.OrderBy(item => item.ExportIndex).ThenBy(item => item.BulkIndex).ThenBy(item => item.RewardIndex).ToList();
    }


    // Find the matching Article

    private static ArticleView FindArticle(NormalExport export, int bulkIndex, int rewardIndex)
    {
        var bulks = Walk(export.Data).OfType<StructPropertyData>().Where(IsArticleBulkInfo).ToArray();
        if (bulkIndex < 0 || bulkIndex >= bulks.Length)
            throw new InvalidDataException($"{export.ObjectName} has no ArticleBulkInfo at index {bulkIndex}.");
        var arrays = bulks[bulkIndex].Value.OfType<ArrayPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), "Articles", StringComparison.Ordinal)).ToArray();
        var values = arrays.Length == 1 ? arrays[0].Value.ToArray() : Array.Empty<PropertyData>();
        if (arrays.Length != 1 || rewardIndex < 0 || rewardIndex >= values.Length || values[rewardIndex] is not StructPropertyData article)
            throw new InvalidDataException($"{export.ObjectName} has no Articles[{rewardIndex}] at bulk index {bulkIndex}.");
        var paths = article.Value.OfType<SoftObjectPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), "ArticleDataPath", StringComparison.Ordinal)).ToArray();
        if (paths.Length != 1) throw new InvalidDataException("Blood Code article must contain exactly one ArticleDataPath.");
        return new ArticleView(bulkIndex, rewardIndex, paths[0], article, values.Length);
    }


    // Read the Article reward

    private static RewardInfo ReadArticleReward(ArticleView article)
    {
        var path = ReadRawSoftPath(article.PathProperty);
        var package = path.PackagePath;
        var assetName = path.AssetName;
        if (!String.Equals(assetName, package[(package.LastIndexOf('/') + 1)..], StringComparison.Ordinal))
            throw new InvalidDataException("Blood Code reward path must reference a package-level asset.");
        var count = article.Struct.Value.OfType<IntPropertyData>()
            .SingleOrDefault(property => String.Equals(property.Name.ToString(), "Count", StringComparison.Ordinal))
            ?? throw new InvalidDataException("ArticleDataPath is missing its integer Count.");
        var notification = article.Struct.Value.OfType<BoolPropertyData>()
            .SingleOrDefault(property => String.Equals(property.Name.ToString(), "bNotification", StringComparison.Ordinal))
            ?? throw new InvalidDataException("ArticleDataPath is missing bNotification.");
        return new RewardInfo(
            package, assetName, count.Value, notification.Value,
            path.PackageNameValue, path.PackageNameNumber,
            path.AssetNameValue, path.AssetNameNumber, path.SubPathString,
            article.ArticlesCount);
    }


    // Set soft path

    private static void SetSoftPath(UAsset asset, SoftObjectPropertyData property, string package, string assetName)
    {
        var pathValue = (FSoftObjectPath)property.Value!;
        pathValue.AssetPath = new FTopLevelAssetPath
        {
            // Keep names such as DA_BloodCode_Noah_01 as literal NameMap entries.
            // The explicit Number=0 prevents FName suffix parsing from treating _01 as a number.
            PackageName = new FName(asset, package, 0),
            AssetName = new FName(asset, assetName, 0),
        };
        property.Value = pathValue;
    }


    // Capture package exports

    private static Dictionary<int, string> SnapshotExports(
        UAsset asset,
        IReadOnlyDictionary<int, HashSet<(int Bulk, int Reward)>> masks,
        bool requireEveryExportSnapshotted = false,
        IReadOnlySet<int>? maskedNoMultipleGainExports = null)
    {
        var result = new Dictionary<int, string>();
        for (var index = 0; index < asset.Exports.Count; index++)
        {
            var export = asset.Exports[index];
            if (export is RawExport rawExport)
            {
                result[index] = JsonSerializer.Serialize(new
                {
                    type = export.GetType().FullName,
                    name = export.ObjectName.ToString(),
                    serialSize = rawExport.SerialSize,
                    data = Convert.ToBase64String(rawExport.Data),
                });
                continue;
            }
            IEnumerable<PropertyData>? properties = export switch
            {
                NormalExport normal => normal.Data,
                _ => null,
            };
            if (properties is null)
            {
                if (requireEveryExportSnapshotted)
                    throw new InvalidDataException($"Quest export {index + 1} has an unsupported opaque type and cannot be preserved safely.");
                result[index] = export.GetType().FullName + "|" + export.ObjectName;
                continue;
            }
            var mask = masks.TryGetValue(index, out var coordinates) && export is NormalExport normalExport
                ? coordinates.Select(pair => FindArticle(normalExport, pair.Bulk, pair.Reward).PathProperty).ToHashSet<PropertyData>(ReferenceEqualityComparer.Instance)
                : new HashSet<PropertyData>(ReferenceEqualityComparer.Instance);
            if (maskedNoMultipleGainExports?.Contains(index) == true && export is NormalExport repeatableTask)
                mask.Add(ReadNoMultipleGain(repeatableTask));
            result[index] = JsonSerializer.Serialize(properties.Select(property => Canonicalize(property, mask)).ToArray());
        }
        return result;
    }


    // Read the duplicate reward flag

    private static BoolPropertyData ReadNoMultipleGain(NormalExport task)
    {
        var flags = task.Data.OfType<BoolPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), "bNoMultipleGain", StringComparison.Ordinal))
            .ToArray();
        if (flags.Length != 1)
            throw new InvalidDataException($"{task.ObjectName} must contain exactly one top-level bNoMultipleGain flag.");
        return flags[0];
    }


    // Normalize serialized data

    private static object? Canonicalize(object? value, HashSet<PropertyData> masks, HashSet<object>? visited = null, int depth = 0)
    {
        if (value is null) return null;
        if (depth > 48) throw new InvalidDataException("Quest export property nesting exceeds its safety limit.");
        if (value is FName fname) return fname.ToString();
        if (value is FSoftObjectPath or FTopLevelAssetPath) return value.ToString();
        if (value is string or bool or byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal)
            return value;
        if (value is Enum enumValue) return enumValue.ToString();
        if (value is PropertyData property)
        {
            var raw = property.GetType().GetProperty("Value")?.GetValue(property);
            return new SortedDictionary<string, object?>(StringComparer.Ordinal)
            {
                ["type"] = property.GetType().FullName,
                ["name"] = property.Name.ToString(),
                ["value"] = masks.Contains(property) ? "<assigned-quest-article-path>" : Canonicalize(raw, masks, visited, depth + 1),
            };
        }
        if (value is IEnumerable enumerable)
        {
            visited ??= new HashSet<object>(ReferenceEqualityComparer.Instance);
            if (!value.GetType().IsValueType && !visited.Add(value)) return "<cycle>";
            try { return enumerable.Cast<object?>().Select(item => Canonicalize(item, masks, visited, depth + 1)).ToArray(); }
            finally { if (!value.GetType().IsValueType) visited.Remove(value); }
        }
        return value.ToString() ?? value.GetType().FullName;
    }


    // Compare package snapshots

    private static void ValidateSnapshots(
        Dictionary<int, string> expected,
        Dictionary<int, string> actual,
        IEnumerable<int> maskedExports,
        string featureName)
    {
        if (expected.Count != actual.Count || !expected.Keys.ToHashSet().SetEquals(actual.Keys))
            throw new InvalidDataException("Quest export inventory changed after serialization.");
        foreach (var item in expected)
            if (!String.Equals(item.Value, actual[item.Key], StringComparison.Ordinal))
                throw new InvalidDataException($"Quest export {item.Key + 1} changed outside the assigned {featureName} ArticleDataPath.");
    }


    // Check package export contents

    private static void ValidateExports((string Name, string? Type)[] expected, IList<Export> actual)
    {
        var observed = actual.Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();
        if (!expected.SequenceEqual(observed))
            throw new InvalidDataException("Quest package export identity, type, or order changed after serialization.");
    }


    // Copy additional sidecars

    private static void CopyAdditionalSidecars(string inputHeader, string temporaryHeader)
    {
        foreach (var extension in new[] { ".ubulk", ".uptnl" })
        {
            var source = Path.ChangeExtension(inputHeader, extension);
            var target = Path.ChangeExtension(temporaryHeader, extension);
            if (File.Exists(source) && !File.Exists(target)) File.Copy(source, target);
        }
    }


    // Check package header and sidecars

    private static void ValidateHeaderAndSidecar(string inputHeader, string outputHeader)
    {
        if (!File.Exists(outputHeader)) throw new InvalidDataException("UAssetAPI omitted a patched Blood Code .uasset header.");
        if (File.Exists(Path.ChangeExtension(inputHeader, ".uexp")) && !File.Exists(Path.ChangeExtension(outputHeader, ".uexp")))
            throw new InvalidDataException("UAssetAPI omitted the patched Blood Code .uexp sidecar.");
    }


    // Resolve an output path

    private static string ResolveOwnedPath(string workRoot, string value, bool mustExist)
    {
        var root = Path.GetFullPath(workRoot);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(root, path);
        if (relative == "." || relative == ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new InvalidDataException("Blood Code package path escaped its generation workspace.");
        if (mustExist && !File.Exists(path)) throw new FileNotFoundException("Blood Code package was not found.", path);
        return path;
    }


    // Collect package files

    private static string[] PackageFiles(string header) => new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" }
        .Select(extension => Path.ChangeExtension(header, extension)).ToArray();


    // Check the virtual package

    private static void ValidateVirtualPackage(string value, bool allowScenario = false)
    {
        var allowed = value.StartsWith("/Game/Quest/", StringComparison.Ordinal) ||
            (allowScenario && IsBloodCodeScenarioPackage(value));
        if (!allowed || value.Contains('\\') || value.EndsWith("/", StringComparison.Ordinal) || value.Split('/').Any(part => part is "." or ".."))
            throw new InvalidDataException("Blood Code source must be a virtual /Game/Quest package path.");
    }


    // Check Blood Code quest package

    private static bool IsBloodCodeScenarioPackage(string value) =>
        value.StartsWith(
            "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/BloodCode/",
            StringComparison.Ordinal) ||
        value.StartsWith(
            "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/CharacterEpisode/CharacterEpisodeLavinia/Present_c08010/",
            StringComparison.Ordinal) ||
        value.StartsWith(
            "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/CharacterEpisode/CharacterEpisodeLavinia/Present_c08030/",
            StringComparison.Ordinal);


    // Check the quest class metadata

    private static bool IsValidatedScenarioSuperClass(string value) => value is
        "BP_SmallScenarioBase_C" or
        "BP_SmallScenarioGainBloodCodeBase_C" or
        "BP_SmallScenarioGainBoosterBase_C";


    // Check Blood Code path

    private static void ValidateBloodCodePath(string package, string asset)
    {
        if (!package.StartsWith(BloodCodePrefix, StringComparison.Ordinal) ||
            !String.Equals(asset, package[(package.LastIndexOf('/') + 1)..], StringComparison.Ordinal))
            throw new InvalidDataException("Assigned reward must be a package-level Blood Code asset path.");
    }


    // Check the reward Article path

    private static void ValidateRewardPath(string package, string asset, string prefix, string featureName)
    {
        if (!package.StartsWith(prefix, StringComparison.Ordinal) ||
            !String.Equals(asset, package[(package.LastIndexOf('/') + 1)..], StringComparison.Ordinal))
            throw new InvalidDataException($"Assigned {featureName} must be a package-level asset under {prefix}.");
    }


    // Check for a GainItem task

    private static bool IsGainItem(string name) =>
        name.Replace("_", "", StringComparison.Ordinal).Contains("GainItem", StringComparison.OrdinalIgnoreCase);


    // Check for ArticleBulkInfo data

    private static bool IsArticleBulkInfo(StructPropertyData property) =>
        String.Equals(property.StructType.ToString(), "ArticleBulkInfo", StringComparison.Ordinal) ||
        String.Equals(property.Name.ToString(), "ArticleBulkInfo", StringComparison.Ordinal);


    // Visit nested property values

    private static IEnumerable<PropertyData> Walk(IEnumerable<PropertyData> properties)
    {
        foreach (var property in properties)
        {
            yield return property;
            if (property is StructPropertyData structure)
            {
                foreach (var child in Walk(structure.Value)) yield return child;
            }
            else if (property is ArrayPropertyData array)
            {
                foreach (var child in array.Value.OfType<PropertyData>())
                    foreach (var nested in Walk(new[] { child })) yield return nested;
            }
        }
    }

    private static IEnumerable<(SoftObjectPropertyData Property, string Path)> FindArticlePaths(
        IEnumerable<PropertyData> properties,
        string parentPath)
    {
        var index = 0;
        foreach (var property in properties)
        {
            var name = property.Name.ToString();
            var segment = String.IsNullOrWhiteSpace(name) || name == "None" ? $"[{index}]" : name;
            var propertyPath = String.IsNullOrEmpty(parentPath) ? segment : parentPath + "." + segment;
            if (property is SoftObjectPropertyData soft && String.Equals(name, "ArticlePath", StringComparison.Ordinal))
                yield return (soft, propertyPath);
            if (property is StructPropertyData structure)
            {
                foreach (var nested in FindArticlePaths(structure.Value, propertyPath)) yield return nested;
            }
            else if (property is ArrayPropertyData array)
            {
                var childIndex = 0;
                foreach (var child in array.Value.OfType<PropertyData>())
                {
                    foreach (var nested in FindArticlePaths(new[] { child }, $"{propertyPath}[{childIndex}]"))
                        yield return nested;
                    childIndex++;
                }
            }
            index++;
        }
    }


    // Read the serialized asset path

    private static SoftPath ReadRawSoftPath(SoftObjectPropertyData property)
    {
        var path = property.Value is FSoftObjectPath softPath
            ? softPath
            : throw new InvalidDataException("ArticleDataPath is not an FSoftObjectPath.");
        var packageName = path.AssetPath.PackageName;
        var assetName = path.AssetPath.AssetName;
        return new SoftPath(
            packageName.ToString(), assetName.ToString(),
            packageName.Value.ToString(), packageName.Number,
            assetName.Value.ToString(), assetName.Number,
            path.SubPathString?.ToString() ?? String.Empty);
    }


    // Check Blood Code path

    private static void ValidateBloodCodePath(SoftPath path)
    {
        if (!String.Equals(path.AssetName, path.PackagePath[(path.PackagePath.LastIndexOf('/') + 1)..], StringComparison.Ordinal))
            throw new InvalidDataException("Blood Code ArticleDataPath is not a package-level soft object path.");
    }


    // Describe a serialized asset path

    private sealed record SoftPath(
        string PackagePath,
        string AssetName,
        string PackageNameValue,
        int PackageNameNumber,
        string AssetNameValue,
        int AssetNameNumber,
        string SubPathString);
    // Describe grant hit
    private sealed record GrantHit(
        string Package,
        int ExportIndex,
        string ExportName,
        int BulkIndex,
        int RewardIndex,
        string RewardPackage,
        string RewardAsset,
        int Count,
        bool Notification,
        string PackageNameValue,
        int PackageNameNumber,
        string AssetNameValue,
        int AssetNameNumber,
        string SubPathString,
        int ArticlesCount);
    // Scan failure
    private sealed record ScanFailure(string Package, int? ExportIndex, string ErrorType, string Message);
    // Scan report
    private sealed record ScanReport(
        int QuestAssetCount,
        int ParsedGainItemCount,
        int GrantCount,
        int FailureCount,
        IReadOnlyList<GrantHit> Grants,
        IReadOnlyList<ScanFailure> Failures);


    // Read the inspection manifest

    private sealed record InspectManifest(IReadOnlyList<PackageInspectionRequest> Packages);
    // Describe Article identity data
    private sealed record ArticleIdentity(
        string VirtualPackage, string InputFile, int ExportIndex, string LiteralFName, int FNameNumber, int ClassIndex);
    // Describe package inspection request
    private sealed record PackageInspectionRequest(string VirtualPackage, string InputFile);
    // Describe package inspection
    private sealed record PackageInspection(string VirtualPackage, string InputFile, int GrantCount, IReadOnlyList<GrantHit> Grants);
    // Describe reference inspection manifest
    private sealed record ReferenceInspectManifest(IReadOnlyList<ReferencePackageInspectRequest> Packages);
    // Describe package inspection request
    private sealed record ReferencePackageInspectRequest(string VirtualPackage, string InputFile, IReadOnlyList<ReferenceLocationRequest> References);
    // Describe inspected package data
    private sealed record ReferencePackageInspection(string VirtualPackage, string InputFile, int ReferenceCount, IReadOnlyList<ReferenceHit> References);
    // Describe location lookup request
    private sealed record ReferenceLocationRequest(
        int ExportIndex,
        string ExportName,
        string ReferenceProperty,
        int ReferenceIndex,
        int ExpectedArrayCount,
        string ExpectedPackage,
        string ExpectedAsset,
        string ExpectedPackageNameValue,
        int ExpectedPackageNameNumber,
        string ExpectedAssetNameValue,
        int ExpectedAssetNameNumber,
        string? ExpectedSuperClass = null);
    // Describe reference patch manifest
    private sealed record ReferencePatchManifest(IReadOnlyList<ReferencePackagePatchRequest> Packages);
    // Describe package patch request
    private sealed record ReferencePackagePatchRequest(string VirtualPackage, string InputFile, string OutputFile, IReadOnlyList<ReferencePatchRequest> Assignments);
    // Describe reference patch request
    private sealed record ReferencePatchRequest(
        int ExportIndex,
        string ExportName,
        string ReferenceProperty,
        int ReferenceIndex,
        int ExpectedArrayCount,
        string ExpectedPackage,
        string ExpectedAsset,
        string ExpectedPackageNameValue,
        int ExpectedPackageNameNumber,
        string ExpectedAssetNameValue,
        int ExpectedAssetNameNumber,
        string AssignedPackage,
        string AssignedAsset,
        string? ExpectedSuperClass = null);
    // Describe a matched reference
    private sealed record ReferenceHit(
        string Package,
        int ExportIndex,
        string ExportName,
        string ReferenceProperty,
        int ReferenceIndex,
        int ArrayCount,
        string RewardPackage,
        string RewardAsset,
        string PackageNameValue,
        int PackageNameNumber,
        string AssetNameValue,
        int AssetNameNumber,
        string SubPathString,
        int PackageNameMapIndex,
        int AssetNameMapIndex);
    // Describe reference target identity
    private sealed record ReferenceTargetIdentity(
        int ExportIndex,
        string ExportName,
        string ReferenceProperty,
        int ReferenceIndex,
        int ExpectedArrayCount,
        string? ExpectedSuperClass);
    // Describe reference details
    private sealed record ReferenceView(ArrayPropertyData Array, int ArrayIndex, SoftObjectPropertyData PathProperty, int ArrayCount);
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
    // Describe raw quest patch data
    private sealed record RawScenarioPatch(
        int ExportIndex,
        int SourceDataStart,
        SerializedFNamePath Path,
        byte[] PatchedData);
    // Describe reference patch results
    private sealed record ReferencePackagePatchResult(
        string VirtualPackage,
        string InputFile,
        string OutputFile,
        int PatchedReferences,
        IReadOnlyList<string> OutputFiles);
    // Apply the patch manifest
    private sealed record PatchManifest(IReadOnlyList<PackagePatchRequest> Packages);
    // Describe package patch request
    private sealed record PackagePatchRequest(string VirtualPackage, string InputFile, string OutputFile, IReadOnlyList<BloodCodePatchRequest> Assignments);
    // Describe Blood Code patch request
    private sealed record BloodCodePatchRequest(
        int ExportIndex,
        string ExportName,
        int BulkIndex,
        int RewardIndex,
        string ExpectedPackage,
        string ExpectedAsset,
        int ExpectedCount,
        bool ExpectedNotification,
        string AssignedPackage,
        string AssignedAsset,
        bool PatchArticlePath = true,
        string? ExpectedSubPathString = null,
        string? ExpectedPackageNameValue = null,
        int? ExpectedPackageNameNumber = null,
        string? ExpectedAssetNameValue = null,
        int? ExpectedAssetNameNumber = null,
        int? ExpectedArticlesCount = null,
        int? PreservedExportIndex = null,
        string? PreservedExportName = null,
        bool RequireStrictExportSnapshots = false,
        bool AllowMultipleGain = false);
    // Describe package patch results
    private sealed record PackagePatchResult(
        string VirtualPackage,
        string InputFile,
        string OutputFile,
        int PatchedSlots,
        IReadOnlyList<string> OutputFiles,
        int PackageLoads,
        double SourceLoadSeconds,
        double SourceInspectionSeconds,
        double MutationSeconds,
        double SerializationSeconds,
        double ReopenValidationSeconds,
        double OutputMoveSeconds);
    // Describe Article view
    private sealed record ArticleView(
        int BulkIndex,
        int RewardIndex,
        SoftObjectPropertyData PathProperty,
        StructPropertyData Struct,
        int ArticlesCount);
    // Describe reward data
    private sealed record RewardInfo(
        string RewardPackage,
        string RewardAsset,
        int Count,
        bool Notification,
        string PackageNameValue,
        int PackageNameNumber,
        string AssetNameValue,
        int AssetNameNumber,
        string SubPathString,
        int ArticlesCount);
}
