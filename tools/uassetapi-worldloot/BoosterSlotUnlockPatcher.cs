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

internal static class BoosterSlotUnlockPatcher
{
    private const string TableName = "DT_StoryFlagDefine_GameSystem";
    private static readonly string[] SupportedRows = [
        "UnlockEquipSlot_Booster5",
        "UnlockEquipSlot_Booster6",
        "UnlockEquipSlot_Weapon2",
    ];
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
            var request = JsonSerializer.Deserialize<PatchRequest>(File.ReadAllText(args[4]),
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
                ?? throw new InvalidDataException("Equipment-slot patch request is empty.");
            var input = Path.GetFullPath(request.InputFile);
            outputHeader = Path.GetFullPath(request.OutputFile);
            if (!File.Exists(input) || !IsWithin(outputHeader, workRoot) || input == outputHeader || File.Exists(outputHeader))
                throw new InvalidDataException("Equipment-slot output must be a new staged file inside the generation workspace.");
            var targetRows = ValidateTargets(request.TargetFlags);

            var asset = new UAsset(input, engineVersion, mappings);
            var table = ResolveTable(asset, input);
            var rows = ReadRows(table);
            var targets = targetRows.ToDictionary(name => name, name => FindRow(rows, name), StringComparer.Ordinal);
            foreach (var name in targetRows)
            {
                var row = targets[name];
                var flagType = row.Value.OfType<EnumPropertyData>().SingleOrDefault(property => property.Name.ToString() == "FlagType");
                var defaultFlag = row.Value.OfType<BoolPropertyData>().SingleOrDefault(property => property.Name.ToString() == "DefaultValueBool");
                var keep = row.Value.OfType<BoolPropertyData>().SingleOrDefault(property => property.Name.ToString() == "IsInitToKeepValue");
                if (flagType?.Value.ToString() != "Boolean" || defaultFlag is null || keep?.Value != true)
                    throw new InvalidDataException($"Story flag {name} no longer matches the audited Boolean/default-retention schema.");
            }

            var originalRows = rows.Select(row => SnapshotRow(row, targetRows)).ToArray();
            foreach (var name in targetRows)
                targets[name].Value.OfType<BoolPropertyData>().Single(property => property.Name.ToString() == "DefaultValueBool").Value = true;

            var directory = Path.GetDirectoryName(outputHeader)!;
            Directory.CreateDirectory(directory);
            var temporaryHeader = Path.Combine(directory, Path.GetFileNameWithoutExtension(outputHeader) + ".cv2-" + Guid.NewGuid().ToString("N") + ".uasset");
            try
            {
                asset.Write(temporaryHeader);
                CopySidecars(input, temporaryHeader);
                var reopened = new UAsset(temporaryHeader, engineVersion, mappings);
                var reopenedRows = ReadRows(ResolveTable(reopened, temporaryHeader));
                ValidateRows(originalRows, reopenedRows, targetRows);

                var temporaryFiles = PackageFiles(temporaryHeader).Where(File.Exists).ToArray();
                foreach (var source in temporaryFiles)
                {
                    var target = Path.ChangeExtension(outputHeader, Path.GetExtension(source));
                    if (File.Exists(target)) throw new IOException("Refusing to overwrite an existing equipment-slot staged sidecar.");
                }
                foreach (var source in temporaryFiles)
                {
                    var target = Path.ChangeExtension(outputHeader, Path.GetExtension(source));
                    File.Move(source, target);
                    committed.Add(target);
                }

                var final = new UAsset(outputHeader, engineVersion, mappings);
                ValidateRows(originalRows, ReadRows(ResolveTable(final, outputHeader)), targetRows);
                Console.WriteLine(JsonSerializer.Serialize(new {
                    virtualPackage = request.VirtualPackage,
                    outputFiles = committed,
                    patchedFlags = targetRows,
                    reopenedAndValidated = true,
                }));
                return 0;
            }
            finally
            {
                foreach (var file in PackageFiles(temporaryHeader))
                    try { File.Delete(file); } catch (IOException) { }
            }
        }
        catch (Exception error)
        {
            foreach (var file in committed)
                try { File.Delete(file); } catch (IOException) { }
            Console.Error.WriteLine(error);
            return 1;
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

    private static string SnapshotRow(StructPropertyData row, string[] targetRows) => JsonSerializer.Serialize(new {
        name = row.Name.ToString(),
        properties = row.Value.Select(property => SnapshotProperty(
            property,
            maskDefault: targetRows.Contains(row.Name.ToString()) && property.Name.ToString() == "DefaultValueBool"
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
        if (maskDefault)
            serialized = "<target-default>";
        return new { name = property.Name.ToString(), type = property.GetType().Name, value = serialized };
    }


    // Check package table rows

    private static void ValidateRows(string[] before, List<StructPropertyData> after, string[] targetRows)
    {
        if (before.Length != after.Count) throw new InvalidDataException("Equipment-slot patch changed the story flag row count.");
        var expectedNames = before.Select(value => JsonDocument.Parse(value).RootElement.GetProperty("name").GetString()).ToArray();
        if (!expectedNames.SequenceEqual(after.Select(row => row.Name.ToString()), StringComparer.Ordinal))
            throw new InvalidDataException("Equipment-slot patch changed story flag row ordering or names.");
        foreach (var row in after)
        {
            if (targetRows.Contains(row.Name.ToString(), StringComparer.Ordinal))
            {
                var flag = row.Value.OfType<BoolPropertyData>().Single(property => property.Name.ToString() == "DefaultValueBool");
                if (!flag.Value) throw new InvalidDataException($"Story flag {row.Name} did not serialize as unlocked.");
            }
        }
        // The complete parsed table must match the source after masking only requested defaults.
        // Source snapshots are captured before mutation; captured property names/types/order remain stable.
        for (var index = 0; index < after.Count; index++)
        {
            var expected = JsonDocument.Parse(before[index]);
            var actual = JsonDocument.Parse(SnapshotRow(after[index], targetRows));
            if (!JsonElement.DeepEquals(expected.RootElement, actual.RootElement))
                throw new InvalidDataException($"Story flag DataTable row {after[index].Name} changed outside the requested equipment-slot unlock defaults.");
        }
    }


    // Check patch targets

    private static string[] ValidateTargets(string[]? requested)
    {
        if (requested is null || requested.Length == 0)
            throw new InvalidDataException("At least one equipment-slot StoryFlag must be requested.");
        if (requested.Distinct(StringComparer.Ordinal).Count() != requested.Length)
            throw new InvalidDataException("Equipment-slot StoryFlag names must be unique.");
        var unknown = requested.Where(row => !SupportedRows.Contains(row, StringComparer.Ordinal)).ToArray();
        if (unknown.Length > 0)
            throw new InvalidDataException("Unsupported equipment-slot StoryFlags: " + string.Join(", ", unknown));
        return SupportedRows.Where(row => requested.Contains(row, StringComparer.Ordinal)).ToArray();
    }


    // Copy package sidecars

    private static void CopySidecars(string input, string temporaryHeader)
    {
        foreach (var source in PackageFiles(input).Where(File.Exists))
        {
            if (String.Equals(source, input, StringComparison.OrdinalIgnoreCase)) continue;
            var target = Path.ChangeExtension(temporaryHeader, Path.GetExtension(source));
            if (File.Exists(target)) continue;
            File.Copy(source, target);
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
        return relative != "." && !Path.IsPathRooted(relative) && relative != ".." && !relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    }


    // Read the package patch request

    private sealed record PatchRequest(string VirtualPackage, string InputFile, string OutputFile, string[]? TargetFlags);
}
