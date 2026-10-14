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

internal static class MotorcycleUnlockPatcher
{
    private const string TableName = "DT_StoryFlagDefine_Beginning";
    private const string TargetFlag = "BloodMagic_BikeCreator";
    private const string ExpectedVirtualPackage = "/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/DT_StoryFlagDefine_Beginning";
    private const string PlayerVirtualPackage = "/Game/Blueprint/Characters/Player/BP_Player";


    // Patch

    public static int Patch(string[] args)
    {
        try
        {
            using var document = JsonDocument.Parse(File.ReadAllText(args[4]));
            var virtualPackage = document.RootElement.GetProperty("virtualPackage").GetString();
            if (virtualPackage == PlayerVirtualPackage)
                return MotorcycleMountGatePatcher.Patch(args);
            if (virtualPackage != ExpectedVirtualPackage)
                throw new InvalidDataException("Motorcycle unlock request names an unexpected package.");
            return PatchStoryFlag(args);
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 1;
        }
    }


    // Patch the story flag

    private static int PatchStoryFlag(string[] args)
    {
        string? temporaryHeader = null;
        var committed = new List<string>();
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var request = JsonSerializer.Deserialize<PatchRequest>(File.ReadAllText(args[4]),
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
                ?? throw new InvalidDataException("Motorcycle unlock request is empty.");
            if (request.VirtualPackage != ExpectedVirtualPackage)
                throw new InvalidDataException("Motorcycle unlock request names an unexpected StoryFlag package.");
            var input = Path.GetFullPath(request.InputFile);
            var output = Path.GetFullPath(request.OutputFile);
            if (!File.Exists(input) || !IsWithin(input, workRoot) || !IsWithin(output, workRoot) || input == output || File.Exists(output))
                throw new InvalidDataException("Motorcycle StoryFlag output must be a new staged package inside the generation workspace.");

            var asset = new UAsset(input, engineVersion, mappings);
            var table = ResolveTable(asset, input);
            var rows = ReadRows(table);
            var row = FindRow(rows, TargetFlag);
            var category = row.Value.OfType<NamePropertyData>().SingleOrDefault(property => property.Name.ToString() == "Category");
            var flagType = row.Value.OfType<EnumPropertyData>().SingleOrDefault(property => property.Name.ToString() == "FlagType");
            var defaultFlag = row.Value.OfType<BoolPropertyData>().SingleOrDefault(property => property.Name.ToString() == "DefaultValueBool");
            if (category?.Value.ToString() != "Item" || flagType?.Value.ToString() != "Boolean" || defaultFlag is null)
                throw new InvalidDataException($"Story flag {TargetFlag} no longer matches the audited Item/Boolean schema.");

            var originalRows = rows.Select(SnapshotRow).ToArray();
            defaultFlag.Value = true;
            var directory = Path.GetDirectoryName(output)!;
            Directory.CreateDirectory(directory);
            temporaryHeader = Path.Combine(directory,
                Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + ".uasset");
            asset.Write(temporaryHeader);
            CopySidecars(input, temporaryHeader);
            var reopened = new UAsset(temporaryHeader, engineVersion, mappings);
            ValidateRows(originalRows, ReadRows(ResolveTable(reopened, temporaryHeader)));

            var temporaryFiles = PackageFiles(temporaryHeader).Where(File.Exists).ToArray();
            foreach (var source in temporaryFiles)
            {
                var target = Path.ChangeExtension(output, Path.GetExtension(source));
                if (File.Exists(target)) throw new IOException("Refusing to overwrite an existing staged motorcycle StoryFlag sidecar.");
            }
            foreach (var source in temporaryFiles)
            {
                var target = Path.ChangeExtension(output, Path.GetExtension(source));
                File.Move(source, target);
                committed.Add(target);
            }

            var final = new UAsset(output, engineVersion, mappings);
            ValidateRows(originalRows, ReadRows(ResolveTable(final, output)));
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                virtualPackage = request.VirtualPackage,
                outputFiles = committed,
                patchedFlags = new[] { TargetFlag },
                patchedMountGate = false,
                reopenedAndValidated = true,
            }));
            return 0;
        }
        catch (Exception error)
        {
            foreach (var path in committed)
                try { File.Delete(path); } catch (IOException) { }
            Console.Error.WriteLine(error);
            return 1;
        }
        finally
        {
            if (temporaryHeader is not null)
                foreach (var path in PackageFiles(temporaryHeader))
                    try { File.Delete(path); } catch (IOException) { }
        }
    }


    // Find the target data table

    private static DataTableExport ResolveTable(UAsset asset, string input)
    {
        var matches = asset.Exports.Select((export, index) => (export, index))
            .Where(item => item.export.ObjectName.ToString() == TableName).ToArray();
        if (matches.Length == 1 && matches[0].export is not DataTableExport)
        {
            using var reader = asset.PathToReader(input, true);
            asset.ParseExport(reader, matches[0].index, true);
        }
        var tables = asset.Exports.OfType<DataTableExport>()
            .Where(export => export.ObjectName.ToString() == TableName).ToArray();
        if (tables.Length != 1 || tables[0].Table?.Data is null)
            throw new InvalidDataException($"Expected exactly one parsed {TableName} DataTable export.");
        return tables[0];
    }


    // Read package table rows

    private static List<StructPropertyData> ReadRows(DataTableExport table)
    {
        var rows = new List<StructPropertyData>();
        foreach (var raw in (IEnumerable)table.Table!.Data)
            rows.Add(raw as StructPropertyData ?? throw new InvalidDataException("Story flag DataTable row is not a parsed struct."));
        if (rows.Select(row => row.Name.ToString()).Distinct(StringComparer.Ordinal).Count() != rows.Count)
            throw new InvalidDataException("Story flag DataTable contains duplicate row names.");
        return rows;
    }


    // Find a table row

    private static StructPropertyData FindRow(List<StructPropertyData> rows, string name)
    {
        var matches = rows.Where(row => row.Name.ToString() == name).ToArray();
        return matches.Length == 1 ? matches[0] : throw new InvalidDataException($"Expected exactly one story flag row {name}; found {matches.Length}.");
    }


    // Capture serialized row state

    private static string SnapshotRow(StructPropertyData row) => JsonSerializer.Serialize(new
    {
        name = row.Name.ToString(),
        properties = row.Value.Select(property => SnapshotProperty(
            property,
            maskDefault: row.Name.ToString() == TargetFlag && property.Name.ToString() == "DefaultValueBool"
        )).ToArray(),
    });


    // Capture serialized property state

    private static object SnapshotProperty(PropertyData property, bool maskDefault = false)
    {
        if (property is StructPropertyData structure)
            return new { name = property.Name.ToString(), type = property.GetType().Name, value = structure.Value.Select(child => SnapshotProperty(child)).ToArray() };
        if (property is ArrayPropertyData array)
            return new { name = property.Name.ToString(), type = property.GetType().Name, value = array.Value.OfType<PropertyData>().Select(child => SnapshotProperty(child)).ToArray() };
        var value = property.GetType().GetProperty("Value", BindingFlags.Instance | BindingFlags.Public)?.GetValue(property);
        var serialized = value is null ? null : Convert.ToString(value, CultureInfo.InvariantCulture);
        if (maskDefault) serialized = "<motorcycle-unlock-default>";
        return new { name = property.Name.ToString(), type = property.GetType().Name, value = serialized };
    }


    // Check package table rows

    private static void ValidateRows(string[] before, List<StructPropertyData> after)
    {
        if (before.Length != after.Count) throw new InvalidDataException("Motorcycle unlock patch changed the story flag row count.");
        var expectedNames = before.Select(value => JsonDocument.Parse(value).RootElement.GetProperty("name").GetString()).ToArray();
        if (!expectedNames.SequenceEqual(after.Select(row => row.Name.ToString()), StringComparer.Ordinal))
            throw new InvalidDataException("Motorcycle unlock patch changed story flag row ordering or names.");
        var target = FindRow(after, TargetFlag);
        var flag = target.Value.OfType<BoolPropertyData>().SingleOrDefault(property => property.Name.ToString() == "DefaultValueBool");
        if (flag?.Value != true) throw new InvalidDataException("Motorcycle unlock flag did not serialize as enabled.");
        for (var index = 0; index < after.Count; index++)
        {
            using var expected = JsonDocument.Parse(before[index]);
            using var actual = JsonDocument.Parse(SnapshotRow(after[index]));
            if (!JsonElement.DeepEquals(expected.RootElement, actual.RootElement))
                throw new InvalidDataException($"Story flag row {after[index].Name} changed outside the requested motorcycle unlock default.");
        }
    }


    // Copy package sidecars

    private static void CopySidecars(string input, string temporaryHeader)
    {
        foreach (var source in PackageFiles(input).Where(File.Exists))
        {
            if (String.Equals(source, input, StringComparison.OrdinalIgnoreCase)) continue;
            var target = Path.ChangeExtension(temporaryHeader, Path.GetExtension(source));
            if (!File.Exists(target)) File.Copy(source, target);
        }
    }


    // Collect package files

    private static IEnumerable<string> PackageFiles(string header)
    {
        yield return header;
        foreach (var extension in new[] { ".uexp", ".ubulk", ".uptnl" })
            yield return Path.ChangeExtension(header, extension);
    }


    // Check path stays inside root

    private static bool IsWithin(string path, string root)
    {
        var relative = Path.GetRelativePath(Path.GetFullPath(root), Path.GetFullPath(path));
        return relative != "." && !Path.IsPathRooted(relative) && relative != ".." &&
            !relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    }


    // Read the package patch request

    private sealed record PatchRequest(string VirtualPackage, string InputFile, string OutputFile);
}
