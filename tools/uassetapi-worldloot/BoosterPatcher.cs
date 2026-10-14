using System.Collections;
using System.Text.Json;
using System.Text.RegularExpressions;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class BoosterPatcher
{
    private const string TableName = "DT_GainBoosterSmallScenario";
    private const string RowStructName = "STR_GainItemSmallScenario";
    private const string BoosterPrefix = "/Game/Blueprint/Article/Booster/";
    private static readonly Regex BoosterFlag = new(@"(?<![A-Za-z0-9_])(Booster_[A-Za-z0-9_]+)\s*==\s*false", RegexOptions.CultureInvariant);
    private static readonly Regex Mastery = new(@"\bBloodCodeMastery\.([A-Za-z0-9_]+)\s*>=\s*(Rank[0-9]+_Master)\b", RegexOptions.CultureInvariant);
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
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<InspectionManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Booster inspection manifest is empty.");
            if (manifest.Packages.Count != 1)
                throw new InvalidDataException("Booster inspection must contain exactly one DataTable package.");
            var request = manifest.Packages[0];
            ValidateVirtualPackage(request.VirtualPackage);
            var inputPath = ResolveReadablePath(request.InputFile);
            var asset = new UAsset(inputPath, engineVersion, mappings);
            var rows = ReadRows(ResolveTable(asset, inputPath));
            Console.WriteLine(JsonSerializer.Serialize(new InspectionResult(request.VirtualPackage, inputPath, rows.Count, rows), JsonOptions));
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
        string? outputHeader = null;
        var committed = new List<string>();
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<PatchManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Booster patch manifest is empty.");
            if (manifest.Packages.Count != 1)
                throw new InvalidDataException("Booster patch must contain exactly one DataTable package.");
            var request = manifest.Packages[0];
            ValidateVirtualPackage(request.VirtualPackage);
            if (request.Assignments.Count == 0)
                throw new InvalidDataException("Booster patch contains no changed rows.");

            var inputPath = ResolveReadablePath(request.InputFile);
            outputHeader = ResolveOwnedPath(workRoot, request.OutputFile, mustExist: false);
            if (String.Equals(inputPath, outputHeader, StringComparison.OrdinalIgnoreCase) || File.Exists(outputHeader))
                throw new IOException("Booster output must be a new staged package separate from its source.");
            var asset = new UAsset(inputPath, engineVersion, mappings);
            var table = ResolveTable(asset, inputPath);
            var beforeRows = ReadRows(table);
            if (beforeRows.Count != 102)
                throw new InvalidDataException($"Expected exactly 102 Booster rows, found {beforeRows.Count}.");
            var beforeNames = beforeRows.Select(row => row.RowName).ToArray();
            var beforeByName = ToRowMap(beforeRows);
            var masks = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var assignmentsByRow = new Dictionary<string, BoosterPatchRequest>(StringComparer.OrdinalIgnoreCase);

            foreach (var patch in request.Assignments)
            {
                if (!masks.Add(patch.RowName))
                    throw new InvalidDataException($"Booster patch contains duplicate destination row {patch.RowName}.");
                if (!assignmentsByRow.TryAdd(patch.RowName, patch))
                    throw new InvalidDataException($"Booster patch contains duplicate destination row {patch.RowName}.");
                ValidateBoosterIdentity(patch.AssignedStoryFlag, patch.AssignedPackage, patch.AssignedAsset);
                var row = FindRow(beforeByName, patch.RowName);
                if (row.RowIndex != patch.RowIndex || row.EvaluationFormula != patch.ExpectedFormula ||
                    row.MasteryOwner != patch.MasteryOwner || row.MasteryRank != patch.MasteryRank ||
                    row.BloodCodePackage != patch.ExpectedBloodCodePackage || row.BloodCodeAsset != patch.ExpectedBloodCodeAsset ||
                    row.BoosterStoryFlag != patch.ExpectedStoryFlag || row.BoosterPackage != patch.ExpectedPackage ||
                    row.BoosterAsset != patch.ExpectedAsset || row.Count != patch.ExpectedCount ||
                    row.Notification != patch.ExpectedNotification)
                    throw new InvalidDataException($"Booster source row {patch.RowName} no longer matches its versioned catalog entry.");
                if (!String.Equals(MatchFlag(patch.ExpectedFormula), patch.ExpectedStoryFlag, StringComparison.Ordinal))
                    throw new InvalidDataException($"Booster source formula flag does not match its reward bundle in {patch.RowName}.");
            }

            var beforeSnapshots = beforeRows.ToDictionary(row => row.RowName, row => Snapshot(row, maskMutableFields: false), StringComparer.OrdinalIgnoreCase);
            foreach (var patch in request.Assignments)
            {
                var row = FindRow(beforeByName, patch.RowName);
                ReplaceFormulaFlag(row, patch.ExpectedStoryFlag, patch.AssignedStoryFlag);
                SetBoosterPath(asset, row, patch.AssignedPackage, patch.AssignedAsset);
            }

            var outputDirectory = Path.GetDirectoryName(outputHeader)!;
            Directory.CreateDirectory(outputDirectory);
            var tempHeader = Path.Combine(outputDirectory,
                Path.GetFileNameWithoutExtension(outputHeader) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(outputHeader));
            var validationSchemaFiles = new List<string>();
            try
            {
                asset.Write(tempHeader);
                CopyAdditionalSidecars(inputPath, tempHeader);
                ValidateHeaderAndSidecar(inputPath, tempHeader);
                CopyRowStructForValidation(inputPath, tempHeader, validationSchemaFiles);
                var reopened = new UAsset(tempHeader, engineVersion, mappings);
                var reopenedRows = ReadRows(ResolveTable(reopened, tempHeader));
                ValidateRows(beforeRows, reopenedRows, beforeNames, assignmentsByRow, beforeSnapshots);
                var tempFiles = PackageFiles(tempHeader).Where(File.Exists).ToArray();
                var outputFiles = tempFiles.Select(path => Path.ChangeExtension(outputHeader, Path.GetExtension(path))).ToArray();
                if (outputFiles.Any(File.Exists))
                    throw new IOException("Refusing to overwrite an existing Booster staged sidecar.");
                for (var index = 0; index < tempFiles.Length; index++)
                {
                    File.Move(tempFiles[index], outputFiles[index]);
                    committed.Add(outputFiles[index]);
                }

                var finalAsset = new UAsset(outputHeader, engineVersion, mappings);
                var finalRows = ReadRows(ResolveTable(finalAsset, outputHeader));
                ValidateRows(beforeRows, finalRows, beforeNames, assignmentsByRow, beforeSnapshots);
                Console.WriteLine(JsonSerializer.Serialize(new PatchResult(
                    request.VirtualPackage, inputPath, outputHeader, request.Assignments.Count, outputFiles, finalRows), JsonOptions));
                return 0;
            }
            finally
            {
                foreach (var path in PackageFiles(tempHeader))
                    try { File.Delete(path); } catch (IOException) { }
                foreach (var path in validationSchemaFiles)
                    try { File.Delete(path); } catch (IOException) { }
            }
        }
        catch (Exception exception)
        {
            foreach (var path in committed)
                try { File.Delete(path); } catch (IOException) { }
            if (outputHeader is not null)
                foreach (var path in PackageFiles(outputHeader))
                    if (committed.Contains(path, StringComparer.OrdinalIgnoreCase))
                        try { File.Delete(path); } catch (IOException) { }
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Check package table rows

    private static void ValidateRows(
        IReadOnlyList<RowInspection> before,
        IReadOnlyList<RowInspection> after,
        IReadOnlyList<string> beforeNames,
        IReadOnlyDictionary<string, BoosterPatchRequest> changed,
        IReadOnlyDictionary<string, string> beforeSnapshots)
    {
        if (before.Count != after.Count || !beforeNames.SequenceEqual(after.Select(row => row.RowName), StringComparer.Ordinal))
            throw new InvalidDataException("Booster DataTable row count, identity, or ordering changed after serialization.");
        var afterByName = ToRowMap(after);
        foreach (var original in before)
        {
            var reopened = FindRow(afterByName, original.RowName);
            if (!changed.TryGetValue(original.RowName, out var patch))
            {
                if (!String.Equals(beforeSnapshots[original.RowName], Snapshot(reopened, maskMutableFields: false), StringComparison.Ordinal))
                    throw new InvalidDataException($"Unassigned Booster row {original.RowName} changed after serialization.");
                continue;
            }
            var expectedFormula = ReplaceFormulaFlag(original.EvaluationFormula, patch.ExpectedStoryFlag, patch.AssignedStoryFlag);
            if (reopened.RowIndex != original.RowIndex || reopened.EvaluationFormula != expectedFormula ||
                reopened.MasteryOwner != original.MasteryOwner || reopened.MasteryRank != original.MasteryRank ||
                reopened.BloodCodePackage != original.BloodCodePackage || reopened.BloodCodeAsset != original.BloodCodeAsset ||
                reopened.BoosterStoryFlag != patch.AssignedStoryFlag || reopened.BoosterPackage != patch.AssignedPackage ||
                reopened.BoosterAsset != patch.AssignedAsset || reopened.Count != original.Count ||
                reopened.Notification != original.Notification)
                throw new InvalidDataException($"Reopened Booster row {original.RowName} failed its field-level assignment validation.");
            if (!String.Equals(Snapshot(original, maskMutableFields: true), Snapshot(reopened, maskMutableFields: true), StringComparison.Ordinal))
                throw new InvalidDataException($"Booster row {original.RowName} changed outside EvaluationFormula and its Booster reward path.");
        }
    }


    // Find the target data table

    private static DataTableExport ResolveTable(UAsset asset, string inputPath)
    {
        var tableIndexes = asset.Exports.Select((export, index) => (export, index))
            .Where(item => String.Equals(item.export.ObjectName.ToString(), TableName, StringComparison.Ordinal))
            .ToArray();
        if (tableIndexes.Length == 1 && asset.Exports[tableIndexes[0].index] is not DataTableExport)
        {
            using var reader = asset.PathToReader(inputPath, true);
            asset.ParseExport(reader, tableIndexes[0].index, true);
        }
        var tables = asset.Exports.OfType<DataTableExport>()
            .Where(export => String.Equals(export.ObjectName.ToString(), TableName, StringComparison.Ordinal)).ToArray();
        if (tables.Length != 1 || tables[0].Table?.Data is null)
            throw new InvalidDataException($"Expected exactly one parsed {TableName} DataTable export; found {tables.Length}. Exports: {String.Join(", ", asset.Exports.Select(export => $"{export.GetType().Name}:{export.ObjectName}"))}");
        return tables[0];
    }


    // Read package table rows

    private static List<RowInspection> ReadRows(DataTableExport table)
    {
        var rows = new List<RowInspection>();
        var index = 0;
        foreach (var raw in (IEnumerable)table.Table!.Data)
        {
            if (raw is not StructPropertyData structure)
                throw new InvalidDataException("Booster DataTable row was not parsed as a struct.");
            rows.Add(ReadRow(index++, structure));
        }
        if (rows.Select(row => row.RowName).Distinct(StringComparer.OrdinalIgnoreCase).Count() != rows.Count)
            throw new InvalidDataException("Booster DataTable contains duplicate row names.");
        return rows;
    }


    // Read one table row

    private static RowInspection ReadRow(int index, StructPropertyData structure)
    {
        var rowName = structure.Name.ToString();
        if (String.IsNullOrWhiteSpace(rowName)) throw new InvalidDataException("Booster DataTable row has an empty name.");
        var properties = structure.Value;
        var formulas = Walk(properties).OfType<StrPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), "EvaluationFormula", StringComparison.Ordinal)).ToArray();
        if (formulas.Length != 1) throw new InvalidDataException($"{rowName} must contain exactly one EvaluationFormula.");
        var formula = formulas[0].Value.ToString();
        var flags = BoosterFlag.Matches(formula).Cast<Match>().ToArray();
        var masteryMatches = Mastery.Matches(formula).Cast<Match>().ToArray();
        if (flags.Length != 1 || masteryMatches.Length != 1)
            throw new InvalidDataException($"{rowName} must contain exactly one Booster flag and mastery requirement.");
        var articlePaths = Walk(properties).OfType<SoftObjectPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), "ArticleDataPath", StringComparison.Ordinal)).ToArray();
        var bloodCodes = articlePaths.Select(ReadSoftPath).Where(path => path.Package.StartsWith("/Game/Blueprint/Article/BloodCode/", StringComparison.Ordinal)).ToArray();
        var boosters = articlePaths.Select(ReadSoftPath).Where(path => path.Package.StartsWith(BoosterPrefix, StringComparison.Ordinal)).ToArray();
        if (bloodCodes.Length != 1 || boosters.Length != 1)
            throw new InvalidDataException($"{rowName} must contain exactly one Blood Code and one Booster ArticleDataPath.");
        ValidatePackageAsset(bloodCodes[0].Package, bloodCodes[0].Asset, "Blood Code");
        ValidateBoosterIdentity(flags[0].Groups[1].Value, boosters[0].Package, boosters[0].Asset);
        var rewardStruct = FindBoosterStruct(properties);
        var articles = rewardStruct.Value.OfType<ArrayPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), "Articles", StringComparison.Ordinal)).ToArray();
        if (articles.Length != 1) throw new InvalidDataException($"{rowName} must contain one Booster Articles array.");
        var entries = articles[0].Value.ToArray();
        if (entries.Length != 1 || entries[0] is not StructPropertyData article)
            throw new InvalidDataException($"{rowName} must contain exactly one Booster Article entry.");
        var count = article.Value.OfType<IntPropertyData>()
            .SingleOrDefault(property => String.Equals(property.Name.ToString(), "Count", StringComparison.Ordinal))
            ?? throw new InvalidDataException($"{rowName} Booster article is missing its integer Count.");
        var notification = article.Value.OfType<BoolPropertyData>()
            .SingleOrDefault(property => String.Equals(property.Name.ToString(), "bNotification", StringComparison.Ordinal))
            ?? throw new InvalidDataException($"{rowName} Booster article is missing bNotification.");
        return new RowInspection(index, rowName, formula,
            masteryMatches[0].Groups[1].Value, masteryMatches[0].Groups[2].Value,
            bloodCodes[0].Package, bloodCodes[0].Asset,
            flags[0].Groups[1].Value, boosters[0].Package, boosters[0].Asset,
            count.Value, notification.Value, structure);
    }


    // Read to row Map

    private static Dictionary<string, RowInspection> ToRowMap(IEnumerable<RowInspection> rows) =>
        rows.ToDictionary(row => row.RowName, StringComparer.OrdinalIgnoreCase);


    // Find a table row

    private static RowInspection FindRow(IReadOnlyDictionary<string, RowInspection> rows, string name) =>
        rows.TryGetValue(name, out var row) ? row : throw new InvalidDataException($"Required Booster row {name} was not found.");


    // Find the Booster struct

    private static StructPropertyData FindBoosterStruct(IEnumerable<PropertyData> properties)
    {
        var matches = properties.OfType<StructPropertyData>()
            .Where(property => Walk(property.Value).OfType<SoftObjectPropertyData>()
                .Any(path => String.Equals(path.Name.ToString(), "ArticleDataPath", StringComparison.Ordinal) &&
                    ReadSoftPath(path).Package.StartsWith(BoosterPrefix, StringComparison.Ordinal))).ToArray();
        if (matches.Length != 1) throw new InvalidDataException("Booster reward must be in exactly one row struct.");
        return matches[0];
    }


    // Replace formula flag

    private static void ReplaceFormulaFlag(RowInspection row, string expected, string assigned)
    {
        var formulas = Walk(row.Structure.Value).OfType<StrPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), "EvaluationFormula", StringComparison.Ordinal)).ToArray();
        if (formulas.Length != 1) throw new InvalidDataException($"{row.RowName} has an ambiguous EvaluationFormula.");
        formulas[0].Value = new FString(ReplaceFormulaFlag(formulas[0].Value.ToString(), expected, assigned));
    }


    // Replace formula flag

    private static string ReplaceFormulaFlag(string formula, string expected, string assigned)
    {
        var match = BoosterFlag.Match(formula);
        if (!match.Success || BoosterFlag.Matches(formula).Count != 1 || !String.Equals(match.Groups[1].Value, expected, StringComparison.Ordinal))
            throw new InvalidDataException("Booster StoryFlag changed before patching.");
        return formula[..match.Groups[1].Index] + assigned + formula[(match.Groups[1].Index + match.Groups[1].Length)..];
    }


    // Set the Booster asset path

    private static void SetBoosterPath(UAsset asset, RowInspection row, string package, string assetName)
    {
        var paths = Walk(row.Structure.Value).OfType<SoftObjectPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), "ArticleDataPath", StringComparison.Ordinal) &&
                ReadSoftPath(property).Package.StartsWith(BoosterPrefix, StringComparison.Ordinal)).ToArray();
        if (paths.Length != 1) throw new InvalidDataException($"{row.RowName} has an ambiguous Booster ArticleDataPath.");
        var value = (FSoftObjectPath)paths[0].Value!;
        value.AssetPath = new FTopLevelAssetPath
        {
            PackageName = new FName(asset, package),
            AssetName = new FName(asset, assetName),
        };
        paths[0].Value = value;
    }


    // Find the matching flag

    private static string MatchFlag(string formula)
    {
        var matches = BoosterFlag.Matches(formula).Cast<Match>().ToArray();
        return matches.Length == 1 ? matches[0].Groups[1].Value : throw new InvalidDataException("EvaluationFormula must have exactly one Booster flag.");
    }

    private static (string Package, string Asset) ReadSoftPath(SoftObjectPropertyData property)
    {
        var path = (FSoftObjectPath)property.Value!;
        return (path.AssetPath.PackageName.ToString(), path.AssetPath.AssetName.ToString());
    }


    // Check the package asset

    private static void ValidatePackageAsset(string package, string asset, string label)
    {
        if (!package.StartsWith("/Game/Blueprint/Article/", StringComparison.Ordinal) ||
            !String.Equals(package[(package.LastIndexOf('/') + 1)..], asset, StringComparison.Ordinal))
            throw new InvalidDataException($"{label} ArticleDataPath must reference a package-level asset.");
    }


    // Check Booster identity

    private static void ValidateBoosterIdentity(string flag, string package, string asset)
    {
        if (!Regex.IsMatch(flag, @"^Booster_[A-Za-z0-9_]+$", RegexOptions.CultureInvariant))
            throw new InvalidDataException("Assigned Booster StoryFlag is malformed.");
        if (!package.StartsWith(BoosterPrefix, StringComparison.Ordinal) ||
            !String.Equals(package[(package.LastIndexOf('/') + 1)..], asset, StringComparison.Ordinal) ||
            !asset.StartsWith("DA_Booster_", StringComparison.Ordinal))
            throw new InvalidDataException("Assigned reward must be a package-level DA_Booster asset.");
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

    private static string Snapshot(RowInspection row, bool maskMutableFields)
    {
        return JsonSerializer.Serialize(new
        {
            Row = row.RowName,
            Properties = row.Structure.Value.Select(property => Canonicalize(property, maskMutableFields)).ToArray(),
        });
    }


    // Normalize serialized data

    private static object? Canonicalize(object? value, bool maskMutableFields)
    {
        if (value is null) return null;
        if (value is PropertyData property)
        {
            var raw = property.GetType().GetProperty("Value")?.GetValue(property);
            var name = property.Name.ToString();
            var mutable = maskMutableFields && (property is StrPropertyData && name == "EvaluationFormula" ||
                property is SoftObjectPropertyData && name == "ArticleDataPath" &&
                raw is FSoftObjectPath path && path.AssetPath.PackageName.ToString().StartsWith(BoosterPrefix, StringComparison.Ordinal));
            return new
            {
                Type = property.GetType().FullName,
                Name = name,
                Value = mutable ? "<assigned-booster-identity>" : Canonicalize(raw, maskMutableFields),
            };
        }
        if (value is FName fname) return fname.ToString();
        if (value is FSoftObjectPath softPath) return softPath.ToString();
        if (value is FTopLevelAssetPath topLevel) return topLevel.ToString();
        if (value is string or bool or byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal)
            return value;
        if (value is Enum enumValue) return enumValue.ToString();
        if (value is IEnumerable enumerable)
            return enumerable.Cast<object?>().Select(item => Canonicalize(item, maskMutableFields)).ToArray();
        return value.ToString() ?? value.GetType().FullName;
    }


    // Check the virtual package

    private static void ValidateVirtualPackage(string package)
    {
        if (!String.Equals(package, "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/DT_GainBoosterSmallScenario", StringComparison.Ordinal))
            throw new InvalidDataException("Only the catalogued DT_GainBoosterSmallScenario package can be patched.");
    }


    // Resolve an output path

    private static string ResolveOwnedPath(string workRoot, string value, bool mustExist)
    {
        var root = Path.GetFullPath(workRoot);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(root, path);
        if (relative == "." || relative == ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new InvalidDataException("Booster package path escaped its generation workspace.");
        if (mustExist && !File.Exists(path)) throw new FileNotFoundException("Booster package was not found.", path);
        return path;
    }


    // Resolve readable path

    private static string ResolveReadablePath(string value)
    {
        var path = Path.GetFullPath(value);
        if (!File.Exists(path)) throw new FileNotFoundException("Extracted Booster package was not found.", path);
        return path;
    }


    // Collect package files

    private static string[] PackageFiles(string header) => new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" }
        .Select(extension => Path.ChangeExtension(header, extension)).ToArray();


    // Copy a row for validation

    private static void CopyRowStructForValidation(string inputHeader, string temporaryHeader, ICollection<string> temporaryFiles)
    {
        var inputDirectory = Path.GetDirectoryName(inputHeader)
            ?? throw new InvalidDataException("Booster source package has no parent directory.");
        var outputDirectory = Path.GetDirectoryName(temporaryHeader)
            ?? throw new InvalidDataException("Booster validation package has no parent directory.");
        var sourceHeader = Path.Combine(inputDirectory, RowStructName + ".uasset");
        if (!File.Exists(sourceHeader))
            throw new FileNotFoundException(
                $"Booster DataTable row schema {RowStructName} was not materialized beside its source package.",
                sourceHeader);
        var outputHeader = Path.Combine(outputDirectory, RowStructName + ".uasset");
        foreach (var source in PackageFiles(sourceHeader).Where(File.Exists))
        {
            var target = Path.ChangeExtension(outputHeader, Path.GetExtension(source));
            if (File.Exists(target))
                throw new IOException($"Refusing to overwrite staged Booster schema package {Path.GetFileName(target)}.");
            File.Copy(source, target);
            temporaryFiles.Add(target);
        }
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
        if (!File.Exists(outputHeader)) throw new InvalidDataException("UAssetAPI omitted the Booster .uasset header.");
        if (File.Exists(Path.ChangeExtension(inputHeader, ".uexp")) && !File.Exists(Path.ChangeExtension(outputHeader, ".uexp")))
            throw new InvalidDataException("UAssetAPI omitted the Booster .uexp sidecar.");
    }


    // Describe package inspection results

    private sealed record InspectionManifest(IReadOnlyList<InspectionRequest> Packages);
    // Describe an inspection request
    private sealed record InspectionRequest(string VirtualPackage, string InputFile);
    // Apply the patch manifest
    private sealed record PatchManifest(IReadOnlyList<PatchPackageRequest> Packages);
    // Describe a package patch request
    private sealed record PatchPackageRequest(string VirtualPackage, string InputFile, string OutputFile, IReadOnlyList<BoosterPatchRequest> Assignments);
    // Describe a Booster patch request
    private sealed record BoosterPatchRequest(
        int RowIndex, string RowName, string ExpectedFormula, string MasteryOwner, string MasteryRank,
        string ExpectedBloodCodePackage, string ExpectedBloodCodeAsset,
        string ExpectedStoryFlag, string ExpectedPackage, string ExpectedAsset,
        int ExpectedCount, bool ExpectedNotification,
        string AssignedStoryFlag, string AssignedPackage, string AssignedAsset);
    // Describe inspection result
    private sealed record InspectionResult(string VirtualPackage, string InputFile, int RowCount, IReadOnlyList<RowInspection> Rows);
    // Patch result
    private sealed record PatchResult(string VirtualPackage, string InputFile, string OutputFile, int PatchedRows, IReadOnlyList<string> OutputFiles, IReadOnlyList<RowInspection> Rows);
    // Describe an inspected row
    private sealed record RowInspection(
        int RowIndex, string RowName, string EvaluationFormula, string MasteryOwner, string MasteryRank,
        string BloodCodePackage, string BloodCodeAsset, string BoosterStoryFlag, string BoosterPackage,
        string BoosterAsset, int Count, bool Notification,
        [property: System.Text.Json.Serialization.JsonIgnore] StructPropertyData Structure);
}
