using System.Text.Json;
using System.Collections;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class DefensiveGadgetPatcher
{
    private const string BaseGadgetPrefix = "/Game/Blueprint/Article/Gadget/Defence/";
    private const string DlcGadgetPrefix = "/Game/Blueprint/Article/Gadget/DLC/";
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
            if (!Directory.Exists(contentRoot)) throw new DirectoryNotFoundException("Content root does not exist.");
            if (!File.Exists(mappingsPath)) throw new FileNotFoundException("CV2.usmap mapping was not found.", mappingsPath);

            var mappings = new Usmap(mappingsPath);
            var packages = Directory.EnumerateFiles(contentRoot, "*.uasset", SearchOption.AllDirectories)
                .OrderBy(path => path, StringComparer.OrdinalIgnoreCase).ToArray();
            var grants = new List<GrantHit>();
            var failures = new List<ScanFailure>();
            var parsedExports = 0;
            foreach (var inputPath in packages)
            {
                var relative = Path.GetRelativePath(contentRoot, inputPath).Replace(Path.DirectorySeparatorChar, '/');
                var virtualPackage = "/Game/" + Path.ChangeExtension(relative, null);
                try
                {
                    var asset = LoadPackage(inputPath, engineVersion, mappings);
                    using var reader = asset.PathToReader(inputPath, true);
                    for (var exportIndex = 0; exportIndex < asset.Exports.Count; exportIndex++)
                    {
                        try
                        {
                            if (asset.Exports[exportIndex] is not NormalExport)
                                asset.ParseExport(reader, exportIndex, true);
                            if (asset.Exports[exportIndex] is not NormalExport export) continue;
                            parsedExports++;
                            grants.AddRange(ReadGadgetGrants(export, virtualPackage, exportIndex + 1));
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
            Console.WriteLine(JsonSerializer.Serialize(
                new ScanReport(packages.Length, parsedExports, grants.Count, failures.Count, grants, failures), JsonOptions));
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
    {
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<GadgetInspectManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Defensive Gadget inspection manifest is empty.");
            if (manifest.Packages.Count == 0 || manifest.Packages.Select(item => item.VirtualPackage).Distinct(StringComparer.OrdinalIgnoreCase).Count() != manifest.Packages.Count)
                throw new InvalidDataException("Defensive Gadget inspection requires unique packages.");
            var results = manifest.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase)
                .Select(request =>
                {
                    ValidateVirtualPackage(request.VirtualPackage);
                    var input = ResolveOwnedPath(workRoot, request.InputFile, true);
                    var asset = LoadPackage(input, engineVersion, mappings);
                    var grants = new List<GrantHit>();
                    for (var index = 0; index < asset.Exports.Count; index++)
                    {
                        if (asset.Exports[index] is not NormalExport)
                            using (var reader = asset.PathToReader(input, true)) asset.ParseExport(reader, index, true);
                        if (asset.Exports[index] is NormalExport export)
                            grants.AddRange(ReadGadgetGrants(export, request.VirtualPackage, index + 1));
                    }
                    return new GadgetPackageInspection(request.VirtualPackage, input, grants.Count, grants);
                }).ToArray();
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
    {
        var outputs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<GadgetPatchManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Defensive Gadget patch manifest is empty.");
            if (manifest.Packages.Count == 0 || manifest.Packages.Select(item => item.VirtualPackage).Distinct(StringComparer.OrdinalIgnoreCase).Count() != manifest.Packages.Count)
                throw new InvalidDataException("Defensive Gadget patch requires unique packages.");
            var results = new List<GadgetPackagePatchResult>();
            foreach (var request in manifest.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase))
            {
                var result = PatchPackage(workRoot, request, engineVersion, mappings);
                foreach (var output in result.OutputFiles) outputs.Add(output);
                results.Add(result);
            }
            Console.WriteLine(JsonSerializer.Serialize(results, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            foreach (var path in outputs) try { File.Delete(path); } catch (IOException) { }
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Patch one asset package

    private static GadgetPackagePatchResult PatchPackage(string workRoot, GadgetPatchPackageRequest request, EngineVersion engineVersion, Usmap mappings)
    {
        ValidateVirtualPackage(request.VirtualPackage);
        var input = ResolveOwnedPath(workRoot, request.InputFile, true);
        var output = ResolveOwnedPath(workRoot, request.OutputFile, false);
        if (String.Equals(input, output, StringComparison.OrdinalIgnoreCase) || File.Exists(output))
            throw new IOException("Defensive Gadget output must be a new staged package file.");
        var asset = LoadPackage(input, engineVersion, mappings);
        var grants = new Dictionary<(int Export, int Bulk, int Reward), GrantHit>();
        for (var index = 0; index < asset.Exports.Count; index++)
        {
            if (asset.Exports[index] is not NormalExport)
                using (var reader = asset.PathToReader(input, true)) asset.ParseExport(reader, index, true);
            if (asset.Exports[index] is NormalExport export)
                foreach (var hit in ReadGadgetGrants(export, request.VirtualPackage, index + 1))
                    grants[(hit.ExportIndex, hit.BulkIndex, hit.RewardIndex)] = hit;
        }
        if (request.Assignments.Count == 0) throw new InvalidDataException("Patch request has no Defensive Gadget assignment.");
        var seen = new HashSet<(int Export, int Bulk, int Reward)>();
        var selected = new List<(GadgetGrantPatch Patch, GrantHit Hit, SoftObjectPropertyData Property)>();
        foreach (var patch in request.Assignments)
        {
            var key = (patch.ExportIndex, patch.BulkIndex, patch.RewardIndex);
            if (!seen.Add(key) || !grants.TryGetValue(key, out var actual) || actual.ExportName != patch.ExportName ||
                actual.RewardPackage != patch.ExpectedPackage || actual.RewardAsset != patch.ExpectedAsset ||
                actual.Count != patch.ExpectedCount || actual.Notification != patch.ExpectedNotification)
                throw new InvalidDataException($"{request.VirtualPackage} source grant does not match {patch.ExportName}[{patch.BulkIndex}:{patch.RewardIndex}].");
            ValidateGadgetPath(patch.AssignedPackage, patch.AssignedAsset);
            if (asset.Exports[patch.ExportIndex - 1] is not NormalExport export)
                throw new InvalidDataException("Defensive Gadget grant export is not a parsed NormalExport.");
            var article = FindArticle(export, patch.BulkIndex, patch.RewardIndex);
            selected.Add((patch, actual, article.PathProperty));
        }
        var identities = asset.Exports.Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();
        var beforeSnapshots = SnapshotExports(asset, selected.ToDictionary(
            item => (item.Patch.ExportIndex - 1, item.Patch.BulkIndex, item.Patch.RewardIndex), item => item.Property));

        foreach (var item in selected)
        {
            var path = (FSoftObjectPath)item.Property.Value!;
            path.AssetPath = new FTopLevelAssetPath
            {
                PackageName = new FName(asset, item.Patch.AssignedPackage),
                AssetName = new FName(asset, item.Patch.AssignedAsset),
            };
            item.Property.Value = path;
        }

        var outputDir = Path.GetDirectoryName(output)!;
        Directory.CreateDirectory(outputDir);
        var tempHeader = Path.Combine(outputDir, Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(output));
        var moved = new List<string>();
        try
        {
            asset.Write(tempHeader);
            foreach (var extension in new[] { ".ubulk", ".uptnl" })
            {
                var source = Path.ChangeExtension(input, extension);
                var target = Path.ChangeExtension(tempHeader, extension);
                if (File.Exists(source) && !File.Exists(target)) File.Copy(source, target);
            }
            ValidateReopenedPackage(tempHeader, request, engineVersion, mappings, identities, beforeSnapshots, selected);
            var tempFiles = PackageFiles(tempHeader).Where(File.Exists).ToArray();
            if (!File.Exists(tempHeader) || (File.Exists(Path.ChangeExtension(input, ".uexp")) && !File.Exists(Path.ChangeExtension(tempHeader, ".uexp"))))
                throw new InvalidDataException("UAssetAPI omitted a required package sidecar.");
            var outputFiles = tempFiles.Select(path => Path.ChangeExtension(output, Path.GetExtension(path))).ToArray();
            if (outputFiles.Any(File.Exists)) throw new IOException("Refusing to overwrite a Defensive Gadget staged sidecar.");
            for (var index = 0; index < tempFiles.Length; index++)
            {
                File.Move(tempFiles[index], outputFiles[index]);
                moved.Add(outputFiles[index]);
            }
            ValidateReopenedPackage(output, request, engineVersion, mappings, identities, beforeSnapshots, selected);
            return new GadgetPackagePatchResult(request.VirtualPackage, input, output, selected.Count, moved, true);
        }
        catch
        {
            foreach (var path in moved) try { File.Delete(path); } catch (IOException) { }
            throw;
        }
        finally
        {
            foreach (var path in PackageFiles(tempHeader)) try { File.Delete(path); } catch (IOException) { }
        }
    }


    // Reopen and validate the package

    private static void ValidateReopenedPackage(
        string packageHeader,
        GadgetPatchPackageRequest request,
        EngineVersion engineVersion,
        Usmap mappings,
        (string Name, string? Type)[] identities,
        Dictionary<int, string> beforeSnapshots,
        IReadOnlyList<(GadgetGrantPatch Patch, GrantHit Hit, SoftObjectPropertyData Property)> selected)
    {
        var reopened = LoadPackage(packageHeader, engineVersion, mappings);
        ParseAllExports(reopened, packageHeader);
        ValidateExportInventory(identities, reopened.Exports);
        var afterSnapshots = SnapshotExports(reopened, selected.ToDictionary(
            item => (item.Patch.ExportIndex - 1, item.Patch.BulkIndex, item.Patch.RewardIndex), item =>
                FindArticle((NormalExport)reopened.Exports[item.Patch.ExportIndex - 1], item.Patch.BulkIndex, item.Patch.RewardIndex).PathProperty));
        ValidateSnapshots(beforeSnapshots, afterSnapshots);
        var reopenedGrants = new Dictionary<(int, int, int), GrantHit>();
        for (var index = 0; index < reopened.Exports.Count; index++)
        {
            if (reopened.Exports[index] is NormalExport export)
                foreach (var hit in ReadGadgetGrants(export, request.VirtualPackage, index + 1))
                    reopenedGrants[(hit.ExportIndex, hit.BulkIndex, hit.RewardIndex)] = hit;
        }
        foreach (var patch in request.Assignments)
        {
            if (!reopenedGrants.TryGetValue((patch.ExportIndex, patch.BulkIndex, patch.RewardIndex), out var actual) ||
                actual.RewardPackage != patch.AssignedPackage || actual.RewardAsset != patch.AssignedAsset ||
                actual.Count != patch.ExpectedCount || actual.Notification != patch.ExpectedNotification)
                throw new InvalidDataException("Reopened package does not contain the requested gadget path with preserved reward fields.");
        }
    }


    // Read Defensive Gadget rewards

    private static List<GrantHit> ReadGadgetGrants(NormalExport export, string package, int exportIndex)
    {
        var grants = new List<GrantHit>();
        var recognizedPaths = new HashSet<SoftObjectPropertyData>(ReferenceEqualityComparer.Instance);
        var bulkIndex = 0;
        foreach (var bulk in Walk(export.Data).OfType<StructPropertyData>().Where(IsArticleBulkInfo))
        {
            var arrays = bulk.Value.OfType<ArrayPropertyData>().Where(item => item.Name.ToString() == "Articles").ToArray();
            if (arrays.Length != 1) throw new InvalidDataException($"{export.ObjectName} ArticleBulkInfo must contain one Articles array.");
            var values = arrays[0].Value.ToArray();
            for (var rewardIndex = 0; rewardIndex < values.Length; rewardIndex++)
            {
                if (values[rewardIndex] is not StructPropertyData article) throw new InvalidDataException("Articles entry is not a struct.");
                var paths = article.Value.OfType<SoftObjectPropertyData>().Where(item => item.Name.ToString() == "ArticleDataPath").ToArray();
                if (paths.Length != 1) throw new InvalidDataException("Article entry must contain exactly one ArticleDataPath.");
                recognizedPaths.Add(paths[0]);
                var softPath = paths[0].Value is FSoftObjectPath parsedPath ? parsedPath : throw new InvalidDataException("ArticleDataPath is not an FSoftObjectPath.");
                var rewardPackage = softPath.AssetPath.PackageName.ToString();
                if (!IsGadgetPackage(rewardPackage)) continue;
                var rewardAsset = softPath.AssetPath.AssetName.ToString();
                if (String.IsNullOrWhiteSpace(rewardAsset)) throw new InvalidDataException("Defensive Gadget reward has no asset name.");
                var count = article.Value.OfType<IntPropertyData>().SingleOrDefault(item => item.Name.ToString() == "Count")
                    ?? throw new InvalidDataException("Defensive Gadget ArticleDataPath is missing integer Count.");
                var notification = article.Value.OfType<BoolPropertyData>().SingleOrDefault(item => item.Name.ToString() == "bNotification")
                    ?? throw new InvalidDataException("Defensive Gadget ArticleDataPath is missing bNotification.");
                grants.Add(new GrantHit(package, exportIndex, export.ObjectName.ToString(), bulkIndex, rewardIndex,
                    rewardPackage, rewardAsset, count.Value, notification.Value, "ArticleBulkInfo", true));
            }
            bulkIndex++;
        }
        var structures = Walk(export.Data).OfType<StructPropertyData>().ToArray();
        foreach (var pathProperty in Walk(export.Data).OfType<SoftObjectPropertyData>().Where(item => item.Name.ToString() == "ArticleDataPath"))
        {
            if (recognizedPaths.Contains(pathProperty)) continue;
            var softPath = pathProperty.Value;
            if (!IsGadgetPackage(softPath.AssetPath.PackageName.ToString())) continue;
            var owner = structures.FirstOrDefault(structure => structure.Value.Any(property => ReferenceEquals(property, pathProperty)));
            var count = owner?.Value.OfType<IntPropertyData>().SingleOrDefault(item => item.Name.ToString() == "Count");
            var notification = owner?.Value.OfType<BoolPropertyData>().SingleOrDefault(item => item.Name.ToString() == "bNotification");
            grants.Add(new GrantHit(package, exportIndex, export.ObjectName.ToString(), -1, -1,
                softPath.AssetPath.PackageName.ToString(), softPath.AssetPath.AssetName.ToString(),
                count?.Value ?? -1, notification?.Value ?? false,
                owner?.StructType.ToString() ?? owner?.Name.ToString() ?? "unresolved", false));
        }
        return grants;
    }


    // Find the matching Article

    private static ArticleView FindArticle(NormalExport export, int bulkIndex, int rewardIndex)
    {
        var bulks = Walk(export.Data).OfType<StructPropertyData>().Where(IsArticleBulkInfo).ToArray();
        if (bulkIndex < 0 || bulkIndex >= bulks.Length) throw new InvalidDataException("ArticleBulkInfo index is invalid.");
        var arrays = bulks[bulkIndex].Value.OfType<ArrayPropertyData>().Where(item => item.Name.ToString() == "Articles").ToArray();
        if (arrays.Length != 1) throw new InvalidDataException("Article target must contain one Articles array.");
        var values = arrays[0].Value.ToArray();
        if (rewardIndex < 0 || rewardIndex >= values.Length || values[rewardIndex] is not StructPropertyData article)
            throw new InvalidDataException("Defensive Gadget ArticleDataPath target no longer exists.");
        var properties = article.Value.OfType<SoftObjectPropertyData>().Where(item => item.Name.ToString() == "ArticleDataPath").ToArray();
        if (properties.Length != 1) throw new InvalidDataException("Article target must contain one ArticleDataPath.");
        return new ArticleView(properties[0]);
    }


    // Visit nested property values

    private static IEnumerable<PropertyData> Walk(IEnumerable<PropertyData> properties)
    {
        foreach (var property in properties)
        {
            yield return property;
            if (property is StructPropertyData structure)
                foreach (var child in Walk(structure.Value)) yield return child;
            else if (property is ArrayPropertyData array)
                foreach (var child in array.Value.OfType<PropertyData>())
                    foreach (var nested in Walk(new[] { child })) yield return nested;
        }
    }


    // Read every package export

    private static void ParseAllExports(UAsset asset, string packagePath)
    {
        using var reader = asset.PathToReader(packagePath, true);
        for (var index = 0; index < asset.Exports.Count; index++)
            if (asset.Exports[index] is not NormalExport) asset.ParseExport(reader, index, true);
    }


    // Capture package exports

    private static Dictionary<int, string> SnapshotExports(
        UAsset asset,
        IReadOnlyDictionary<(int Export, int Bulk, int Reward), SoftObjectPropertyData> maskedPaths)
    {
        var result = new Dictionary<int, string>();
        for (var index = 0; index < asset.Exports.Count; index++)
        {
            if (asset.Exports[index] is not NormalExport export)
            {
                result[index] = asset.Exports[index].GetType().FullName + "|" + asset.Exports[index].ObjectName;
                continue;
            }
            var masks = maskedPaths.Where(item => item.Key.Export == index).Select(item => item.Value)
                .ToHashSet<PropertyData>(ReferenceEqualityComparer.Instance);
            result[index] = JsonSerializer.Serialize(export.Data.Select(property => Canonicalize(property, masks)).ToArray());
        }
        return result;
    }


    // Normalize serialized data

    private static object? Canonicalize(object? value, HashSet<PropertyData> masks, HashSet<object>? visited = null, int depth = 0)
    {
        if (value is null) return null;
        if (depth > 48) throw new InvalidDataException("Gadget grant export nesting exceeds its safety limit.");
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
                ["value"] = masks.Contains(property) ? "<assigned-gadget-path>" : Canonicalize(raw, masks, visited, depth + 1),
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

    private static void ValidateSnapshots(Dictionary<int, string> expected, Dictionary<int, string> actual)
    {
        if (expected.Count != actual.Count || !expected.Keys.ToHashSet().SetEquals(actual.Keys))
            throw new InvalidDataException("Gadget grant export inventory changed after serialization.");
        foreach (var item in expected)
            if (!String.Equals(item.Value, actual[item.Key], StringComparison.Ordinal))
                throw new InvalidDataException($"Export {item.Key + 1} changed outside assigned Defensive Gadget ArticleDataPath fields.");
    }


    // Check package exports

    private static void ValidateExportInventory((string Name, string? Type)[] expected, IList<Export> actual)
    {
        var observed = actual.Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();
        if (!expected.SequenceEqual(observed))
            throw new InvalidDataException("Quest package export identity, type, or order changed after serialization.");
    }


    // Load the Unreal package

    private static UAsset LoadPackage(string input, EngineVersion version, Usmap mappings) =>
        new(input, true, version, mappings, CustomSerializationFlags.SkipParsingExports | CustomSerializationFlags.SkipPreloadDependencyLoading);


    // Check for ArticleBulkInfo data

    private static bool IsArticleBulkInfo(StructPropertyData property) => property.StructType.ToString() == "ArticleBulkInfo" || property.Name.ToString() == "ArticleBulkInfo";
    // Collect package files
    private static string[] PackageFiles(string header) => new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" }.Select(ext => Path.ChangeExtension(header, ext)).ToArray();


    // Resolve an output path

    private static string ResolveOwnedPath(string root, string value, bool mustExist)
    {
        var fullRoot = Path.GetFullPath(root);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(fullRoot, path);
        if (relative == "." || relative == ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new InvalidDataException("Defensive Gadget package path escaped the supplied workspace.");
        if (mustExist && !File.Exists(path)) throw new FileNotFoundException("Defensive Gadget package was not found.", path);
        return path;
    }


    // Check the virtual package

    private static void ValidateVirtualPackage(string value)
    {
        if (!value.StartsWith("/Game/Quest/", StringComparison.Ordinal) || value.Contains('\\') || value.EndsWith('/'))
            throw new InvalidDataException("Defensive Gadget grants must be in a virtual /Game/Quest package.");
    }


    // Check gadget path

    private static void ValidateGadgetPath(string package, string asset)
    {
        if (!IsGadgetPackage(package) || String.IsNullOrWhiteSpace(asset))
            throw new InvalidDataException("Assigned reward must be a BASE or DLC Defensive Gadget ArticleDataPath.");
    }


    // Check gadget package

    private static bool IsGadgetPackage(string package) =>
        package.StartsWith(BaseGadgetPrefix, StringComparison.Ordinal) ||
        package.StartsWith(DlcGadgetPrefix, StringComparison.Ordinal);


    // Describe grant hit

    private sealed record GrantHit(string Package, int ExportIndex, string ExportName, int BulkIndex, int RewardIndex,
        string RewardPackage, string RewardAsset, int Count, bool Notification, string ParentStruct, bool ArticleBulkInfo);
    // Scan failure
    private sealed record ScanFailure(string Package, int? ExportIndex, string ErrorType, string Message);
    // Scan report
    private sealed record ScanReport(int PackageCount, int ParsedExportCount, int GrantCount, int FailureCount,
        IReadOnlyList<GrantHit> Grants, IReadOnlyList<ScanFailure> Failures);
    // Describe gadget inspect manifest
    private sealed record GadgetInspectManifest(IReadOnlyList<GadgetInspectPackageRequest> Packages);
    // Describe gadget inspect package request
    private sealed record GadgetInspectPackageRequest(string VirtualPackage, string InputFile);
    // Describe gadget package inspection
    private sealed record GadgetPackageInspection(string VirtualPackage, string InputFile, int GrantCount, IReadOnlyList<GrantHit> Grants);
    // Describe gadget patch manifest
    private sealed record GadgetPatchManifest(IReadOnlyList<GadgetPatchPackageRequest> Packages);
    // Describe gadget patch package request
    private sealed record GadgetPatchPackageRequest(string VirtualPackage, string InputFile, string OutputFile, IReadOnlyList<GadgetGrantPatch> Assignments);
    // Read gadget grant patch
    private sealed record GadgetGrantPatch(int ExportIndex, string ExportName, int BulkIndex, int RewardIndex, string ExpectedPackage,
        string ExpectedAsset, int ExpectedCount, bool ExpectedNotification, string AssignedPackage, string AssignedAsset);
    // Describe gadget package patch result
    private sealed record GadgetPackagePatchResult(string VirtualPackage, string InputFile, string OutputFile, int PatchedSlots, IReadOnlyList<string> OutputFiles, bool ReopenedAndValidated);
    // Describe Article view
    private sealed record ArticleView(SoftObjectPropertyData PathProperty);
}
