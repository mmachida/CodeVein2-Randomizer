using System.Collections;
using System.Globalization;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class EnemyDropPatcher
{
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
                ?? throw new InvalidDataException("Enemy Drop inspection manifest is empty.");
            var tables = RequireManifestTables(manifest.Tables, "inspection");
            ValidateManifestTables(tables.Select(table => table.Table));
            var results = tables
                .OrderBy(table => TableOrder(table.Table))
                .Select(table => InspectTable(workRoot, table, engineVersion, mappings))
                .ToArray();
            Console.WriteLine(JsonSerializer.Serialize(results, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(exception.ToString());
            return 1;
        }
    }


    // Patch

    public static int Patch(string[] args)
    {
        var createdOutputs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<PatchManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Enemy Drop patch manifest is empty.");
            var patchRequests = RequireManifestTables(manifest.Tables, "patch");
            ValidatePatchTables(patchRequests.Select(table => table.Table));
            var sourceRequests = manifest.SourceTables ?? patchRequests
                .Select(table => new SourceTableRequest(table.Table, table.InputFile)).ToList();
            if (sourceRequests.Any(table => table is null))
                throw new InvalidDataException("Enemy Drop sourceTables contains a null request.");
            ValidateAvailableTables(sourceRequests.Select(table => table.Table), "source", requireBase: false);
            var sourceTables = new Dictionary<string, SourceTableData>(StringComparer.OrdinalIgnoreCase);
            foreach (var sourceRequest in sourceRequests)
            {
                var sourcePath = ResolveTablePackage(workRoot, sourceRequest.InputFile, $"{sourceRequest.Table} assignment source");
                var sourceAsset = new UAsset(sourcePath, engineVersion, mappings);
                var sourceTable = ResolveDataTable(sourceAsset, DataTableExportName(sourceRequest.Table));
                var sourceRows = ToRowMap(ReadRows(sourceTable), sourceRequest.Table);
                if (!sourceTables.TryAdd(sourceRequest.Table, new SourceTableData(sourceAsset, sourceRows)))
                    throw new InvalidDataException($"Duplicate Enemy Drop source table {sourceRequest.Table}.");
            }
            var results = new List<PatchResult>();
            foreach (var table in patchRequests.OrderBy(table => TableOrder(table.Table)))
            {
                var result = PatchTable(workRoot, table, engineVersion, mappings, sourceTables);
                foreach (var output in result.OutputFiles)
                    createdOutputs.Add(Path.GetFullPath(output));
                results.Add(result);
            }
            Console.WriteLine(JsonSerializer.Serialize(results, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            foreach (var path in createdOutputs)
            {
                try { File.Delete(path); } catch (IOException) { }
            }
            Console.Error.WriteLine(exception.ToString());
            return 1;
        }
    }


    // Inspect the data table

    private static InspectionResult InspectTable(
        string workRoot,
        InspectionRequest request,
        EngineVersion engineVersion,
        Usmap mappings)
    {
        var inputPath = ResolveTablePackage(workRoot, request.InputFile, $"{request.Table} inspection source");
        var asset = new UAsset(inputPath, engineVersion, mappings);
        var table = ResolveDataTable(asset, DataTableExportName(request.Table));
        var rows = ReadRows(table);
        return new InspectionResult(
            request.Table,
            inputPath,
            rows.Count,
            rows.Sum(row => row.Slots.Count),
            rows.Select(row => new RowInspection(
                row.RowId,
                row.Slots.Count,
                row.Slots.Select(slot => slot.Inspection).ToArray())).ToArray());
    }


    // Patch the data table

    private static PatchResult PatchTable(
        string workRoot,
        TablePatchRequest request,
        EngineVersion engineVersion,
        Usmap mappings,
        IReadOnlyDictionary<string, SourceTableData> sourceTables)
    {
        if (request.Assignments is null || request.Assignments.Count == 0)
            throw new InvalidDataException($"{request.Table} patch request contains no assignments.");
        var inputPath = ResolveTablePackage(workRoot, request.InputFile, $"{request.Table} patch source");
        var outputPath = ResolveOwnedPath(workRoot, request.OutputFile, mustExist: false);
        if (String.Equals(inputPath, outputPath, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Enemy Drop output must be separate from its source package.");
        if (File.Exists(outputPath))
            throw new IOException($"Refusing to overwrite staged Enemy Drop output {Path.GetFileName(outputPath)}.");

        var asset = new UAsset(inputPath, engineVersion, mappings);
        var table = ResolveDataTable(asset, DataTableExportName(request.Table));
        var beforeRows = ReadRows(table);
        var beforeByRow = ToRowMap(beforeRows, request.Table);
        var seenDestinations = new HashSet<(string Row, int Slot)>();
        var sourceBundles = new Dictionary<(string Row, int Slot), ArrayPropertyData>();
        var expectedBundles = new Dictionary<(string Row, int Slot), string>();
        var expectedWeights = new Dictionary<(string Row, int Slot), string>();
        var expectedNonReward = new Dictionary<(string Row, int Slot), string>();

        foreach (var assignment in request.Assignments)
        {
            if (assignment is null)
                throw new InvalidDataException($"Enemy Drop table {request.Table} contains a null assignment.");
            if (assignment.DestinationSlotIndex < 0 || assignment.SourceSlotIndex < 0)
                throw new InvalidDataException("Enemy Drop assignment indexes must be non-negative.");
            var destinationRow = FindRow(beforeByRow, assignment.DestinationRowId);
            var sourceTableName = assignment.SourceTable ?? request.Table;
            if (!sourceTables.TryGetValue(sourceTableName, out var sourceTable))
                throw new InvalidDataException($"Enemy Drop source table {sourceTableName} is not available.");
            var sourceRow = FindRow(sourceTable.Rows, assignment.SourceRowId);
            var destination = FindSlot(destinationRow, assignment.DestinationSlotIndex);
            var source = FindSlot(sourceRow, assignment.SourceSlotIndex);
            if (!seenDestinations.Add((destinationRow.RowId, assignment.DestinationSlotIndex)))
                throw new InvalidDataException("Enemy Drop plan contains a duplicate destination slot.");
            if (!String.Equals(destination.BundleFingerprint, assignment.ExpectedDestinationFingerprint, StringComparison.Ordinal)
                || !String.Equals(source.BundleFingerprint, assignment.ExpectedSourceFingerprint, StringComparison.Ordinal))
                throw new InvalidDataException("Enemy Drop source package no longer matches the validated plan fingerprints.");
            var copiedArticles = (ArrayPropertyData)source.Articles.Clone();
            var context = $"{request.Table}:{destinationRow.RowId}[{assignment.DestinationSlotIndex}] <- " +
                $"{sourceTableName}:{sourceRow.RowId}[{assignment.SourceSlotIndex}]";
            try
            {
                BindPropertyNames(copiedArticles, asset, context);
            }
            catch (Exception exception)
            {
                throw new InvalidDataException($"Could not rebind Article property names for {context}: {exception.Message}", exception);
            }
            sourceBundles[(assignment.DestinationRowId, assignment.DestinationSlotIndex)] = copiedArticles;
            expectedBundles[(assignment.DestinationRowId, assignment.DestinationSlotIndex)] = source.BundleFingerprint;
            expectedWeights[(assignment.DestinationRowId, assignment.DestinationSlotIndex)] = destination.WeightFingerprint;
            expectedNonReward[(assignment.DestinationRowId, assignment.DestinationSlotIndex)] = destination.NonRewardFingerprint;
        }

        foreach (var assignment in request.Assignments)
        {
            var destinationRow = FindRow(beforeByRow, assignment.DestinationRowId);
            var destination = FindSlot(destinationRow, assignment.DestinationSlotIndex);
            var articlesIndex = FindPropertyIndex(destination.ArticleBulk.Value, "Articles");
            destination.ArticleBulk.Value[articlesIndex] = sourceBundles[(assignment.DestinationRowId, assignment.DestinationSlotIndex)];
        }

        var outputDirectory = Path.GetDirectoryName(outputPath)!;
        Directory.CreateDirectory(outputDirectory);
        var tempStem = Path.GetFileNameWithoutExtension(outputPath) + ".cv2-" + Guid.NewGuid().ToString("N");
        var temporaryHeader = Path.Combine(outputDirectory, tempStem + Path.GetExtension(outputPath));
        var committed = new List<string>();
        try
        {
            asset.Write(temporaryHeader);
            var reopened = new UAsset(temporaryHeader, engineVersion, mappings);
            {
                var reopenedTable = ResolveDataTable(reopened, DataTableExportName(request.Table));
                var afterRows = ReadRows(reopenedTable);
                ValidateReopenedRows(
                    request.Table,
                    beforeRows,
                    afterRows,
                    expectedBundles,
                    expectedWeights,
                    expectedNonReward);
            }

            var temporaryFiles = PackageFiles(temporaryHeader).Where(File.Exists).ToArray();
            if (!temporaryFiles.Contains(temporaryHeader, StringComparer.OrdinalIgnoreCase))
                throw new InvalidDataException("UAssetAPI did not write an Enemy Drop DataTable header.");
            if (File.Exists(Path.ChangeExtension(inputPath, ".uexp")) && !File.Exists(Path.ChangeExtension(temporaryHeader, ".uexp")))
                throw new InvalidDataException("UAssetAPI omitted the source DataTable .uexp sidecar.");
            var outputFiles = temporaryFiles.Select(path => (Source: path, Target: Path.ChangeExtension(outputPath, Path.GetExtension(path)))).ToArray();
            if (outputFiles.Any(pair => File.Exists(pair.Target)))
                throw new IOException("Refusing to overwrite an Enemy Drop package sidecar.");
            foreach (var pair in outputFiles)
            {
                File.Move(pair.Source, pair.Target);
                committed.Add(pair.Target);
            }
            var final = new UAsset(outputPath, engineVersion, mappings);
            var finalRows = ReadRows(ResolveDataTable(final, DataTableExportName(request.Table)));
            ValidateReopenedRows(
                request.Table,
                beforeRows,
                finalRows,
                expectedBundles,
                expectedWeights,
                expectedNonReward);
            var changed = expectedBundles.Count(pair =>
                beforeByRow[pair.Key.Row].Slots[pair.Key.Slot].BundleFingerprint != pair.Value);
            return new PatchResult(
                request.Table,
                inputPath,
                outputPath,
                beforeRows.Count,
                beforeRows.Sum(row => row.Slots.Count),
                changed,
                expectedBundles.Count - changed,
                finalRows.Select(row => new RowInspection(
                    row.RowId,
                    row.Slots.Count,
                    row.Slots.Select(slot => slot.Inspection).ToArray())).ToArray(),
                committed.ToArray());
        }
        catch
        {
            foreach (var path in committed)
                try { File.Delete(path); } catch (IOException) { }
            throw;
        }
        finally
        {
            foreach (var path in PackageFiles(temporaryHeader))
                try { File.Delete(path); } catch (IOException) { }
        }
    }


    // Bind property names

    private static void BindPropertyNames(PropertyData? property, UAsset target, string context)
    {
        if (property is null)
            throw new InvalidDataException($"{context} contains a null Article property.");
        foreach (var field in property.GetType().GetFields(BindingFlags.Public | BindingFlags.Instance))
        {
            if (field.FieldType == typeof(FName) && field.GetValue(property) is FName name)
                field.SetValue(property, new FName(target, name.Value.ToString(), name.Number));
        }
        var typeName = property.PropertyTypeName;
        var typeNodes = typeName?.Nodes;
        if (typeNodes is not null)
        {
            for (var index = 0; index < typeNodes.Count; index++)
            {
                var node = typeNodes[index];
                node.Name = new FName(target, node.Name.Value.ToString(), node.Name.Number);
                typeNodes[index] = node;
            }
            property.PropertyTypeName = typeName;
        }

        if (property is StructPropertyData structure)
        {
            foreach (var child in structure.Value)
                BindPropertyNames(child, target, $"{context}.{child?.Name}");
            return;
        }
        if (property is ArrayPropertyData array)
        {
            if (array.DummyStruct is not null)
                BindPropertyNames(array.DummyStruct, target, $"{context}.<element-type>");
            var values = array.Value;
            for (var index = 0; index < values.Length; index++)
                BindPropertyNames(values[index], target, $"{context}[{index}]");
            return;
        }
        if (property is SoftObjectPropertyData softObject && softObject.Value is FSoftObjectPath path)
        {
            path.AssetPath = new FTopLevelAssetPath
            {
                PackageName = new FName(target, path.AssetPath.PackageName.Value.ToString(), path.AssetPath.PackageName.Number),
                AssetName = new FName(target, path.AssetPath.AssetName.Value.ToString(), path.AssetPath.AssetName.Number),
            };
            softObject.Value = path;
        }
    }


    // Check reopened rows

    private static void ValidateReopenedRows(
        string table,
        IReadOnlyList<RowData> before,
        IReadOnlyList<RowData> after,
        IReadOnlyDictionary<(string Row, int Slot), string> expectedBundles,
        IReadOnlyDictionary<(string Row, int Slot), string> expectedWeights,
        IReadOnlyDictionary<(string Row, int Slot), string> expectedNonReward)
    {
        if (before.Count != after.Count)
            throw new InvalidDataException($"{table} row count changed after serialization.");
        for (var rowIndex = 0; rowIndex < before.Count; rowIndex++)
        {
            var original = before[rowIndex];
            var patched = after[rowIndex];
            if (!String.Equals(original.RowId, patched.RowId, StringComparison.Ordinal)
                || original.Slots.Count != patched.Slots.Count)
                throw new InvalidDataException($"{table} row identity/order or slot count changed after serialization.");
            for (var slotIndex = 0; slotIndex < original.Slots.Count; slotIndex++)
            {
                var oldSlot = original.Slots[slotIndex];
                var newSlot = patched.Slots[slotIndex];
                var key = (original.RowId, slotIndex);
                if (!String.Equals(oldSlot.WeightFingerprint, newSlot.WeightFingerprint, StringComparison.Ordinal))
                    throw new InvalidDataException($"{table}:{original.RowId}[{slotIndex}] Weight changed.");
                if (!String.Equals(oldSlot.NonRewardFingerprint, newSlot.NonRewardFingerprint, StringComparison.Ordinal))
                    throw new InvalidDataException($"{table}:{original.RowId}[{slotIndex}] non-reward properties changed.");
                var expected = expectedBundles.TryGetValue(key, out var assigned) ? assigned : oldSlot.BundleFingerprint;
                if (!String.Equals(expected, newSlot.BundleFingerprint, StringComparison.Ordinal))
                    throw new InvalidDataException($"{table}:{original.RowId}[{slotIndex}] reward bundle does not match the verified assignment.");
                if (expectedWeights.TryGetValue(key, out var weight) && !String.Equals(weight, newSlot.WeightFingerprint, StringComparison.Ordinal))
                    throw new InvalidDataException($"{table}:{original.RowId}[{slotIndex}] assigned destination Weight changed.");
                if (expectedNonReward.TryGetValue(key, out var nonReward) && !String.Equals(nonReward, newSlot.NonRewardFingerprint, StringComparison.Ordinal))
                    throw new InvalidDataException($"{table}:{original.RowId}[{slotIndex}] assigned destination non-reward properties changed.");
            }
        }
    }


    // Read package table rows

    private static List<RowData> ReadRows(DataTableExport table)
    {
        if (table.Table?.Data is null)
            throw new InvalidDataException("DataTable export has no parsed row data.");
        var rows = new List<RowData>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var row in table.Table.Data)
        {
            var rowId = row.Name.ToString();
            if (String.IsNullOrWhiteSpace(rowId) || !seen.Add(rowId))
                throw new InvalidDataException("DataTable contains an empty or duplicate row identity.");
            var infos = RequireProperty<ArrayPropertyData>(row.Value, "DropItemInfos", rowId);
            var slots = new List<SlotData>();
            var slotValues = infos.Value.ToArray();
            for (var slotIndex = 0; slotIndex < slotValues.Length; slotIndex++)
            {
                if (slotValues[slotIndex] is not StructPropertyData slot)
                    throw new InvalidDataException($"{rowId} DropItemInfos[{slotIndex}] is not a struct.");
                var weight = FindProperty(slot.Value, "Weight", rowId);
                var bulk = RequireProperty<StructPropertyData>(slot.Value, "ArticleBulkInfo", rowId);
                var articles = RequireProperty<ArrayPropertyData>(bulk.Value, "Articles", rowId);
                slots.Add(new SlotData(
                    rowId,
                    slotIndex,
                    slot,
                    bulk,
                    articles,
                    Fingerprint(weight),
                    NonRewardFingerprint(slot),
                    Fingerprint(articles),
                    ReadRewardLabels(articles)));
            }
            rows.Add(new RowData(rowId, row, slots));
        }
        return rows;
    }


    // Read enemy reward labels

    private static string[] ReadRewardLabels(ArrayPropertyData articles)
    {
        var labels = new List<string>();
        foreach (var value in articles.Value)
        {
            if (value is not StructPropertyData article) continue;
            var paths = article.Value.OfType<SoftObjectPropertyData>()
                .Where(property => String.Equals(property.Name.ToString(), "ArticleDataPath", StringComparison.Ordinal))
                .ToArray();
            if (paths.Length != 1) continue;
            var path = (FSoftObjectPath)paths[0].Value!;
            var label = path.AssetPath.AssetName.ToString();
            if (!String.IsNullOrWhiteSpace(label) && !String.Equals(label, "None", StringComparison.OrdinalIgnoreCase))
                labels.Add(label);
        }
        return labels.ToArray();
    }


    // Find the source data table

    private static DataTableExport ResolveDataTable(UAsset asset, string expectedTable)
    {
        var matches = asset.Exports.OfType<DataTableExport>()
            .Where(export => String.Equals(export.ObjectName.ToString(), expectedTable, StringComparison.OrdinalIgnoreCase))
            .ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"Expected exactly one DataTable export named {expectedTable}; found {matches.Length}.");
        return matches[0];
    }


    // Read to row Map

    private static Dictionary<string, RowData> ToRowMap(IEnumerable<RowData> rows, string table)
    {
        var result = new Dictionary<string, RowData>(StringComparer.OrdinalIgnoreCase);
        foreach (var row in rows)
            if (!result.TryAdd(row.RowId, row))
                throw new InvalidDataException($"{table} contains duplicate row {row.RowId}.");
        return result;
    }


    // Find a table row

    private static RowData FindRow(IReadOnlyDictionary<string, RowData> rows, string rowId) =>
        rows.TryGetValue(rowId, out var row)
            ? row
            : throw new InvalidDataException($"DataTable row {rowId} was not found.");


    // Find the matching reward slot

    private static SlotData FindSlot(RowData row, int slotIndex) =>
        slotIndex >= 0 && slotIndex < row.Slots.Count
            ? row.Slots[slotIndex]
            : throw new InvalidDataException($"{row.RowId} DropItemInfos slot {slotIndex} is outside the table.");

    private static T RequireProperty<T>(IEnumerable<PropertyData> properties, string name, string row)
        where T : PropertyData
    {
        var value = FindProperty(properties, name, row);
        return value as T ?? throw new InvalidDataException($"{row}.{name} has type {value.GetType().Name}, expected {typeof(T).Name}.");
    }


    // Find the named property

    private static PropertyData FindProperty(IEnumerable<PropertyData> properties, string name, string row)
    {
        var matches = properties.Where(property => String.Equals(property.Name.ToString(), name, StringComparison.Ordinal)).ToArray();
        return matches.Length == 1
            ? matches[0]
            : throw new InvalidDataException($"{row} must contain exactly one {name} property; found {matches.Length}.");
    }


    // Find the property index

    private static int FindPropertyIndex(IReadOnlyList<PropertyData> properties, string name)
    {
        var matches = Enumerable.Range(0, properties.Count)
            .Where(index => String.Equals(properties[index].Name.ToString(), name, StringComparison.Ordinal))
            .ToArray();
        return matches.Length == 1
            ? matches[0]
            : throw new InvalidDataException($"ArticleBulkInfo must contain exactly one {name} property; found {matches.Length}.");
    }


    // Fingerprint unchanged table data

    private static string NonRewardFingerprint(StructPropertyData slot)
    {
        var projection = new List<object?>();
        foreach (var property in slot.Value)
        {
            if (!String.Equals(property.Name.ToString(), "ArticleBulkInfo", StringComparison.Ordinal))
            {
                projection.Add(Canonicalize(property));
                continue;
            }
            if (property is not StructPropertyData bulk)
                throw new InvalidDataException("ArticleBulkInfo is not a struct.");
            projection.Add(new SortedDictionary<string, object?>(StringComparer.Ordinal)
            {
                ["type"] = bulk.GetType().Name,
                ["name"] = bulk.Name.ToString(),
                ["value"] = bulk.Value
                    .Where(value => !String.Equals(value.Name.ToString(), "Articles", StringComparison.Ordinal))
                    .Select(value => Canonicalize(value))
                    .ToArray(),
            });
        }
        return Fingerprint(projection);
    }


    // Read fingerprint

    private static string Fingerprint(object? value)
    {
        var canonical = Canonicalize(value);
        var json = JsonSerializer.Serialize(canonical);
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(json))).ToLowerInvariant();
    }


    // Normalize serialized data

    private static object? Canonicalize(object? value, HashSet<object>? visited = null, int depth = 0)
    {
        if (value is null) return null;
        if (depth > 48) throw new InvalidDataException("DataTable property nesting exceeds the canonicalization safety limit.");
        if (value is string or bool or byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal)
            return value;
        if (value is char character) return character.ToString();
        if (value is Enum enumValue) return enumValue.ToString();
        if (value is FName fname) return fname.ToString();
        if (value is byte[] bytes) return Convert.ToHexString(bytes).ToLowerInvariant();

        visited ??= new HashSet<object>(ReferenceEqualityComparer.Instance);
        var type = value.GetType();
        if (!type.IsValueType && !visited.Add(value)) return $"<cycle:{type.FullName}>";
        try
        {
            if (value is PropertyData property)
            {
                var valueProperty = type.GetProperty("Value", BindingFlags.Public | BindingFlags.Instance)
                    ?? throw new InvalidDataException($"{type.Name} has no readable Value property.");
                return new SortedDictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["type"] = type.FullName ?? type.Name,
                    ["name"] = property.Name.ToString(),
                    ["value"] = Canonicalize(valueProperty.GetValue(property), visited, depth + 1),
                };
            }
            if (value is IDictionary dictionary)
            {
                var result = new SortedDictionary<string, object?>(StringComparer.Ordinal);
                foreach (DictionaryEntry entry in dictionary)
                    result[Convert.ToString(entry.Key, CultureInfo.InvariantCulture) ?? ""] = Canonicalize(entry.Value, visited, depth + 1);
                return result;
            }
            if (value is IEnumerable enumerable)
                return enumerable.Cast<object?>().Select(item => Canonicalize(item, visited, depth + 1)).ToArray();

            var members = new SortedDictionary<string, object?>(StringComparer.Ordinal);
            foreach (var field in type.GetFields(BindingFlags.Public | BindingFlags.Instance).OrderBy(member => member.Name, StringComparer.Ordinal))
            {
                if (field.IsStatic || IsNavigationMember(field.Name)) continue;
                members[field.Name] = Canonicalize(field.GetValue(value), visited, depth + 1);
            }
            foreach (var propertyInfo in type.GetProperties(BindingFlags.Public | BindingFlags.Instance)
                         .Where(propertyInfo => propertyInfo.CanRead && propertyInfo.GetIndexParameters().Length == 0)
                         .OrderBy(member => member.Name, StringComparer.Ordinal))
            {
                if (IsNavigationMember(propertyInfo.Name) || members.ContainsKey(propertyInfo.Name)) continue;
                try { members[propertyInfo.Name] = Canonicalize(propertyInfo.GetValue(value), visited, depth + 1); }
                catch (TargetInvocationException exception)
                {
                    throw new InvalidDataException($"Could not canonicalize {type.Name}.{propertyInfo.Name}.", exception.InnerException ?? exception);
                }
            }
            if (members.Count == 0)
                return value.ToString() ?? type.FullName ?? type.Name;
            return new SortedDictionary<string, object?>(StringComparer.Ordinal)
            {
                ["type"] = type.FullName ?? type.Name,
                ["members"] = members,
            };
        }
        finally
        {
            if (!type.IsValueType) visited.Remove(value);
        }
    }


    // Check for a navigation property

    private static bool IsNavigationMember(string name) =>
        name is "Asset" or "Owner" or "UAsset" or "Parent" or "Outer" or "Package";


    // Resolve an output path

    private static string ResolveOwnedPath(string workRoot, string value, bool mustExist)
    {
        var root = Path.GetFullPath(workRoot);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(root, path);
        if (relative == "." || relative == ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal)
            || Path.IsPathRooted(relative))
            throw new InvalidDataException("Enemy Drop package path escaped its generation workspace.");
        if (mustExist && !File.Exists(path)) throw new FileNotFoundException("Enemy Drop package was not found.", path);
        return path;
    }


    // Resolve the data table package

    private static string ResolveTablePackage(string workRoot, string value, string context)
    {
        var path = ResolveOwnedPath(workRoot, value, mustExist: true);
        if (!String.Equals(Path.GetExtension(path), ".uasset", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"{context} must be a .uasset file.");
        if (!File.Exists(Path.ChangeExtension(path, ".uexp")))
            throw new FileNotFoundException($"{context} is missing its required .uexp sidecar.", Path.ChangeExtension(path, ".uexp"));
        return path;
    }


    // Collect package files

    private static string[] PackageFiles(string header)
    {
        var directory = Path.GetDirectoryName(header)!;
        var stem = Path.GetFileNameWithoutExtension(header);
        return new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" }
            .Select(extension => Path.Combine(directory, stem + extension))
            .ToArray();
    }


    // Check manifest table paths

    private static void ValidateManifestTables(IEnumerable<string> tables)
    {
        ValidateAvailableTables(tables, "inspection", requireBase: false);
    }


    // Check requested patch tables

    private static void ValidatePatchTables(IEnumerable<string> tables)
    {
        ValidateAvailableTables(tables, "patch", requireBase: false);
    }


    // Check available data tables

    private static void ValidateAvailableTables(IEnumerable<string> tables, string operation, bool requireBase)
    {
        var supplied = tables.ToArray();
        var supported = new[] { "BASE", "AFTER_ENDING", "DLC" };
        if (supplied.Length is < 1 or > 3
            || supplied.Distinct(StringComparer.OrdinalIgnoreCase).Count() != supplied.Length
            || supplied.Any(table => !supported.Contains(table, StringComparer.OrdinalIgnoreCase))
            || (requireBase && !String.Equals(supplied[0], "BASE", StringComparison.OrdinalIgnoreCase))
            || !supplied.SequenceEqual(
                supported.Where(table => supplied.Contains(table, StringComparer.OrdinalIgnoreCase)),
                StringComparer.OrdinalIgnoreCase))
            throw new InvalidDataException(requireBase
                ? $"Enemy Drop {operation} requires BASE and any available optional tables in canonical order."
                : $"Enemy Drop {operation} tables must be a canonical subset of supported destination tables.");
    }

    private static List<T> RequireManifestTables<T>(List<T>? tables, string operation) where T : class
    {
        if (tables is null || tables.Count == 0)
            throw new InvalidDataException($"Enemy Drop {operation} manifest has no tables.");
        if (tables.Any(table => table is null))
            throw new InvalidDataException($"Enemy Drop {operation} manifest contains a null table request.");
        return tables;
    }


    // Sort tables by package path

    private static int TableOrder(string table) => table switch
    {
        "BASE" => 0,
        "AFTER_ENDING" => 1,
        "DLC" => 2,
        _ => throw new InvalidDataException($"Unknown Enemy Drop table {table}.")
    };


    // Read the data table export name

    private static string DataTableExportName(string table) => table switch
    {
        "BASE" => "DT_DropItemData",
        "AFTER_ENDING" => "DT_DropItemData_AfterEnding",
        "DLC" => "DT_DropItemData_DLC",
        _ => throw new InvalidDataException($"Unknown Enemy Drop table {table}.")
    };


    // Describe package inspection results

    private sealed record InspectionManifest(List<InspectionRequest> Tables);
    // Describe an inspection request
    private sealed record InspectionRequest(string Table, string InputFile);
    // Apply the patch manifest
    private sealed record PatchManifest(List<TablePatchRequest> Tables, List<SourceTableRequest>? SourceTables = null);
    // Describe source table request
    private sealed record SourceTableRequest(string Table, string InputFile);
    // Describe table patch request
    private sealed record TablePatchRequest(string Table, string InputFile, string OutputFile, List<DropAssignment> Assignments);
    // Describe source table data
    private sealed record SourceTableData(UAsset Asset, Dictionary<string, RowData> Rows);
    // Read drop assignment
    private sealed record DropAssignment(
        string DestinationRowId,
        int DestinationSlotIndex,
        string? SourceTable,
        string SourceRowId,
        int SourceSlotIndex,
        string ExpectedDestinationFingerprint,
        string ExpectedSourceFingerprint);
    // Describe inspection result
    private sealed record InspectionResult(string Table, string InputFile, int RowCount, int SlotCount, RowInspection[] Rows);
    // Describe an inspected row
    private sealed record RowInspection(string RowId, int SlotCount, SlotInspection[] Slots);
    // Describe slot inspection
    private sealed record SlotInspection(
        int SlotIndex,
        string WeightFingerprint,
        string NonRewardFingerprint,
        string BundleFingerprint,
        string[] RewardLabels);
    // Patch result
    private sealed record PatchResult(
        string Table,
        string InputFile,
        string OutputFile,
        int RowCount,
        int SlotCount,
        int ChangedSlots,
        int UnchangedSlots,
        RowInspection[] Rows,
        string[] OutputFiles);
    // Describe row data
    private sealed record RowData(string RowId, StructPropertyData Row, List<SlotData> Slots);
    // Describe slot data
    private sealed record SlotData(
        string RowId,
        int SlotIndex,
        StructPropertyData Slot,
        StructPropertyData ArticleBulk,
        ArrayPropertyData Articles,
        string WeightFingerprint,
        string NonRewardFingerprint,
        string BundleFingerprint,
        string[] RewardLabels)
    {
        public SlotInspection Inspection => new(SlotIndex, WeightFingerprint, NonRewardFingerprint, BundleFingerprint, RewardLabels);
    }
}
