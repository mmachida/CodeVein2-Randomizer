using System.Collections;
using System.Globalization;
using System.Reflection;
using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class BloodCodeMasteryPatcher
{
    private const string ArticleRoot = "/Game/Blueprint/Article/BloodCode/";
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
            var manifest = JsonSerializer.Deserialize<InspectManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Blood Code mastery manifest is empty.");
            if (manifest.Packages.Count is < 1 or > 100)
                throw new InvalidDataException("Blood Code mastery inspection must contain 1 to 100 packages.");

            var results = new List<ArticleInspection>();
            foreach (var request in manifest.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase))
            {
                if (!request.VirtualPackage.StartsWith(ArticleRoot, StringComparison.Ordinal))
                    throw new InvalidDataException("Blood Code mastery package is outside the Blood Code Article folder.");
                var input = Path.GetFullPath(request.InputFile);
                var relative = Path.GetRelativePath(root, input);
                if (relative is "." or ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
                    throw new InvalidDataException("Blood Code mastery source escaped the extracted asset root.");
                if (!File.Exists(input)) throw new FileNotFoundException("Blood Code mastery source package is missing.", input);

                var asset = new UAsset(input, engine, mappings);
                using var reader = asset.PathToReader(input, true);
                var expectedName = request.VirtualPackage[(request.VirtualPackage.LastIndexOf('/') + 1)..];
                var matches = asset.Exports.Select((value, index) => (value, index))
                    .Where(item => item.value.ObjectName.ToString() == expectedName).ToArray();
                if (matches.Length != 1) throw new InvalidDataException($"{request.VirtualPackage} has an invalid exact export inventory.");
                var (export, index) = matches[0];
                if (export is not NormalExport) asset.ParseExport(reader, index, true);
                if (asset.Exports[index] is not NormalExport parsed)
                    throw new InvalidDataException($"{request.VirtualPackage} DataAsset export is not parseable.");
                var properties = new List<PropertyInspection>();
                foreach (var property in parsed.Data) AddProperties(property, property.Name.ToString(), properties);
                results.Add(new ArticleInspection(request.VirtualPackage, input, expectedName, properties));
            }
            Console.WriteLine(JsonSerializer.Serialize(results, JsonOptions));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine($"{error.GetType().Name}: {error.Message}");
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
            var request = JsonSerializer.Deserialize<PatchManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Blood Code mastery patch manifest is empty.");
            if (request.TargetProficiency is float requestedTarget &&
                (!float.IsFinite(requestedTarget) || requestedTarget <= 0))
                throw new InvalidDataException("Target Blood Code mastery EXP must be a positive finite number.");
            if (request.TargetAppropriateLevel <= 0)
                throw new InvalidDataException("Target Blood Code mastery appropriate level must be a positive integer.");
            if (request.Packages.Count is < 1 or > 100)
                throw new InvalidDataException("Blood Code mastery patch must contain 1 to 100 packages.");
            var lou01Requests = request.Packages.Where(package =>
                package.VirtualPackage.EndsWith("/DA_BloodCode_Lou_01", StringComparison.Ordinal)).ToArray();
            if (lou01Requests.Length != 1)
                throw new InvalidDataException("The installed Blood Code Article inventory must contain exactly one Lou 1 baseline.");
            var lou01Input = ResolveWithin(root, lou01Requests[0].InputFile, mustExist: true);
            var lou01Asset = new UAsset(lou01Input, engine, mappings);
            var lou01Article = ResolveArticle(lou01Asset, lou01Input, lou01Requests[0].VirtualPackage);
            var lou01Fields = lou01Article.Data.OfType<FloatPropertyData>()
                .Where(property => property.Name.ToString() == "MaxProficiency").ToArray();
            if (lou01Fields.Length != 1 || !float.IsFinite(lou01Fields[0].Value) || lou01Fields[0].Value <= 0)
                throw new InvalidDataException("Lou 1 does not have exactly one valid original MaxProficiency value.");
            var lou01OriginalProficiency = lou01Fields[0].Value;
            var targetProficiency = request.TargetProficiency ?? lou01OriginalProficiency;
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var results = new List<PackagePatchResult>();
            foreach (var package in request.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase))
            {
                if (!package.VirtualPackage.StartsWith(ArticleRoot, StringComparison.Ordinal) ||
                    !seen.Add(package.VirtualPackage))
                    throw new InvalidDataException("Blood Code mastery package is outside the Article folder or duplicated.");
                var input = ResolveWithin(root, package.InputFile, mustExist: true);
                var output = ResolveWithin(root, package.OutputFile, mustExist: false);
                if (String.Equals(input, output, StringComparison.OrdinalIgnoreCase) || File.Exists(output))
                    throw new IOException("Blood Code mastery output must be a new staged package separate from its source.");

                var asset = new UAsset(input, engine, mappings);
                var parsed = ResolveArticle(asset, input, package.VirtualPackage);
                var masteryFields = parsed.Data.OfType<FloatPropertyData>()
                    .Where(property => property.Name.ToString() == "MaxProficiency").ToArray();
                if (masteryFields.Length != 1 ||
                    !float.IsFinite(masteryFields[0].Value) || masteryFields[0].Value <= 0)
                    throw new InvalidDataException($"{package.VirtualPackage} does not have exactly one valid Float MaxProficiency field.");
                var appropriateLevelFields = parsed.Data
                    .Where(property => property.Name.ToString() == "AppropriateLevel").ToArray();
                if (appropriateLevelFields.Length > 1)
                    throw new InvalidDataException($"{package.VirtualPackage} has duplicate AppropriateLevel fields.");
                IntPropertyData? appropriateLevelField = null;
                if (appropriateLevelFields.Length == 1)
                    appropriateLevelField = appropriateLevelFields[0] as IntPropertyData
                        ?? throw new InvalidDataException($"{package.VirtualPackage} has an unsupported AppropriateLevel field type.");
                var originalValue = masteryFields[0].Value;
                var originalProperties = SnapshotProperties(parsed.Data);
                masteryFields[0].Value = targetProficiency;
                if (appropriateLevelField is not null)
                    appropriateLevelField.Value = request.TargetAppropriateLevel;

                var directory = Path.GetDirectoryName(output)!;
                Directory.CreateDirectory(directory);
                var temporary = Path.Combine(directory,
                    Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + ".uasset");
                try
                {
                    asset.Write(temporary);
                    CopyAdditionalSidecars(input, temporary);
                    ValidateHeaderAndSidecar(temporary);
                    var reopened = new UAsset(temporary, engine, mappings);
                    var reopenedArticle = ResolveArticle(reopened, temporary, package.VirtualPackage);
                    ValidateProperties(originalProperties, reopenedArticle.Data, targetProficiency,
                        request.TargetAppropriateLevel, package.VirtualPackage);

                    var temporaryFiles = PackageFiles(temporary).Where(File.Exists).ToArray();
                    if (!temporaryFiles.Contains(temporary, StringComparer.OrdinalIgnoreCase))
                        throw new InvalidDataException("UAssetAPI omitted the Blood Code Article package header.");
                    var outputFiles = temporaryFiles
                        .Select(path => Path.ChangeExtension(output, Path.GetExtension(path))).ToArray();
                    if (outputFiles.Any(File.Exists))
                        throw new IOException("Refusing to overwrite an existing Blood Code mastery output sidecar.");
                    for (var index = 0; index < temporaryFiles.Length; index++)
                    {
                        File.Move(temporaryFiles[index], outputFiles[index]);
                        committed.Add(outputFiles[index]);
                    }
                    var final = new UAsset(output, engine, mappings);
                    var finalArticle = ResolveArticle(final, output, package.VirtualPackage);
                    ValidateProperties(originalProperties, finalArticle.Data, targetProficiency,
                        request.TargetAppropriateLevel, package.VirtualPackage);
                    results.Add(new PackagePatchResult(package.VirtualPackage, originalValue,
                        targetProficiency, outputFiles));
                }
                finally
                {
                    foreach (var path in PackageFiles(temporary))
                        try { File.Delete(path); } catch (IOException) { }
                }
            }
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                patchedPackages = results.Select(item => item.VirtualPackage).ToArray(),
                patchedCount = results.Count,
                targetProficiency,
                lou01OriginalProficiency,
                targetAppropriateLevel = request.TargetAppropriateLevel,
                outputFiles = committed,
                reopenedAndValidated = true,
            }, JsonOptions));
            return 0;
        }
        catch (Exception error)
        {
            foreach (var path in committed)
                try { File.Delete(path); } catch (IOException) { }
            Console.Error.WriteLine($"{error.GetType().Name}: {error.Message}");
            return 1;
        }
    }


    // Resolve the Article identity

    private static NormalExport ResolveArticle(UAsset asset, string file, string virtualPackage)
    {
        var expectedName = virtualPackage[(virtualPackage.LastIndexOf('/') + 1)..];
        var matches = asset.Exports.Select((value, index) => (value, index))
            .Where(item => item.value.ObjectName.ToString() == expectedName).ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"{virtualPackage} has an invalid exact export inventory.");
        var (export, index) = matches[0];
        if (export is not NormalExport)
        {
            using var reader = asset.PathToReader(file, true);
            asset.ParseExport(reader, index, true);
        }
        return asset.Exports[index] as NormalExport
            ?? throw new InvalidDataException($"{virtualPackage} DataAsset export is not parseable.");
    }


    // Capture serialized properties

    private static string[] SnapshotProperties(IEnumerable<PropertyData> properties) =>
        properties.Select(property => JsonSerializer.Serialize(SnapshotProperty(
            property,
            maskPatchedValue: (property is FloatPropertyData && property.Name.ToString() == "MaxProficiency") ||
                (property is IntPropertyData && property.Name.ToString() == "AppropriateLevel"))))
            .ToArray();


    // Capture serialized property state

    private static object SnapshotProperty(PropertyData property, bool maskPatchedValue = false)
    {
        if (property is StructPropertyData structure)
            return new { name = property.Name.ToString(), type = property.GetType().Name,
                value = structure.Value.Select(child => SnapshotProperty(child)).ToArray() };
        if (property is ArrayPropertyData array)
            return new { name = property.Name.ToString(), type = property.GetType().Name,
                value = array.Value.Select(value => value is PropertyData child
                    ? SnapshotProperty(child)
                    : Convert.ToString(value, CultureInfo.InvariantCulture)).ToArray() };
        var value = property.GetType().GetProperty("Value", BindingFlags.Instance | BindingFlags.Public)?.GetValue(property);
        var serialized = Convert.ToString(value, CultureInfo.InvariantCulture);
        if (maskPatchedValue) serialized = "<mastery-setting>";
        return new { name = property.Name.ToString(), type = property.GetType().Name, value = serialized };
    }


    // Check properties

    private static void ValidateProperties(string[] before, IEnumerable<PropertyData> after,
        float targetProficiency, int targetAppropriateLevel, string virtualPackage)
    {
        var properties = after.ToArray();
        var mastery = properties.OfType<FloatPropertyData>()
            .Where(property => property.Name.ToString() == "MaxProficiency").ToArray();
        if (mastery.Length != 1 || mastery[0].Value != targetProficiency)
            throw new InvalidDataException($"{virtualPackage} did not retain the requested Blood Code mastery EXP.");
        var appropriateLevelFields = properties.Where(property => property.Name.ToString() == "AppropriateLevel").ToArray();
        if (appropriateLevelFields.Length > 1 ||
            (appropriateLevelFields.Length == 1 &&
                (appropriateLevelFields[0] is not IntPropertyData level || level.Value != targetAppropriateLevel)))
            throw new InvalidDataException($"{virtualPackage} did not retain the requested Blood Code enemy-level setting.");
        var actual = SnapshotProperties(properties);
        if (!before.SequenceEqual(actual, StringComparer.Ordinal))
            throw new InvalidDataException($"{virtualPackage} changed outside Blood Code mastery settings.");
    }


    // Check path stays inside root

    private static string ResolveWithin(string root, string candidate, bool mustExist)
    {
        var full = Path.GetFullPath(candidate);
        var relative = Path.GetRelativePath(root, full);
        if (relative is "." or ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal)
            || Path.IsPathRooted(relative))
            throw new InvalidDataException("Blood Code mastery package path escaped the generation workspace.");
        if (mustExist && !File.Exists(full)) throw new FileNotFoundException("Blood Code mastery source package is missing.", full);
        if (!mustExist && Path.GetExtension(full) != ".uasset")
            throw new InvalidDataException("Blood Code mastery output must be a .uasset package header.");
        return full;
    }


    // Collect package files

    private static IEnumerable<string> PackageFiles(string header)
    {
        yield return header;
        foreach (var extension in new[] { ".uexp", ".ubulk", ".uptnl" })
            yield return Path.ChangeExtension(header, extension);
    }


    // Copy additional sidecars

    private static void CopyAdditionalSidecars(string input, string output)
    {
        foreach (var extension in new[] { ".ubulk", ".uptnl" })
        {
            var source = Path.ChangeExtension(input, extension);
            if (File.Exists(source)) File.Copy(source, Path.ChangeExtension(output, extension), overwrite: false);
        }
    }


    // Check package header and sidecars

    private static void ValidateHeaderAndSidecar(string header)
    {
        if (!File.Exists(header) || new FileInfo(header).Length == 0)
            throw new InvalidDataException("Serialized Blood Code Article header is missing or empty.");
        var sidecars = PackageFiles(header).Where(File.Exists).ToArray();
        if (sidecars.Any(path => new FileInfo(path).Length == 0))
            throw new InvalidDataException("Serialized Blood Code Article contains an empty sidecar.");
    }


    // Add serialized package properties

    private static void AddProperties(PropertyData property, string path, List<PropertyInspection> output)
    {
        switch (property)
        {
            case StructPropertyData structure:
                output.Add(new PropertyInspection(path, property.GetType().Name, null));
                foreach (var child in structure.Value) AddProperties(child, path + "." + child.Name, output);
                break;
            case ArrayPropertyData array:
                output.Add(new PropertyInspection(path, property.GetType().Name, $"count={array.Value.Length}"));
                for (var index = 0; index < array.Value.Length; index++)
                    if (array.Value[index] is PropertyData child)
                        AddProperties(child, $"{path}[{index}].{child.Name}", output);
                    else
                        output.Add(new PropertyInspection($"{path}[{index}]", array.Value[index]?.GetType().Name ?? "null", array.Value[index]?.ToString()));
                break;
            default:
                output.Add(new PropertyInspection(path, property.GetType().Name, Format(property)));
                break;
        }
    }


    // Format the property value

    private static string? Format(PropertyData property)
    {
        var value = property.GetType().GetProperty("Value")?.GetValue(property);
        if (value is FSoftObjectPath path)
            return path.AssetPath.ToString();
        if (value is IEnumerable sequence and not string)
            return string.Join(",", sequence.Cast<object?>().Select(value => value?.ToString() ?? "null"));
        return value?.ToString();
    }


    // Read the inspection manifest

    private sealed record InspectManifest(IReadOnlyList<InspectRequest> Packages);
    // Read the inspection request
    private sealed record InspectRequest(string VirtualPackage, string InputFile);
    // Apply the patch manifest
    private sealed record PatchManifest(float? TargetProficiency, int TargetAppropriateLevel,
        IReadOnlyList<PatchRequest> Packages);
    // Read the package patch request
    private sealed record PatchRequest(string VirtualPackage, string InputFile, string OutputFile);
    // Describe package patch results
    private sealed record PackagePatchResult(string VirtualPackage, float OriginalProficiency,
        float TargetProficiency, IReadOnlyList<string> OutputFiles);
    // Describe inspected Article data
    private sealed record ArticleInspection(string VirtualPackage, string InputFile, string ExportName,
        IReadOnlyList<PropertyInspection> Properties);
    // Describe inspected property data
    private sealed record PropertyInspection(string Path, string Type, string? Value);
}
