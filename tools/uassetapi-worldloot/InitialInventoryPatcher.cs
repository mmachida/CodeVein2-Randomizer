using System.Collections;
using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class InitialInventoryPatcher
{
    private const string VirtualPackage = "/Game/Blueprint/Article/DA_InitialInventory_Player";
    private const string MotorcycleFormaPath = "/Game/Blueprint/Article/Item/Important/DA_Item_Important_SpellForMotorcycle";
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };
    private static readonly IReadOnlyDictionary<string, SlotSpec> Slots = new Dictionary<string, SlotSpec>(StringComparer.Ordinal)
    {
        ["starting_weapon"] = new("starting_weapon", "Starting Weapon", "Weapons[0].0.ArticlePath",
            "/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_Future_Lesser_1"),
        ["starting_defensive_gadget"] = new("starting_defensive_gadget", "Starting Defensive Gadget", "DefenceGadgets[0].0.ArticlePath",
            "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_LesserShield_1"),
        ["starting_jail"] = new("starting_jail", "Starting Jail", "Jails[0].0.ArticlePath",
            "/Game/Blueprint/Article/Jail/DA_Jail_Ogre"),
    };
    private static readonly PreservedSpec[] PreservedEntries =
    [
        new("DA_BodyLight.Items", "Items[1].1.ArticlePath", "/Game/Blueprint/Article/Item/DA_BodyLight"),
        new("DA_BodyLight.ItemPalette", "ItemPalette[0].0.ArticlePath", "/Game/Blueprint/Article/Item/DA_BodyLight"),
        new("DA_Item_BasicRecovery", "Items[0].0.ArticlePath", "/Game/Blueprint/Article/Item/Recover/DA_Item_BasicRecovery_1"),
        new("DA_BloodCode_Lou_01", "BloodCodes[0].0.ArticlePath", "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01"),
    ];


    // Inspect

    public static int Inspect(string[] args) => Run(args, patch: false);
    // Patch
    public static int Patch(string[] args) => Run(args, patch: true);


    // Run

    private static int Run(string[] args, bool patch)
    {
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappingsPath = Path.GetFullPath(args[2]);
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            if (!Directory.Exists(workRoot)) throw new DirectoryNotFoundException("Initial Inventory work root does not exist.");
            if (!File.Exists(mappingsPath)) throw new FileNotFoundException("CV2.usmap mapping was not found.", mappingsPath);
            var request = JsonSerializer.Deserialize<InitialInventoryRequest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Initial Inventory request is empty.");
            if (!String.Equals(request.VirtualPackage, VirtualPackage, StringComparison.Ordinal))
                throw new InvalidDataException("Initial Inventory request names an unexpected source package.");
            var input = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
            var mappings = new Usmap(mappingsPath);
            var asset = LoadPackage(input, engineVersion, mappings);
            ParseAllExports(asset, input);
            var scan = Scan(asset);
            ValidateExpectedSource(scan);
            if (!patch)
            {
                Console.WriteLine(JsonSerializer.Serialize(MakeInspection(request.VirtualPackage, input, scan), JsonOptions));
                return 0;
            }

            var output = ResolveOwnedPath(workRoot,
                request.OutputFile ?? throw new InvalidDataException("Initial Inventory patch request has no output file."),
                mustExist: false);
            if (String.Equals(input, output, StringComparison.OrdinalIgnoreCase) || File.Exists(output) || PackageFiles(output).Any(File.Exists))
                throw new IOException("Initial Inventory output must be a new staged package.");
            if (request.Assignments is null || (request.Assignments.Count != 0 && request.Assignments.Count != Slots.Count))
                throw new InvalidDataException("Initial Inventory patch request must assign either zero or all three confirmed starting slots.");
            if (request.Assignments.Count == 0 && !request.MotorcycleUnlocked)
                throw new InvalidDataException("Initial Inventory patch request contains no changed starting inventory slot.");

            var selected = new List<(InitialInventoryAssignment Assignment, SlotHit Hit)>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var assignment in request.Assignments)
            {
                if (!Slots.TryGetValue(assignment.SlotId, out var spec) || !seen.Add(assignment.SlotId))
                    throw new InvalidDataException("Initial Inventory assignment has an unknown or duplicate slot ID.");
                var hit = scan.Slots.Single(item => item.SlotId == spec.SlotId);
                if (!String.Equals(hit.ArticlePath, spec.VanillaArticlePath, StringComparison.Ordinal) ||
                    !String.Equals(assignment.ExpectedArticlePath, spec.VanillaArticlePath, StringComparison.Ordinal))
                    throw new InvalidDataException($"{spec.DisplayName} no longer has its confirmed vanilla ArticlePath.");
                ValidateArticlePath(assignment.AssignedArticlePath);
                if (!String.Equals(assignment.AssignedArticlePath, assignment.ExpectedArticlePath, StringComparison.Ordinal) &&
                    String.Equals(assignment.AssignedArticlePath, hit.ArticlePath, StringComparison.Ordinal))
                    throw new InvalidDataException("Initial Inventory assignment unexpectedly equals a different source identity.");
                selected.Add((assignment, hit));
            }

            var identities = ExportIdentities(asset);
            var masked = selected.Select(item => (PropertyData)item.Hit.Property).ToList();
            var itemRowsBefore = Array.Empty<string>();
            ArrayPropertyData? itemsArray = null;
            if (request.MotorcycleUnlocked)
            {
                itemsArray = FindItemsArray(asset);
                var originalItems = itemsArray.Value.OfType<StructPropertyData>().ToArray();
                if (originalItems.Length != 2)
                    throw new InvalidDataException($"Expected two vanilla starting inventory Items rows; found {originalItems.Length}.");
                if (originalItems.Any(item => String.Equals(ReadItemArticlePath(item), MotorcycleFormaPath, StringComparison.Ordinal)))
                    throw new InvalidDataException("Motorcycle Forma is already present in the vanilla starting Items list.");
                itemRowsBefore = originalItems.Select(item => CanonicalSnapshot(item)).ToArray();
                masked.Add(itemsArray);
            }
            var beforeSnapshots = SnapshotExports(asset,
                new HashSet<PropertyData>(masked, ReferenceEqualityComparer.Instance));
            var beforeImports = SnapshotImports(asset);
            foreach (var item in selected)
                SetSoftPath(asset, item.Hit.Property, item.Assignment.AssignedArticlePath);
            if (request.MotorcycleUnlocked)
                AddMotorcycleForma(asset, itemsArray!);
            var outputDirectory = Path.GetDirectoryName(output)!;
            Directory.CreateDirectory(outputDirectory);
            var tempHeader = Path.Combine(outputDirectory,
                Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(output));
            var moved = new List<string>();
            try
            {
                asset.Write(tempHeader);
                CopyAdditionalSidecars(input, tempHeader);
                ValidateReopened(tempHeader, request, engineVersion, mappings, identities, beforeSnapshots,
                    beforeImports, scan, selected, itemRowsBefore);
                var tempFiles = PackageFiles(tempHeader).Where(File.Exists).ToArray();
                ValidateRequiredSidecars(input, tempHeader);
                var outputFiles = tempFiles.Select(path => Path.ChangeExtension(output, Path.GetExtension(path))).ToArray();
                if (outputFiles.Any(File.Exists)) throw new IOException("Refusing to overwrite an Initial Inventory sidecar.");
                for (var index = 0; index < tempFiles.Length; index++)
                {
                    File.Move(tempFiles[index], outputFiles[index]);
                    moved.Add(outputFiles[index]);
                }
                ValidateReopened(output, request, engineVersion, mappings, identities, beforeSnapshots,
                    beforeImports, scan, selected, itemRowsBefore);
                var final = LoadPackage(output, engineVersion, mappings);
                ParseAllExports(final, output);
                var finalScan = Scan(final, selected.ToDictionary(
                    item => item.Assignment.SlotId,
                    item => item.Assignment.AssignedArticlePath,
                    StringComparer.Ordinal));
                Console.WriteLine(JsonSerializer.Serialize(new InitialInventoryPatchResult(
                    request.VirtualPackage, input, output, selected.Count, MakeInspection(request.VirtualPackage, output, finalScan),
                    outputFiles.Select(Path.GetFullPath).ToArray(), request.MotorcycleUnlocked,
                    request.MotorcycleUnlocked ? MotorcycleFormaPath : null, true), JsonOptions));
                return 0;
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
        catch (Exception exception)
        {
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Check reopened

    private static void ValidateReopened(
        string packageHeader,
        InitialInventoryRequest request,
        EngineVersion engineVersion,
        Usmap mappings,
        (string Name, string? Type)[] identities,
        Dictionary<int, string> beforeSnapshots,
        string beforeImports,
        InventoryScan beforeScan,
        IReadOnlyList<(InitialInventoryAssignment Assignment, SlotHit Hit)> selected,
        IReadOnlyList<string> itemRowsBefore)
    {
        var reopened = LoadPackage(packageHeader, engineVersion, mappings);
        ParseAllExports(reopened, packageHeader);
        if (!identities.SequenceEqual(ExportIdentities(reopened)))
            throw new InvalidDataException("Initial Inventory export identity, type, or order changed after serialization.");
        if (!String.Equals(beforeImports, SnapshotImports(reopened), StringComparison.Ordinal))
            throw new InvalidDataException("Initial Inventory package imports changed during ArticlePath patching.");
        var afterSelected = new HashSet<PropertyData>(
            selected.Select(item => (PropertyData)FindSlotAtAddress(reopened, item.Hit).Property),
            ReferenceEqualityComparer.Instance);
        if (request.MotorcycleUnlocked)
            afterSelected.Add(FindItemsArray(reopened));
        ValidateSnapshots(beforeSnapshots, SnapshotExports(reopened, afterSelected));

        var assignedBySlot = selected.ToDictionary(item => item.Assignment.SlotId, item => item.Assignment.AssignedArticlePath,
            StringComparer.Ordinal);
        var afterScan = Scan(reopened, assignedBySlot);
        foreach (var spec in Slots.Values)
        {
            var actual = afterScan.Slots.Single(item => item.SlotId == spec.SlotId);
            var expected = assignedBySlot.GetValueOrDefault(spec.SlotId, spec.VanillaArticlePath);
            if (!String.Equals(actual.ArticlePath, expected, StringComparison.Ordinal))
                throw new InvalidDataException($"Reopened {spec.DisplayName} ArticlePath differs from its requested identity.");
        }

        foreach (var identity in PreservedEntries.Select(item => item.Identity).Distinct(StringComparer.Ordinal))
        {
            var before = beforeScan.Preserved.Where(item => item.Identity == identity)
                .OrderBy(item => item.PropertyPath, StringComparer.Ordinal).ToArray();
            var after = afterScan.Preserved.Where(item => item.Identity == identity)
                .OrderBy(item => item.PropertyPath, StringComparer.Ordinal).ToArray();
            if (!before.SequenceEqual(after))
                throw new InvalidDataException($"Preserved Initial Inventory entry {identity} changed.");
        }
        if (request.MotorcycleUnlocked)
            ValidateMotorcycleFormaGrant(reopened, itemRowsBefore);
    }


    // Add the Motorcycle Forma

    private static void AddMotorcycleForma(UAsset asset, ArrayPropertyData itemsArray)
    {
        var originalItems = itemsArray.Value.OfType<StructPropertyData>().ToArray();
        var bodyLight = originalItems.SingleOrDefault(item =>
            String.Equals(ReadItemArticlePath(item), "/Game/Blueprint/Article/Item/DA_BodyLight", StringComparison.Ordinal))
            ?? throw new InvalidDataException("Could not locate the confirmed non-slot starting Item row to clone.");
        var motorcycleItem = bodyLight.Clone() as StructPropertyData
            ?? throw new InvalidDataException("Could not clone the starting inventory Item row.");
        var articlePaths = FindArticlePaths(new[] { motorcycleItem }, "Items[2]").ToArray();
        if (articlePaths.Length != 1)
            throw new InvalidDataException("Cloned starting Item row does not contain exactly one ArticlePath.");
        SetSoftPath(asset, articlePaths[0].Property, MotorcycleFormaPath);
        var count = motorcycleItem.Value.OfType<IntPropertyData>()
            .SingleOrDefault(property => property.Name.ToString() == "Count")
            ?? throw new InvalidDataException("Cloned starting Item row has no unique Count field.");
        count.Value = 1;
        itemsArray.Value = itemsArray.Value.Append((PropertyData)motorcycleItem).ToArray();
    }


    // Check the Motorcycle reward grant

    private static void ValidateMotorcycleFormaGrant(UAsset asset, IReadOnlyList<string> originalItemSnapshots)
    {
        var items = FindItemsArray(asset).Value.OfType<StructPropertyData>().ToArray();
        if (items.Length != originalItemSnapshots.Count + 1)
            throw new InvalidDataException("Motorcycle unlock must append exactly one new starting Item row.");
        for (var index = 0; index < originalItemSnapshots.Count; index++)
            if (!String.Equals(CanonicalSnapshot(items[index]), originalItemSnapshots[index], StringComparison.Ordinal))
                throw new InvalidDataException($"Existing initial inventory Items row {index} changed while adding Motorcycle Forma.");
        var added = items[^1];
        if (!String.Equals(ReadItemArticlePath(added), MotorcycleFormaPath, StringComparison.Ordinal))
            throw new InvalidDataException("The added starting Item is not the audited Motorcycle Forma Article.");
        var count = added.Value.OfType<IntPropertyData>()
            .SingleOrDefault(property => property.Name.ToString() == "Count");
        if (count?.Value != 1)
            throw new InvalidDataException("Starting Motorcycle Forma count is not exactly one.");
        if (items.Count(item => String.Equals(ReadItemArticlePath(item), MotorcycleFormaPath, StringComparison.Ordinal)) != 1)
            throw new InvalidDataException("Motorcycle Forma was duplicated in starting inventory.");
    }


    // Find the starting items array

    private static ArrayPropertyData FindItemsArray(UAsset asset)
    {
        var owners = asset.Exports.OfType<NormalExport>()
            .Where(export => export.ObjectName.ToString() == "DA_InitialInventory_Player").ToArray();
        if (owners.Length != 1)
            throw new InvalidDataException("Expected exactly one DA_InitialInventory_Player export.");
        return owners[0].Data.OfType<ArrayPropertyData>()
            .SingleOrDefault(property => property.Name.ToString() == "Items")
            ?? throw new InvalidDataException("DA_InitialInventory_Player has no unique Items array.");
    }


    // Read the item Article path

    private static string ReadItemArticlePath(StructPropertyData item)
    {
        var matches = FindArticlePaths(new[] { item }, "Item").ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException("Initial inventory Items row must contain exactly one ArticlePath.");
        return ReadArticlePath(matches[0].Property);
    }


    // Capture the canonical state

    private static string CanonicalSnapshot(PropertyData property) => JsonSerializer.Serialize(
        Canonicalize(property, new HashSet<PropertyData>(ReferenceEqualityComparer.Instance)));


    // Scan serialized package data

    private static InventoryScan Scan(
        UAsset asset,
        IReadOnlyDictionary<string, string>? expectedArticlePaths = null)
    {
        var all = new List<SlotHit>();
        for (var index = 0; index < asset.Exports.Count; index++)
        {
            if (asset.Exports[index] is not NormalExport export) continue;
            foreach (var (property, propertyPath) in FindArticlePaths(export.Data, ""))
            {
                var path = ReadArticlePath(property);
                all.Add(new SlotHit("", path, index + 1, export.ObjectName.ToString(), propertyPath, property));
            }
        }
        var slots = Slots.Values.Select(spec =>
        {
            var expected = expectedArticlePaths is not null && expectedArticlePaths.TryGetValue(spec.SlotId, out var requested)
                ? requested : spec.VanillaArticlePath;
            var matches = all.Where(item => String.Equals(item.PropertyPath, spec.PropertyPath, StringComparison.Ordinal)).ToArray();
            if (matches.Length != 1)
                throw new InvalidDataException($"Expected exactly one structurally located {spec.DisplayName} ArticlePath at {spec.PropertyPath}, found {matches.Length}.");
            var hit = matches[0];
            if (!String.Equals(hit.ArticlePath, expected, StringComparison.Ordinal))
                throw new InvalidDataException($"{spec.DisplayName} at {spec.PropertyPath} has unexpected ArticlePath {hit.ArticlePath}; expected {expected}.");
            return hit with { SlotId = spec.SlotId };
        }).ToArray();
        var preserved = PreservedEntries.Select(entry =>
        {
            var matches = all.Where(item => String.Equals(item.PropertyPath, entry.PropertyPath, StringComparison.Ordinal)).ToArray();
            if (matches.Length != 1)
                throw new InvalidDataException($"Expected exactly one preserved {entry.Identity} ArticlePath at {entry.PropertyPath}, found {matches.Length}.");
            var hit = matches[0];
            if (!String.Equals(hit.ArticlePath, entry.VanillaArticlePath, StringComparison.Ordinal))
                throw new InvalidDataException($"Preserved Initial Inventory entry {entry.Identity} has an unexpected ArticlePath.");
            return new PreservedHit(entry.Identity, hit.ArticlePath, hit.ExportIndex, hit.ExportName, hit.PropertyPath);
        }).ToArray();
        if (slots.Select(item => item.Property).Distinct(ReferenceEqualityComparer.Instance).Count() != Slots.Count)
            throw new InvalidDataException("Initial Inventory source slots do not point to distinct ArticlePath properties.");
        if (preserved.Any(item => slots.Any(slot => slot.ExportIndex == item.ExportIndex && slot.PropertyPath == item.PropertyPath)))
            throw new InvalidDataException("A required vanilla entry overlaps one of the three randomized slots.");
        return new InventoryScan(slots, preserved, all.Count);
    }


    // Find a slot at this address

    private static SlotHit FindSlotAtAddress(UAsset asset, SlotHit baseline)
    {
        if (baseline.ExportIndex < 1 || baseline.ExportIndex > asset.Exports.Count ||
            asset.Exports[baseline.ExportIndex - 1] is not NormalExport export ||
            !String.Equals(export.ObjectName.ToString(), baseline.ExportName, StringComparison.Ordinal))
            throw new InvalidDataException("Initial Inventory target export changed after serialization.");
        var matches = FindArticlePaths(export.Data, "")
            .Where(item => String.Equals(item.PropertyPath, baseline.PropertyPath, StringComparison.Ordinal))
            .ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException("Initial Inventory ArticlePath structural address changed after serialization.");
        var found = matches[0];
        return baseline with { ArticlePath = ReadArticlePath(found.Property), Property = found.Property };
    }

    private static IEnumerable<(SoftObjectPropertyData Property, string PropertyPath)> FindArticlePaths(
        IEnumerable<PropertyData> properties, string parentPath)
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
                    foreach (var nested in FindArticlePaths(new[] { child }, $"{propertyPath}[{childIndex}]")) yield return nested;
                    childIndex++;
                }
            }
            index++;
        }
    }


    // Read the reward Article path

    private static string ReadArticlePath(SoftObjectPropertyData property)
    {
        var softPath = (FSoftObjectPath)property.Value!;
        if (softPath.SubPathString is not null && !String.IsNullOrEmpty(softPath.SubPathString.ToString()))
            throw new InvalidDataException("Initial Inventory ArticlePath is not a package-level FSoftObjectPath.");
        var package = softPath.AssetPath.PackageName.ToString();
        var asset = softPath.AssetPath.AssetName.ToString();
        if (String.IsNullOrWhiteSpace(package) || String.IsNullOrWhiteSpace(asset))
            throw new InvalidDataException("Initial Inventory ArticlePath has an empty package or asset name.");
        if (!String.Equals(AssetName(package), asset, StringComparison.Ordinal))
            throw new InvalidDataException($"Initial Inventory FSoftObjectPath package and asset names disagree: package={package}, asset={asset}.");
        return package;
    }


    // Resolve the asset name

    private static string AssetName(string path) => path[(path.LastIndexOf('/') + 1)..];


    // Check the expected source reward

    private static void ValidateExpectedSource(InventoryScan scan)
    {
        if (scan.Slots.Count != 3 || scan.Preserved.Count != PreservedEntries.Length || scan.ArticlePathCount != 7)
            throw new InvalidDataException("Initial Inventory package does not contain the expected three randomized and four preserved ArticlePath entries.");
        foreach (var item in scan.Slots)
        {
            if (!Slots.TryGetValue(item.SlotId, out var spec) || item.ArticlePath != spec.VanillaArticlePath)
                throw new InvalidDataException("Initial Inventory source identities differ from the confirmed vanilla layout.");
        }
    }


    // Set soft path

    private static void SetSoftPath(UAsset asset, SoftObjectPropertyData property, string articlePath)
    {
        ValidateArticlePath(articlePath);
        var split = articlePath.LastIndexOf('/');
        var assetName = articlePath[(split + 1)..];
        var path = (FSoftObjectPath)property.Value!;
        path.AssetPath = new FTopLevelAssetPath
        {
            PackageName = new FName(asset, articlePath),
            AssetName = new FName(asset, assetName),
        };
        property.Value = path;
    }


    // Check Article path

    private static void ValidateArticlePath(string value)
    {
        if (!value.StartsWith("/Game/Blueprint/Article/", StringComparison.Ordinal) || value.Contains('\\') || value.EndsWith('/') ||
            value.Split('/').Skip(1).Any(part => String.IsNullOrEmpty(part) || part is "." or ".."))
            throw new InvalidDataException("Assigned Initial Inventory identity must be a canonical /Game/Blueprint/Article package path.");
        if (value.Contains('.') || value.Split('/').Last().Length == 0)
            throw new InvalidDataException("Assigned Initial Inventory ArticlePath must reference a package-level Article asset.");
    }


    // Make inspection

    private static InitialInventoryInspection MakeInspection(string virtualPackage, string file, InventoryScan scan) =>
        new(virtualPackage, file, scan.ArticlePathCount,
            scan.Slots.Select(item => new InitialInventorySlotInspection(item.SlotId, item.ArticlePath,
                item.ExportIndex, item.ExportName, item.PropertyPath)).ToArray(), scan.Preserved);


    // Capture package exports

    private static Dictionary<int, string> SnapshotExports(UAsset asset, HashSet<PropertyData> maskedPaths)
    {
        var result = new Dictionary<int, string>();
        for (var index = 0; index < asset.Exports.Count; index++)
        {
            if (asset.Exports[index] is NormalExport export)
                result[index] = JsonSerializer.Serialize(export.Data.Select(property => Canonicalize(property, maskedPaths)).ToArray());
            else if (asset.Exports[index] is RawExport raw)
                result[index] = JsonSerializer.Serialize(new { type = raw.GetType().FullName, name = raw.ObjectName.ToString(), raw.SerialSize, data = Convert.ToBase64String(raw.Data) });
            else
                throw new InvalidDataException($"Initial Inventory export {index + 1} cannot be snapshotted safely.");
        }
        return result;
    }


    // Normalize serialized data

    private static object? Canonicalize(object? value, HashSet<PropertyData> masks, HashSet<object>? visited = null, int depth = 0)
    {
        if (value is null) return null;
        if (depth > 48) throw new InvalidDataException("Initial Inventory property nesting exceeds its safety limit.");
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
                ["value"] = masks.Contains(property) ? "<assigned-ArticlePath>" : Canonicalize(raw, masks, visited, depth + 1),
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


    // Capture package imports

    private static string SnapshotImports(UAsset asset) => JsonSerializer.Serialize(asset.Imports.Select(item => new
    {
        ClassPackage = item.ClassPackage.ToString(), ClassName = item.ClassName.ToString(),
        ObjectName = item.ObjectName.ToString(), OuterIndex = item.OuterIndex.Index,
    }).ToArray());

    private static (string Name, string? Type)[] ExportIdentities(UAsset asset) => asset.Exports
        .Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();


    // Compare package snapshots

    private static void ValidateSnapshots(Dictionary<int, string> expected, Dictionary<int, string> actual)
    {
        if (!expected.Keys.ToHashSet().SetEquals(actual.Keys))
            throw new InvalidDataException("Initial Inventory export inventory changed after serialization.");
        foreach (var item in expected)
            if (!String.Equals(item.Value, actual[item.Key], StringComparison.Ordinal))
                throw new InvalidDataException($"Initial Inventory export {item.Key + 1} changed outside the requested ArticlePath fields.");
    }


    // Check package exports

    private static void ValidateExportInventory((string Name, string? Type)[] expected, IList<Export> actual)
    {
        var observed = actual.Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();
        if (!expected.SequenceEqual(observed))
            throw new InvalidDataException("Initial Inventory export identity, type, or order changed after serialization.");
    }


    // Read every package export

    private static void ParseAllExports(UAsset asset, string input)
    {
        using var reader = asset.PathToReader(input, true);
        for (var index = 0; index < asset.Exports.Count; index++)
            if (asset.Exports[index] is not NormalExport)
                asset.ParseExport(reader, index, true);
    }


    // Load the Unreal package

    private static UAsset LoadPackage(string input, EngineVersion version, Usmap mappings) =>
        new(input, true, version, mappings, CustomSerializationFlags.SkipParsingExports | CustomSerializationFlags.SkipPreloadDependencyLoading);


    // Copy additional sidecars

    private static void CopyAdditionalSidecars(string input, string temporaryHeader)
    {
        foreach (var extension in new[] { ".ubulk", ".uptnl" })
        {
            var source = Path.ChangeExtension(input, extension);
            var target = Path.ChangeExtension(temporaryHeader, extension);
            if (File.Exists(source) && !File.Exists(target)) File.Copy(source, target);
        }
    }


    // Check required package sidecars

    private static void ValidateRequiredSidecars(string input, string output)
    {
        foreach (var extension in new[] { ".uexp", ".ubulk", ".uptnl" })
            if (File.Exists(Path.ChangeExtension(input, extension)) != File.Exists(Path.ChangeExtension(output, extension)))
                throw new InvalidDataException($"Initial Inventory serialization did not preserve its {extension} sidecar inventory.");
    }


    // Collect package files

    private static string[] PackageFiles(string header) =>
        new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" }.Select(extension => Path.ChangeExtension(header, extension)).ToArray();


    // Resolve an output path

    private static string ResolveOwnedPath(string root, string value, bool mustExist)
    {
        var fullRoot = Path.GetFullPath(root);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(fullRoot, path);
        if (relative == "." || relative == ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new InvalidDataException("Initial Inventory package path escaped the supplied workspace.");
        if (mustExist && !File.Exists(path)) throw new FileNotFoundException("Initial Inventory package was not found.", path);
        return path;
    }


    // Describe slot spec

    private sealed record SlotSpec(string SlotId, string DisplayName, string PropertyPath, string VanillaArticlePath);
    // Describe preserved spec
    private sealed record PreservedSpec(string Identity, string PropertyPath, string VanillaArticlePath);
    // Describe slot hit
    private sealed record SlotHit(string SlotId, string ArticlePath, int ExportIndex, string ExportName,
        string PropertyPath, SoftObjectPropertyData Property);
    // Describe preserved hit
    private sealed record PreservedHit(string Identity, string ArticlePath, int ExportIndex, string ExportName, string PropertyPath);
    // Read inventory scan
    private sealed record InventoryScan(IReadOnlyList<SlotHit> Slots, IReadOnlyList<PreservedHit> Preserved,
        int ArticlePathCount);
    // Describe initial inventory request
    private sealed record InitialInventoryRequest(string VirtualPackage, string InputFile, string? OutputFile,
        IReadOnlyList<InitialInventoryAssignment>? Assignments, bool MotorcycleUnlocked);
    // Read initial inventory assignment
    private sealed record InitialInventoryAssignment(string SlotId, string ExpectedArticlePath, string AssignedArticlePath);
    // Describe initial inventory slot inspection
    private sealed record InitialInventorySlotInspection(string SlotId, string ArticlePath, int ExportIndex,
        string ExportName, string PropertyPath);
    // Describe initial inventory inspection
    private sealed record InitialInventoryInspection(string VirtualPackage, string InputFile, int ArticlePathCount,
        IReadOnlyList<InitialInventorySlotInspection> Slots, IReadOnlyList<PreservedHit> Preserved);
    // Describe initial inventory patch result
    private sealed record InitialInventoryPatchResult(string VirtualPackage, string InputFile, string OutputFile,
        int PatchedSlots, InitialInventoryInspection Inspection, IReadOnlyList<string> OutputFiles,
        bool MotorcycleUnlocked, string? MotorcycleArticlePath, bool ReopenedAndValidated);
}
