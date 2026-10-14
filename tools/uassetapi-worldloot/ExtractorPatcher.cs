using System.Collections;
using System.Security.Cryptography;
using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class ExtractorPatcher
{
    private const string BaseTable = "DT_BoosterRecipe";
    private const string DlcTable = "DT_BoosterRecipe_DLC";
    private const string BoosterPrefix = "/Game/Blueprint/Article/Booster/";
    private static readonly string[] RequiredRecipeFields =
        { "CreateArticleDataPath", "CreateNum", "RawMaterials", "UseExp", "AcquireFlag", "VisibleFlag", "MaterialEnemyImage" };
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };


    // Inspect

    public static int Inspect(string[] args)
    {
        try
        {
            var root = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engine = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<InspectionManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Extractor inspection manifest is empty.");
            if (manifest.Packages.Count is < 1 or > 2)
                throw new InvalidDataException("Extractor inspection must contain one or both recipe tables.");
            var results = new List<TableInspection>();
            foreach (var request in manifest.Packages)
            {
                ValidateVirtualPackage(request.VirtualPackage);
                var input = ResolveReadablePath(request.InputFile);
                var asset = new UAsset(input, engine, mappings);
                var rows = ReadRows(ResolveTable(asset, request.VirtualPackage, input));
                results.Add(new TableInspection(request.VirtualPackage, input, rows.Count, rows));
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


    // Patch

    public static int Patch(string[] args)
    {
        var committed = new List<string>();
        try
        {
            var root = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engine = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<PatchManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Extractor patch manifest is empty.");
            if (manifest.Packages.Count is < 1 or > 2)
                throw new InvalidDataException("Extractor patch must contain one or both recipe tables.");
            var results = new List<TablePatchResult>();
            foreach (var request in manifest.Packages)
            {
                ValidateVirtualPackage(request.VirtualPackage);
                if (request.Assignments.Count == 0 && !request.OneMaterialEach)
                    throw new InvalidDataException("Extractor patch table contains no assignments.");
                var input = ResolveReadablePath(request.InputFile);
                var output = ResolveOwnedPath(root, request.OutputFile);
                if (String.Equals(input, output, StringComparison.OrdinalIgnoreCase) || File.Exists(output))
                    throw new IOException("Extractor output must be a new staged package separate from source.");
                var asset = new UAsset(input, engine, mappings);
                var table = ResolveTable(asset, request.VirtualPackage, input);
                var before = ReadRows(table);
                if (before.Count != request.ExpectedRowCount)
                    throw new InvalidDataException($"{request.VirtualPackage} row count changed from its catalog.");
                var beforeByName = before.ToDictionary(row => row.RowName, StringComparer.OrdinalIgnoreCase);
                var beforeNames = before.Select(row => row.RowName).ToArray();
                var beforeSnapshots = before.ToDictionary(row => row.RowName, row => row.SnapshotSha256, StringComparer.OrdinalIgnoreCase);
                if (request.ExpectedRows is null || request.ExpectedRows.Count != before.Count)
                    throw new InvalidDataException($"{request.VirtualPackage} has an incomplete expected-row inventory.");
                for (var rowIndex = 0; rowIndex < before.Count; rowIndex++)
                {
                    var expected = request.ExpectedRows[rowIndex];
                    var row = before[rowIndex];
                    if (row.RowName != expected.RowName || row.RowIndex != expected.RowIndex ||
                        row.ArticlePath != expected.ExpectedArticlePath ||
                        row.ImmutableFingerprintSha256 != expected.ExpectedImmutableFingerprint)
                        throw new InvalidDataException($"Extractor source row {expected.RowName} no longer matches its bound catalog metadata.");
                }
                var patches = new Dictionary<string, RowPatch>(StringComparer.OrdinalIgnoreCase);
                foreach (var patch in request.Assignments)
                {
                    if (!patches.TryAdd(patch.RowName, patch))
                        throw new InvalidDataException($"Duplicate Extractor destination row {patch.RowName}.");
                    if (!beforeByName.TryGetValue(patch.RowName, out var row) ||
                        row.RowIndex != patch.RowIndex || row.ArticlePath != patch.ExpectedArticlePath ||
                        row.ImmutableFingerprintSha256 != patch.ExpectedImmutableFingerprint)
                        throw new InvalidDataException($"Extractor row {patch.RowName} no longer matches its bound catalog metadata.");
                    ValidateBoosterPath(patch.AssignedArticlePath);
                }
                foreach (var patch in request.Assignments)
                    SetArticlePath(asset, beforeByName[patch.RowName], patch.AssignedArticlePath);
                if (request.OneMaterialEach)
                    foreach (var row in before)
                        SetMaterialCostsToOne(row);

                var outputDirectory = Path.GetDirectoryName(output)!;
                Directory.CreateDirectory(outputDirectory);
                var temporary = Path.Combine(outputDirectory,
                    Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(output));
                try
                {
                    asset.Write(temporary);
                    CopyAdditionalSidecars(input, temporary);
                    ValidateHeaderAndSidecar(input, temporary);
                    var reopened = new UAsset(temporary, engine, mappings);
                    ValidateRows(before, ReadRows(ResolveTable(reopened, request.VirtualPackage, temporary)), beforeNames,
                        patches, beforeSnapshots, request.OneMaterialEach);
                    var temporaryFiles = PackageFiles(temporary).Where(File.Exists).ToArray();
                    if (!temporaryFiles.Contains(temporary, StringComparer.OrdinalIgnoreCase))
                        throw new InvalidDataException("UAssetAPI omitted the Extractor .uasset header.");
                    var outputs = temporaryFiles.Select(path => Path.ChangeExtension(output, Path.GetExtension(path))).ToArray();
                    if (outputs.Any(File.Exists)) throw new IOException("Refusing to overwrite Extractor output sidecars.");
                    for (var index = 0; index < temporaryFiles.Length; index++)
                    {
                        File.Move(temporaryFiles[index], outputs[index]);
                        committed.Add(outputs[index]);
                    }
                    var final = new UAsset(output, engine, mappings);
                    var finalRows = ReadRows(ResolveTable(final, request.VirtualPackage, output));
                    ValidateRows(before, finalRows, beforeNames, patches, beforeSnapshots, request.OneMaterialEach);
                    var patchedRows = before.Count(original =>
                        original.ArticlePath != finalRows[original.RowIndex].ArticlePath ||
                        (request.OneMaterialEach && original.RawMaterialQuantities.Any(quantity => quantity != 1)));
                    results.Add(new TablePatchResult(request.VirtualPackage, input, output,
                        request.ExpectedRowCount, patchedRows, outputs, finalRows));
                }
                finally
                {
                    foreach (var path in PackageFiles(temporary))
                        try { File.Delete(path); } catch (IOException) { }
                }
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


    // Check package table rows

    private static void ValidateRows(IReadOnlyList<RowInspection> before, IReadOnlyList<RowInspection> after,
        IReadOnlyList<string> beforeNames, IReadOnlyDictionary<string, RowPatch> patches,
        IReadOnlyDictionary<string, string> beforeSnapshots, bool oneMaterialEach)
    {
        if (before.Count != after.Count || !beforeNames.SequenceEqual(after.Select(row => row.RowName), StringComparer.Ordinal))
            throw new InvalidDataException("Extractor DataTable row count, identity, or ordering changed after serialization.");
        var afterByName = after.ToDictionary(row => row.RowName, StringComparer.OrdinalIgnoreCase);
        foreach (var original in before)
        {
            var reopened = afterByName[original.RowName];
            if (oneMaterialEach)
            {
                var expectedArticle = patches.TryGetValue(original.RowName, out var freePatch)
                    ? freePatch.AssignedArticlePath
                    : original.ArticlePath;
                if (reopened.RowIndex != original.RowIndex || reopened.ArticlePath != expectedArticle ||
                    reopened.StructuralFingerprintSha256 != original.StructuralFingerprintSha256 ||
                    reopened.HazeCost != original.HazeCost ||
                    reopened.RawMaterialQuantities.Length != original.RawMaterialQuantities.Length ||
                    reopened.RawMaterialQuantities.Any(quantity => quantity != 1))
                    throw new InvalidDataException($"Extractor row {original.RowName} failed reward or 1-per-material cost validation.");
                continue;
            }
            if (!patches.TryGetValue(original.RowName, out var patch))
            {
                if (reopened.ArticlePath != original.ArticlePath || reopened.ImmutableFingerprintSha256 != original.ImmutableFingerprintSha256 ||
                    reopened.SnapshotSha256 != beforeSnapshots[original.RowName])
                    throw new InvalidDataException($"Unassigned Extractor row {original.RowName} changed.");
                continue;
            }
            if (reopened.RowIndex != original.RowIndex || reopened.ArticlePath != patch.AssignedArticlePath ||
                reopened.ImmutableFingerprintSha256 != original.ImmutableFingerprintSha256)
                throw new InvalidDataException($"Extractor row {original.RowName} failed field-level reward validation.");
        }
    }


    // Read package table rows

    private static List<RowInspection> ReadRows(DataTableExport table)
    {
        var result = new List<RowInspection>();
        var index = 0;
        foreach (var raw in (IEnumerable)table.Table!.Data)
        {
            if (raw is not StructPropertyData structure)
                throw new InvalidDataException("Extractor DataTable row was not parsed as a struct.");
            var rowName = structure.Name.ToString();
            if (String.IsNullOrWhiteSpace(rowName)) throw new InvalidDataException("Extractor row has an empty name.");
            foreach (var required in RequiredRecipeFields)
            {
                var matches = structure.Value.Count(property => String.Equals(property.Name.ToString(), required, StringComparison.Ordinal));
                if (matches != 1) throw new InvalidDataException($"{rowName} must contain exactly one {required} field.");
            }
            var paths = Walk(structure.Value).OfType<SoftObjectPropertyData>()
                .Where(property => String.Equals(property.Name.ToString(), "CreateArticleDataPath", StringComparison.Ordinal)).ToArray();
            if (paths.Length != 1) throw new InvalidDataException($"{rowName} must contain exactly one CreateArticleDataPath.");
            var path = ReadSoftPath(paths[0]);
            ValidateBoosterPath(path);
            var rawMaterials = structure.Value.Single(property => String.Equals(
                property.Name.ToString(), "RawMaterials", StringComparison.Ordinal));
            var quantities = ReadRawMaterialQuantities(rawMaterials, rowName);
            var materialPaths = ReadRawMaterialPaths(rawMaterials, rowName);
            var haze = structure.Value.Single(property => property.Name.ToString() == "UseExp") as IntPropertyData
                ?? throw new InvalidDataException($"{rowName}.UseExp is not an integer.");
            result.Add(new RowInspection(index++, rowName, path, Hash(Snapshot(structure, maskArticlePath: false)),
                Hash(Snapshot(structure, maskArticlePath: true)),
                Hash(Snapshot(structure, maskArticlePath: true, maskExtractionCosts: true)),
                quantities, materialPaths, haze.Value, structure));
        }
        if (result.Select(row => row.RowName).Distinct(StringComparer.OrdinalIgnoreCase).Count() != result.Count)
            throw new InvalidDataException("Extractor DataTable has duplicate row names.");
        return result;
    }


    // Find the target data table

    private static DataTableExport ResolveTable(UAsset asset, string virtualPackage, string inputPath)
    {
        var tableName = virtualPackage[(virtualPackage.LastIndexOf('/') + 1)..];
        var matches = asset.Exports.Select((export, index) => (export, index))
            .Where(item => String.Equals(item.export.ObjectName.ToString(), tableName, StringComparison.Ordinal)).ToArray();
        if (matches.Length == 1 && matches[0].export is not DataTableExport)
        {
            using var reader = asset.PathToReader(inputPath, true);
            asset.ParseExport(reader, matches[0].index, true);
        }
        var tables = asset.Exports.OfType<DataTableExport>()
            .Where(export => String.Equals(export.ObjectName.ToString(), tableName, StringComparison.Ordinal)).ToArray();
        if (tables.Length != 1 || tables[0].Table?.Data is null)
            throw new InvalidDataException($"Expected one parsed DataTable export named {tableName}; found {tables.Length}.");
        return tables[0];
    }


    // Set the reward Article path

    private static void SetArticlePath(UAsset asset, RowInspection row, string articlePath)
    {
        var matches = Walk(row.Structure!.Value).OfType<SoftObjectPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), "CreateArticleDataPath", StringComparison.Ordinal)).ToArray();
        if (matches.Length != 1) throw new InvalidDataException($"{row.RowName} has an ambiguous CreateArticleDataPath.");
        var (package, name) = SplitArticlePath(articlePath);
        var value = (FSoftObjectPath)matches[0].Value!;
        value.AssetPath = new FTopLevelAssetPath
        {
            PackageName = new FName(asset, package),
            AssetName = new FName(asset, name),
        };
        matches[0].Value = value;
    }


    // Read serialized material amounts

    private static int[] ReadRawMaterialQuantities(PropertyData rawMaterials, string rowName)
    {
        if (rawMaterials is not ArrayPropertyData array)
            throw new InvalidDataException($"{rowName}.RawMaterials is not an array.");
        var quantities = new List<int>();
        foreach (var item in array.Value)
        {
            if (item is not StructPropertyData material)
                throw new InvalidDataException($"{rowName}.RawMaterials contains a non-struct entry.");
            var useNum = material.Value.Where(property => String.Equals(
                property.Name.ToString(), "UseNum", StringComparison.Ordinal)).ToArray();
            if (useNum.Length != 1 || useNum[0] is not IntPropertyData quantity)
                throw new InvalidDataException($"{rowName}.RawMaterials entry must have one integer UseNum.");
            quantities.Add(quantity.Value);
        }
        return quantities.ToArray();
    }


    // Read serialized material paths

    private static string[] ReadRawMaterialPaths(PropertyData rawMaterials, string rowName)
    {
        if (rawMaterials is not ArrayPropertyData array)
            throw new InvalidDataException($"{rowName}.RawMaterials is not an array.");
        var paths = new List<string>();
        foreach (var item in array.Value)
        {
            if (item is not StructPropertyData material)
                throw new InvalidDataException($"{rowName}.RawMaterials contains a non-struct entry.");
            var matches = Walk(material.Value).OfType<SoftObjectPropertyData>()
                .Where(property => property.Name.ToString() == "ArticleDataPath").ToArray();
            if (matches.Length != 1)
                throw new InvalidDataException($"{rowName}.RawMaterials entry must contain one ArticleDataPath.");
            var path = ReadSoftPath(matches[0]);
            if (!path.StartsWith("/Game/Blueprint/Article/", StringComparison.Ordinal))
                throw new InvalidDataException($"{rowName} references a non-Article Booster ingredient: {path}.");
            paths.Add(path);
        }
        return paths.ToArray();
    }


    // Set material costs to one

    private static void SetMaterialCostsToOne(RowInspection row)
    {
        var matches = row.Structure!.Value.Where(property => String.Equals(
            property.Name.ToString(), "RawMaterials", StringComparison.Ordinal)).ToArray();
        if (matches.Length != 1 || matches[0] is not ArrayPropertyData array)
            throw new InvalidDataException($"{row.RowName} has an invalid RawMaterials array.");
        foreach (var item in array.Value)
        {
            if (item is not StructPropertyData material)
                throw new InvalidDataException($"{row.RowName}.RawMaterials contains a non-struct entry.");
            var useNum = material.Value.Where(property => String.Equals(
                property.Name.ToString(), "UseNum", StringComparison.Ordinal)).ToArray();
            if (useNum.Length != 1 || useNum[0] is not IntPropertyData quantity)
                throw new InvalidDataException($"{row.RowName}.RawMaterials entry must have one integer UseNum.");
            quantity.Value = 1;
        }
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


    // Capture

    private static string Snapshot(StructPropertyData row, bool maskArticlePath, bool maskExtractionCosts = false) =>
        JsonSerializer.Serialize(new { Row = row.Name.ToString(), Properties = row.Value.Select(property => Canonicalize(
            property, maskArticlePath, maskExtractionCosts)).ToArray() });


    // Calculate the data hash

    private static string Hash(string value) => Convert.ToHexString(SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(value))).ToLowerInvariant();


    // Normalize serialized data

    private static object? Canonicalize(object? value, bool maskArticlePath, bool maskExtractionCosts,
        bool insideRawMaterials = false)
    {
        if (value is null) return null;
        if (value is PropertyData property)
        {
            var raw = property.GetType().GetProperty("Value")?.GetValue(property);
            var mutable = maskArticlePath && property is SoftObjectPropertyData &&
                String.Equals(property.Name.ToString(), "CreateArticleDataPath", StringComparison.Ordinal);
            var rawMaterials = maskExtractionCosts && property is ArrayPropertyData &&
                String.Equals(property.Name.ToString(), "RawMaterials", StringComparison.Ordinal);
            var quantity = maskExtractionCosts && insideRawMaterials && property is IntPropertyData &&
                String.Equals(property.Name.ToString(), "UseNum", StringComparison.Ordinal);
            var haze = maskExtractionCosts && !insideRawMaterials && property is IntPropertyData &&
                String.Equals(property.Name.ToString(), "UseExp", StringComparison.Ordinal);
            var masked = mutable ? "<extractor-booster-reward>" : quantity || haze ? "<booster-material-cost-test>" : null;
            return new { Type = property.GetType().FullName, Name = property.Name.ToString(),
                Value = masked ?? Canonicalize(raw, maskArticlePath, maskExtractionCosts, insideRawMaterials || rawMaterials) };
        }
        if (value is FName name) return name.ToString();
        if (value is FSoftObjectPath softPath) return softPath.ToString();
        if (value is FTopLevelAssetPath topLevel) return topLevel.ToString();
        if (value is string or bool or byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal) return value;
        if (value is Enum enumValue) return enumValue.ToString();
        if (value is IEnumerable enumerable) return enumerable.Cast<object?>().Select(item => Canonicalize(
            item, maskArticlePath, maskExtractionCosts, insideRawMaterials)).ToArray();
        return value.ToString() ?? value.GetType().FullName;
    }


    // Read a serialized asset path

    private static string ReadSoftPath(SoftObjectPropertyData property)
    {
        var path = (FSoftObjectPath)property.Value!;
        var package = path.AssetPath.PackageName.ToString();
        var asset = path.AssetPath.AssetName.ToString();
        if (String.IsNullOrWhiteSpace(package) || String.IsNullOrWhiteSpace(asset) || !String.Equals(package[(package.LastIndexOf('/') + 1)..], asset, StringComparison.Ordinal))
            throw new InvalidDataException("CreateArticleDataPath must point to a package-level Article asset.");
        return package;
    }


    // Check the virtual package

    private static void ValidateVirtualPackage(string package)
    {
        if (package is not "/Game/Blueprint/GameSystem/ItemSynthetic/Booster/DT_BoosterRecipe" and
            not "/Game/Blueprint/GameSystem/ItemSynthetic/Booster/DT_BoosterRecipe_DLC")
            throw new InvalidDataException("Only the two catalogued Booster recipe DataTables can be inspected or patched.");
    }


    // Check the Booster asset path

    private static void ValidateBoosterPath(string path)
    {
        if (!path.StartsWith(BoosterPrefix, StringComparison.Ordinal) ||
            !path[(path.LastIndexOf('/') + 1)..].StartsWith("DA_Booster_", StringComparison.Ordinal))
            throw new InvalidDataException($"Extractor reward is not a canonical Booster Article: {path}.");
        _ = SplitArticlePath(path);
    }

    private static (string Package, string Asset) SplitArticlePath(string path)
    {
        if (!path.StartsWith("/Game/Blueprint/Article/", StringComparison.Ordinal) || path.Contains('\\') ||
            path.EndsWith('/') || path.Contains("/../", StringComparison.Ordinal) || path.Contains("/./", StringComparison.Ordinal) ||
            path[(path.LastIndexOf('/') + 1)..].Contains('.', StringComparison.Ordinal))
            throw new InvalidDataException($"Malformed package-level Article path: {path}.");
        return (path, path[(path.LastIndexOf('/') + 1)..]);
    }


    // Resolve readable path

    private static string ResolveReadablePath(string value)
    {
        var path = Path.GetFullPath(value);
        if (!File.Exists(path)) throw new FileNotFoundException("Extractor DataTable header was not found.", path);
        return path;
    }


    // Resolve an output path

    private static string ResolveOwnedPath(string root, string value)
    {
        var absoluteRoot = Path.GetFullPath(root);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(absoluteRoot, path);
        if (relative is "." or ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new InvalidDataException("Extractor output escaped its generation workspace.");
        return path;
    }


    // Collect package files

    private static string[] PackageFiles(string header) => new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" }
        .Select(extension => Path.ChangeExtension(header, extension)).ToArray();


    // Copy additional sidecars

    private static void CopyAdditionalSidecars(string input, string output)
    {
        foreach (var extension in new[] { ".ubulk", ".uptnl" })
        {
            var source = Path.ChangeExtension(input, extension);
            var target = Path.ChangeExtension(output, extension);
            if (File.Exists(source) && !File.Exists(target)) File.Copy(source, target);
        }
    }


    // Check package header and sidecars

    private static void ValidateHeaderAndSidecar(string input, string output)
    {
        if (!File.Exists(output)) throw new InvalidDataException("UAssetAPI omitted the Extractor .uasset header.");
        if (File.Exists(Path.ChangeExtension(input, ".uexp")) && !File.Exists(Path.ChangeExtension(output, ".uexp")))
            throw new InvalidDataException("UAssetAPI omitted the Extractor .uexp sidecar.");
    }


    // Describe package inspection results

    private sealed record InspectionManifest(IReadOnlyList<InspectionRequest> Packages);
    // Describe an inspection request
    private sealed record InspectionRequest(string VirtualPackage, string InputFile);
    // Apply the patch manifest
    private sealed record PatchManifest(IReadOnlyList<PatchTableRequest> Packages);
    // Patch table request
    private sealed record PatchTableRequest(string VirtualPackage, string InputFile, string OutputFile, int ExpectedRowCount,
        bool OneMaterialEach, IReadOnlyList<ExpectedRecipeRow> ExpectedRows, IReadOnlyList<RowPatch> Assignments);
    // Describe an expected recipe row
    private sealed record ExpectedRecipeRow(int RowIndex, string RowName, string ExpectedArticlePath,
        string ExpectedImmutableFingerprint);
    // Describe a table row change
    private sealed record RowPatch(int RowIndex, string RowName, string ExpectedArticlePath,
        string ExpectedImmutableFingerprint, string AssignedArticlePath);
    // Describe inspected table data
    private sealed record TableInspection(string VirtualPackage, string InputFile, int RowCount, IReadOnlyList<RowInspection> Rows);
    // Describe table patch results
    private sealed record TablePatchResult(string VirtualPackage, string InputFile, string OutputFile, int RowCount,
        int PatchedRows, IReadOnlyList<string> OutputFiles, IReadOnlyList<RowInspection> Rows);
    // Describe an inspected row
    private sealed record RowInspection(int RowIndex, string RowName, string ArticlePath, string SnapshotSha256,
        string ImmutableFingerprintSha256, string StructuralFingerprintSha256, int[] RawMaterialQuantities,
        string[] RawMaterialPaths, int HazeCost,
        [property: System.Text.Json.Serialization.JsonIgnore] StructPropertyData? Structure = null);
}
