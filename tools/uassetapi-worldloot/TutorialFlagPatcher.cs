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

internal static class TutorialFlagPatcher
{
    private static readonly string[] SupportedTables = [
        "/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/DT_StoryFlagTutorialDefine",
        "/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/DT_StoryFlagTutorialDefine_DLC",
        "/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/AfterEnding/DT_StoryFlagTutorialDefine_AfterEnding",
    ];


    // Patch

    public static int Patch(string[] args)
    {
        var committed = new List<string>();
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var request = JsonSerializer.Deserialize<PatchRequest>(File.ReadAllText(args[4]),
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
                ?? throw new InvalidDataException("Tutorial flag patch request is empty.");
            var tables = ValidateRequests(request.Tables, workRoot);
            var patchedCount = 0;
            foreach (var tableRequest in tables)
            {
                var input = Path.GetFullPath(tableRequest.InputFile);
                var output = Path.GetFullPath(tableRequest.OutputFile);
                if (!File.Exists(input) || File.Exists(output))
                    throw new InvalidDataException("Tutorial table output must be new and its source must exist.");
                var expectedTableName = Path.GetFileNameWithoutExtension(input);
                var asset = new UAsset(input, engineVersion, mappings);
                var table = ResolveTable(asset, input, expectedTableName);
                var rows = ReadRows(table);
                var rowNames = rows.Select(row => row.Name.ToString()).ToArray();
                if (rowNames.Length == 0 || rowNames.Any(name => !name.StartsWith("Tutorial.", StringComparison.Ordinal)))
                    throw new InvalidDataException($"{expectedTableName} contains an empty or non-Tutorial StoryFlag row.");

                foreach (var row in rows)
                {
                    var flagType = row.Value.OfType<EnumPropertyData>().SingleOrDefault(property => property.Name.ToString() == "FlagType");
                    var defaultFlag = row.Value.OfType<BoolPropertyData>().SingleOrDefault(property => property.Name.ToString() == "DefaultValueBool");
                    var keepValue = row.Value.OfType<BoolPropertyData>().SingleOrDefault(property => property.Name.ToString() == "IsInitToKeepValue");
                    if (flagType?.Value.ToString() != "Boolean" || defaultFlag is null || keepValue is null)
                        throw new InvalidDataException($"Tutorial StoryFlag {row.Name} no longer matches the audited Boolean/default-retention schema.");
                }

                var snapshots = rows.Select(SnapshotRow).ToArray();
                foreach (var row in rows)
                {
                    row.Value.OfType<BoolPropertyData>().Single(property => property.Name.ToString() == "DefaultValueBool").Value = true;
                    // Ignore a previously saved unseen value and initialize it as viewed too.
                    row.Value.OfType<BoolPropertyData>().Single(property => property.Name.ToString() == "IsInitToKeepValue").Value = false;
                }
                patchedCount += rows.Count;

                var directory = Path.GetDirectoryName(output)!;
                Directory.CreateDirectory(directory);
                var temporaryHeader = Path.Combine(directory,
                    Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + ".uasset");
                try
                {
                    asset.Write(temporaryHeader);
                    CopySidecars(input, temporaryHeader);
                    var reopened = new UAsset(temporaryHeader, engineVersion, mappings);
                    ValidateRows(snapshots, ReadRows(ResolveTable(reopened, temporaryHeader, expectedTableName)));
                    var temporaryFiles = PackageFiles(temporaryHeader).Where(File.Exists).ToArray();
                    foreach (var source in temporaryFiles)
                    {
                        var target = Path.ChangeExtension(output, Path.GetExtension(source));
                        if (File.Exists(target)) throw new IOException("Refusing to overwrite an existing tutorial table output sidecar.");
                    }
                    foreach (var source in temporaryFiles)
                    {
                        var target = Path.ChangeExtension(output, Path.GetExtension(source));
                        File.Move(source, target);
                        committed.Add(target);
                    }
                    var final = new UAsset(output, engineVersion, mappings);
                    ValidateRows(snapshots, ReadRows(ResolveTable(final, output, expectedTableName)));
                }
                finally
                {
                    foreach (var file in PackageFiles(temporaryHeader))
                        try { File.Delete(file); } catch (IOException) { }
                }
            }

            Console.WriteLine(JsonSerializer.Serialize(new {
                patchedTables = tables.Select(table => table.VirtualPackage).ToArray(),
                patchedFlagCount = patchedCount,
                outputFiles = committed,
                reopenedAndValidated = true,
            }));
            return 0;
        }
        catch (Exception error)
        {
            foreach (var file in committed)
                try { File.Delete(file); } catch (IOException) { }
            Console.Error.WriteLine(error);
            return 1;
        }
    }


    // Check package patch requests

    private static TableRequest[] ValidateRequests(TableRequest[]? requested, string workRoot)
    {
        if (requested is null || requested.Length == 0)
            throw new InvalidDataException("At least one tutorial StoryFlag table must be requested.");
        var supported = requested.Select(table => table.VirtualPackage).ToArray();
        if (supported.Distinct(StringComparer.Ordinal).Count() != supported.Length ||
            supported.Any(path => !SupportedTables.Contains(path, StringComparer.Ordinal)) ||
            !supported.Contains(SupportedTables[0], StringComparer.Ordinal))
            throw new InvalidDataException("Tutorial StoryFlag requests must be unique supported tables and include the base table.");
        var stable = SupportedTables.Where(supported.Contains).ToArray();
        if (!stable.SequenceEqual(supported, StringComparer.Ordinal))
            throw new InvalidDataException("Tutorial StoryFlag tables must use the audited package order.");
        foreach (var table in requested)
        {
            var input = Path.GetFullPath(table.InputFile);
            var output = Path.GetFullPath(table.OutputFile);
            if (!IsWithin(input, workRoot) || !IsWithin(output, workRoot) || input == output ||
                !Path.GetFileNameWithoutExtension(input).Equals(Path.GetFileName(table.VirtualPackage), StringComparison.Ordinal) ||
                !Path.GetFileNameWithoutExtension(output).Equals(Path.GetFileName(table.VirtualPackage), StringComparison.Ordinal))
                throw new InvalidDataException("Tutorial StoryFlag source/output paths do not match their package identity or workspace.");
        }
        return requested;
    }


    // Find the target data table

    private static DataTableExport ResolveTable(UAsset asset, string input, string tableName)
    {
        var matches = asset.Exports.Select((export, index) => (export, index))
            .Where(item => item.export.ObjectName.ToString() == tableName).ToArray();
        if (matches.Length == 1 && matches[0].export is not DataTableExport)
        {
            using var reader = asset.PathToReader(input, true);
            asset.ParseExport(reader, matches[0].index, true);
        }
        var tables = asset.Exports.OfType<DataTableExport>()
            .Where(export => export.ObjectName.ToString() == tableName).ToArray();
        if (tables.Length != 1 || tables[0].Table?.Data is null)
            throw new InvalidDataException($"Expected exactly one parsed {tableName} DataTable export.");
        return tables[0];
    }


    // Read package table rows

    private static List<StructPropertyData> ReadRows(DataTableExport table)
    {
        var rows = new List<StructPropertyData>();
        foreach (var raw in (IEnumerable)table.Table!.Data)
            rows.Add(raw as StructPropertyData ?? throw new InvalidDataException("Tutorial DataTable row is not a parsed struct."));
        if (rows.Select(row => row.Name.ToString()).Distinct(StringComparer.Ordinal).Count() != rows.Count)
            throw new InvalidDataException("Tutorial DataTable contains duplicate row names.");
        return rows;
    }


    // Capture serialized row state

    private static string SnapshotRow(StructPropertyData row) => JsonSerializer.Serialize(new {
        name = row.Name.ToString(),
        properties = row.Value.Select(property => SnapshotProperty(
            property,
            maskDefault: property.Name.ToString() is "DefaultValueBool" or "IsInitToKeepValue"
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
        if (maskDefault) serialized = "<tutorial-default>";
        return new { name = property.Name.ToString(), type = property.GetType().Name, value = serialized };
    }


    // Check package table rows

    private static void ValidateRows(string[] snapshots, List<StructPropertyData> actualRows)
    {
        if (snapshots.Length != actualRows.Count) throw new InvalidDataException("Tutorial patch changed a DataTable row count.");
        for (var index = 0; index < actualRows.Count; index++)
        {
            var row = actualRows[index];
            if (!row.Name.ToString().StartsWith("Tutorial.", StringComparison.Ordinal))
                throw new InvalidDataException($"Unexpected non-tutorial StoryFlag row {row.Name} after serialization.");
            if (!row.Value.OfType<BoolPropertyData>().Single(property => property.Name.ToString() == "DefaultValueBool").Value ||
                row.Value.OfType<BoolPropertyData>().Single(property => property.Name.ToString() == "IsInitToKeepValue").Value)
                throw new InvalidDataException($"Tutorial StoryFlag {row.Name} was not initialized as viewed.");
            var expected = JsonDocument.Parse(snapshots[index]);
            var actual = JsonDocument.Parse(SnapshotRow(row));
            if (!JsonElement.DeepEquals(expected.RootElement, actual.RootElement))
                throw new InvalidDataException($"Tutorial DataTable row {row.Name} changed outside its viewed defaults.");
        }
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
        return relative != "." && !Path.IsPathRooted(relative) && relative != ".." &&
            !relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    }


    // Read the package patch request

    private sealed record PatchRequest(TableRequest[]? Tables);
    // Describe a data table request
    private sealed record TableRequest(string VirtualPackage, string InputFile, string OutputFile);
}
