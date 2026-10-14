using System.Buffers.Binary;
using System.Collections;
using System.Security.Cryptography;
using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class RawArticleGrantPatcher
{
    private const int ExpectedSerialSize = 55;
    private const int PathNameIndexOffset = 0x16;
    private const int PathNumberOffset = 0x1A;
    private const int ObjectNameIndexOffset = 0x1E;
    private const int ObjectNumberOffset = 0x22;
    private const int ExpectedPathNameIndex = 0;
    private const int ExpectedObjectNameIndex = 27;
    private const int ExpectedFNameNumber = 2;
    private static readonly IReadOnlyDictionary<string, RawArticleGrantSlot> KnownSlots =
        new Dictionary<string, RawArticleGrantSlot>(StringComparer.Ordinal)
        {
            ["jadwiga_servant_lightning_barrage_reward"] = new(
                "jadwiga_servant_lightning_barrage_reward",
                "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Common/Test/JadwigaServant/BP_SmallScenario_JadwigaServant1",
                "Default__BP_SmallScenario_JadwigaServant1_C", 2, ExpectedSerialSize,
                PathNameIndexOffset, PathNumberOffset, ObjectNameIndexOffset, ObjectNumberOffset,
                ExpectedPathNameIndex, ExpectedObjectNameIndex,
                "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicLongRange/DA_GM_CV02_LightningBarrage",
                "DA_GM_CV02_LightningBarrage",
                "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicLongRange/DA_GM_CV02_LightningBarrage", 0,
                "97ad9e4403de3933993175e42381a8dfea5a6cc646df865105227ee5d3392f3d",
                "999507cbe374382cbcf7e585e565bb53416d55822c677634a0484afe9d2076a1"),
            ["jadwiga_servant_booster_dex_reward"] = new(
                "jadwiga_servant_booster_dex_reward",
                "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Common/Test/JadwigaServant/BP_SmallScenario_JadwigaServant2",
                "Default__BP_SmallScenario_JadwigaServant2_C", 2, ExpectedSerialSize,
                PathNameIndexOffset, PathNumberOffset, ObjectNameIndexOffset, ObjectNumberOffset,
                ExpectedPathNameIndex, ExpectedObjectNameIndex,
                "/Game/Blueprint/Article/Booster/Basic/DA_Booster_Dex", "DA_Booster_Dex",
                "/Game/Blueprint/Article/Booster/Basic/DA_Booster_Dex_1", ExpectedFNameNumber,
                "88afd1283169d1e3620a169e191112fc803e13aca499602cc023158efde32fcd",
                "124c3baaf309e6b6568eeef7ef4050ead4a5ab2f681436674c9f36437877517d"),
            ["jadwiga_servant_initial_parry_reward"] = new(
                "jadwiga_servant_initial_parry_reward",
                "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Common/Test/JadwigaServant/BP_SmallScenario_JadwigaServant3",
                "Default__BP_SmallScenario_JadwigaServant3_C", 2, ExpectedSerialSize,
                PathNameIndexOffset, PathNumberOffset, ObjectNameIndexOffset, ObjectNumberOffset,
                ExpectedPathNameIndex, ExpectedObjectNameIndex,
                "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Parry", "DA_DefGadget_Parry",
                "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Parry_1", ExpectedFNameNumber,
                "fc6fc87534a026926a3281750ccce92d7f3cd36c48893946c68d271f2e1d1c9b",
                "1a34124c4b48274b13178a656958717ae8734cf70955900c0c23872c7c45a43b"),
        };

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };


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
            if (!Directory.Exists(workRoot)) throw new InvalidDataException("Generation workspace does not exist.");
            if (!File.Exists(mappingsPath)) throw new FileNotFoundException("CV2.usmap mapping was not found.", mappingsPath);
            var request = JsonSerializer.Deserialize<RawArticleGrantRequest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Raw Article grant request is empty.");
            ValidateSlot(request.VirtualPackage, request.Slot);
            var input = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
            var mappings = new Usmap(mappingsPath);
            RawArticleInspection inspection;
            if (patch)
            {
                var output = ResolveOwnedPath(workRoot, request.OutputFile
                    ?? throw new InvalidDataException("Raw Article patch request has no output file."), mustExist: false);
                var desired = ValidateArticlePath(request.AssignedArticlePath
                    ?? throw new InvalidDataException("Raw Article patch request has no assigned Article path."));
                if (File.Exists(output) || PackageFiles(output).Any(File.Exists))
                    throw new IOException("Refusing to overwrite an existing Raw Article grant output.");
                inspection = PatchOne(input, output, request, desired, engineVersion, mappings);
            }
            else
            {
                inspection = InspectOne(input, request, engineVersion, mappings);
            }
            Console.WriteLine(JsonSerializer.Serialize(inspection, JsonOptions));
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"{exception.GetType().Name}: {exception.Message}");
            return 1;
        }
    }


    // Run patch fixture checks

    public static int TestFixtures()
    {
        try
        {
            var fixtures = new[]
            {
                "/Game/Blueprint/Article/Gadget/StandAlone/DA_Bow",
                "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Warp_2",
                "/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight_1",
            };
            var results = KnownSlots.Values
                .SelectMany(slot => fixtures.Select(path => TestFixture(slot, path)))
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


    // Test one patch fixture

    private static FixtureResult TestFixture(RawArticleGrantSlot slot, string requestedPath)
    {
        var original = Enumerable.Range(0, ExpectedSerialSize).Select(value => (byte)(value * 17 + 3)).ToArray();
        BinaryPrimitives.WriteInt32LittleEndian(original.AsSpan(slot.PathNameIndexOffset, 4), slot.ExpectedPathNameIndex);
        BinaryPrimitives.WriteInt32LittleEndian(original.AsSpan(PathNumberOffset, 4), slot.ExpectedVanillaNumber);
        BinaryPrimitives.WriteInt32LittleEndian(original.AsSpan(slot.ObjectNameIndexOffset, 4), slot.ExpectedObjectNameIndex);
        BinaryPrimitives.WriteInt32LittleEndian(original.AsSpan(ObjectNumberOffset, 4), slot.ExpectedVanillaNumber);
        var patched = original.ToArray();
        var nameMap = new Dictionary<int, string>
        {
            [slot.ExpectedPathNameIndex] = slot.ExpectedPathNameMapValue,
            [slot.ExpectedObjectNameIndex] = slot.ExpectedObjectNameMapValue,
        };
        PatchLiteralIdentity(patched, slot.ExpectedPathNameIndex, slot.ExpectedObjectNameIndex, requestedPath,
            (index, name) => nameMap[index] = name);
        ValidatePatchedBytes(original, patched, requestedPath, nameMap, slot);
        return new FixtureResult(slot.DestinationId, slot.ExpectedVanillaNumber, requestedPath,
            nameMap[slot.ExpectedPathNameIndex], nameMap[slot.ExpectedObjectNameIndex],
            BinaryPrimitives.ReadInt32LittleEndian(patched.AsSpan(PathNumberOffset, 4)),
            BinaryPrimitives.ReadInt32LittleEndian(patched.AsSpan(ObjectNumberOffset, 4)), true);
    }


    // Inspect one asset package

    private static RawArticleInspection InspectOne(
        string input,
        RawArticleGrantRequest request,
        EngineVersion engineVersion,
        Usmap mappings)
    {
        ValidateSourceFingerprint(input, request.Slot);
        var asset = LoadPackage(input, engineVersion, mappings);
        var target = FindTarget(asset, request.Slot);
        ValidateVanillaTarget(asset, target.Export, request.Slot);
        return MakeInspection(request.VirtualPackage, input, target.Index + 1, target.Export, asset,
            ReadIdentity((RawExport)target.Export, asset, request.Slot));
    }


    // Patch one asset package

    private static RawArticleInspection PatchOne(
        string input,
        string output,
        RawArticleGrantRequest request,
        string desiredArticlePath,
        EngineVersion engineVersion,
        Usmap mappings)
    {
        ValidateSourceFingerprint(input, request.Slot);
        var asset = LoadPackage(input, engineVersion, mappings);
        ParseNonRawExports(asset, input);
        var target = FindTarget(asset, request.Slot);
        ValidateVanillaTarget(asset, target.Export, request.Slot);
        var originalRaw = ((RawExport)target.Export).Data.ToArray();
        var originalNumberOffsets = new HashSet<int> { request.Slot.PathNumberOffset, request.Slot.ObjectNumberOffset };
        var beforeSnapshots = SnapshotOtherExports(asset, target.Index);
        var beforeImports = SnapshotImports(asset);
        var identities = ExportIdentities(asset);
        var pathNameIndex = ReadInt32(originalRaw, request.Slot.PathNameIndexOffset);
        var objectNameIndex = ReadInt32(originalRaw, request.Slot.ObjectNameIndexOffset);
        PatchLiteralIdentity(((RawExport)target.Export).Data, pathNameIndex, objectNameIndex, desiredArticlePath,
            (index, value) => asset.SetNameReference(index, new FString(value)));

        var directory = Path.GetDirectoryName(output)!;
        Directory.CreateDirectory(directory);
        var temporaryHeader = Path.Combine(directory,
            Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(output));
        var moved = new List<string>();
        try
        {
            asset.Write(temporaryHeader);
            CopyAdditionalSidecars(input, temporaryHeader);
            ValidateHeaderAndSidecar(input, temporaryHeader);
            ValidateReopened(temporaryHeader, request, desiredArticlePath, engineVersion, mappings, identities,
                beforeSnapshots, beforeImports, originalRaw, originalNumberOffsets);

            var tempFiles = PackageFiles(temporaryHeader).Where(File.Exists).ToArray();
            var outputFiles = tempFiles.Select(path => Path.ChangeExtension(output, Path.GetExtension(path))).ToArray();
            if (outputFiles.Any(File.Exists)) throw new IOException("Refusing to overwrite a Raw Article output sidecar.");
            for (var index = 0; index < tempFiles.Length; index++)
            {
                File.Move(tempFiles[index], outputFiles[index]);
                moved.Add(outputFiles[index]);
            }
            // ValidateReopened already parsed and checked the exact bytes written
            // to temporaryHeader. File.Move is within the same output directory,
            // so reparsing the moved package would repeat identical work.
            return MakeInspection(request.VirtualPackage, output, target.Index + 1, target.Export, asset,
                ReadIdentity((RawExport)target.Export, asset, request.Slot),
                outputFiles.Select(Path.GetFullPath).ToArray(), true);
        }
        catch
        {
            foreach (var path in moved)
                try { File.Delete(path); } catch (IOException) { }
            throw;
        }
        finally
        {
            foreach (var path in PackageFiles(temporaryHeader))
                try { File.Delete(path); } catch (IOException) { }
        }
    }


    // Check reopened

    private static void ValidateReopened(
        string packageHeader,
        RawArticleGrantRequest request,
        string desiredArticlePath,
        EngineVersion engineVersion,
        Usmap mappings,
        (string Name, string? Type)[] identities,
        Dictionary<int, string> beforeSnapshots,
        string beforeImports,
        byte[] originalRaw,
        HashSet<int> allowedChangedOffsets)
    {
        var reopened = LoadPackage(packageHeader, engineVersion, mappings);
        ParseNonRawExports(reopened, packageHeader);
        if (!identities.SequenceEqual(ExportIdentities(reopened)))
            throw new InvalidDataException("direct reward package export identity, type, or order changed after serialization.");
        if (!String.Equals(beforeImports, SnapshotImports(reopened), StringComparison.Ordinal))
            throw new InvalidDataException("direct reward package imports changed when its reward NameMap values were patched.");
        ValidateSnapshots(beforeSnapshots, SnapshotOtherExports(reopened,
            FindTarget(reopened, request.Slot).Index));
        var target = FindTarget(reopened, request.Slot);
        if (target.Export is not RawExport raw || raw.SerialSize != request.Slot.SerialSize || raw.Data.Length != request.Slot.SerialSize)
            throw new InvalidDataException("Reopened direct reward reward CDO is no longer the expected 55-byte RawExport.");
        var actual = ReadIdentity(raw, reopened, request.Slot);
        var expectedObjectName = desiredArticlePath[(desiredArticlePath.LastIndexOf('/') + 1)..];
        if (actual.ArticlePath != desiredArticlePath || actual.ObjectNameMapValue != expectedObjectName ||
            actual.PathNumber != 0 || actual.ObjectNumber != 0)
            throw new InvalidDataException("Reopened direct reward reward does not match the requested literal Article identity.");
        for (var offset = 0; offset < originalRaw.Length; offset++)
            if (!allowedChangedOffsets.Contains(offset) && raw.Data[offset] != originalRaw[offset])
                throw new InvalidDataException($"direct reward RawExport byte 0x{offset:X} changed outside the two FName Number fields.");
    }


    // Check the original quest reward

    private static void ValidateVanillaTarget(UAsset asset, Export export, RawArticleGrantSlot slot)
    {
        if (export is not RawExport raw || raw.SerialSize != slot.SerialSize || raw.Data.Length != slot.SerialSize)
            throw new InvalidDataException("direct reward CDO does not match the confirmed RawExport/SerialSize layout.");
        var identity = ReadIdentity(raw, asset, slot);
        if (identity.PathNameIndex != slot.ExpectedPathNameIndex || identity.ObjectNameIndex != slot.ExpectedObjectNameIndex ||
            identity.PathNameMapValue != slot.ExpectedPathNameMapValue || identity.ObjectNameMapValue != slot.ExpectedObjectNameMapValue ||
            identity.PathNumber != slot.ExpectedVanillaNumber || identity.ObjectNumber != slot.ExpectedVanillaNumber ||
            identity.ArticlePath != slot.ExpectedVanillaArticlePath)
            throw new InvalidDataException("direct reward source reward identity or NameMap references differ from the confirmed vanilla layout.");
    }

    private static (int Index, Export Export) FindTarget(UAsset asset, RawArticleGrantSlot slot)
    {
        var matches = asset.Exports.Select((export, index) => (export, index))
            .Where(item => String.Equals(item.export.ObjectName.ToString(), slot.ExportName, StringComparison.Ordinal))
            .ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"Expected exactly one {slot.ExportName} CDO, found {matches.Length}.");
        if (matches[0].index + 1 != slot.ExportIndex)
            throw new InvalidDataException($"direct reward CDO export index differs from metadata (actual {matches[0].index + 1}, expected {slot.ExportIndex}).");
        return (matches[0].index, matches[0].export);
    }


    // Read the source identity

    private static RawArticleIdentity ReadIdentity(RawExport export, UAsset asset, RawArticleGrantSlot slot)
    {
        var data = export.Data;
        var pathIndex = ReadInt32(data, slot.PathNameIndexOffset);
        var pathNumber = ReadInt32(data, slot.PathNumberOffset);
        var objectIndex = ReadInt32(data, slot.ObjectNameIndexOffset);
        var objectNumber = ReadInt32(data, slot.ObjectNumberOffset);
        if (pathIndex < 0 || pathIndex >= asset.GetNameMapIndexList().Count ||
            objectIndex < 0 || objectIndex >= asset.GetNameMapIndexList().Count)
            throw new InvalidDataException("direct reward CDO reward NameMap reference is outside the package NameMap.");
        var pathName = asset.GetNameReference(pathIndex).ToString();
        var objectName = asset.GetNameReference(objectIndex).ToString();
        var articlePath = pathName + (pathNumber > 0 ? "_" + (pathNumber - 1) : string.Empty);
        return new RawArticleIdentity(pathIndex, pathNumber, objectIndex, objectNumber, pathName, objectName, articlePath);
    }


    // Patch the exact identity

    private static void PatchLiteralIdentity(
        byte[] rawData,
        int pathNameIndex,
        int objectNameIndex,
        string articlePath,
        Action<int, string> setNameReference)
    {
        var desired = ValidateArticlePath(articlePath);
        var objectName = desired[(desired.LastIndexOf('/') + 1)..];
        if (pathNameIndex < 0 || objectNameIndex < 0 || setNameReference is null)
            throw new InvalidDataException("direct reward reward NameMap references are invalid.");
        setNameReference(pathNameIndex, desired);
        setNameReference(objectNameIndex, objectName);
        BinaryPrimitives.WriteInt32LittleEndian(rawData.AsSpan(PathNumberOffset, 4), 0);
        BinaryPrimitives.WriteInt32LittleEndian(rawData.AsSpan(ObjectNumberOffset, 4), 0);
    }


    // Check patched package bytes

    private static void ValidatePatchedBytes(
        byte[] original,
        byte[] patched,
        string desiredPath,
        IReadOnlyDictionary<int, string> nameMap,
        RawArticleGrantSlot slot)
    {
        var expectedObject = desiredPath[(desiredPath.LastIndexOf('/') + 1)..];
        if (nameMap[slot.ExpectedPathNameIndex] != desiredPath || nameMap[slot.ExpectedObjectNameIndex] != expectedObject ||
            ReadInt32(patched, slot.PathNameIndexOffset) != slot.ExpectedPathNameIndex ||
            ReadInt32(patched, slot.ObjectNameIndexOffset) != slot.ExpectedObjectNameIndex ||
            ReadInt32(patched, slot.PathNumberOffset) != 0 || ReadInt32(patched, slot.ObjectNumberOffset) != 0)
            throw new InvalidDataException("Literal Article fixture did not preserve complete names with FName Number zero.");
        for (var index = 0; index < original.Length; index++)
            if (index != slot.PathNumberOffset && index != slot.ObjectNumberOffset && original[index] != patched[index])
                throw new InvalidDataException($"Literal Article fixture changed unrelated raw byte 0x{index:X}.");
    }


    // Capture unrelated exports

    private static Dictionary<int, string> SnapshotOtherExports(UAsset asset, int targetIndex)
    {
        var result = new Dictionary<int, string>();
        for (var index = 0; index < asset.Exports.Count; index++)
        {
            if (index == targetIndex) continue;
            var export = asset.Exports[index];
            result[index] = export switch
            {
                NormalExport normal => JsonSerializer.Serialize(normal.Data.Select(item => Canonicalize(item)).ToArray()),
                RawExport raw => Convert.ToHexString(raw.Data),
                _ => export.GetType().FullName + "|" + export.ObjectName,
            };
        }
        return result;
    }


    // Normalize serialized data

    private static object? Canonicalize(object? value, HashSet<object>? visited = null, int depth = 0)
    {
        if (value is null) return null;
        if (depth > 48) throw new InvalidDataException("direct reward export nesting exceeds its safety limit.");
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
                ["value"] = Canonicalize(raw, visited, depth + 1),
            };
        }
        if (value is IEnumerable enumerable)
        {
            visited ??= new HashSet<object>(ReferenceEqualityComparer.Instance);
            if (!value.GetType().IsValueType && !visited.Add(value)) return "<cycle>";
            try { return enumerable.Cast<object?>().Select(item => Canonicalize(item, visited, depth + 1)).ToArray(); }
            finally { if (!value.GetType().IsValueType) visited.Remove(value); }
        }
        return value.ToString() ?? value.GetType().FullName;
    }


    // Capture package imports

    private static string SnapshotImports(UAsset asset) => JsonSerializer.Serialize(asset.Imports.Select(item => new
    {
        ClassPackage = item.ClassPackage.ToString(),
        ClassName = item.ClassName.ToString(),
        ObjectName = item.ObjectName.ToString(),
        OuterIndex = item.OuterIndex.Index,
    }).ToArray());

    private static (string Name, string? Type)[] ExportIdentities(UAsset asset) => asset.Exports
        .Select(export => (Name: export.ObjectName.ToString(), Type: export.GetType().FullName)).ToArray();


    // Compare package snapshots

    private static void ValidateSnapshots(Dictionary<int, string> expected, Dictionary<int, string> actual)
    {
        if (!expected.Keys.ToHashSet().SetEquals(actual.Keys))
            throw new InvalidDataException("direct reward package export inventory changed after serialization.");
        foreach (var item in expected)
            if (!String.Equals(item.Value, actual[item.Key], StringComparison.Ordinal))
                throw new InvalidDataException($"direct reward package export {item.Key + 1} changed outside the reward CDO.");
    }


    // Make inspection

    private static RawArticleInspection MakeInspection(
        string virtualPackage,
        string file,
        int exportIndex,
        Export export,
        UAsset asset,
        RawArticleIdentity identity,
        string[]? outputFiles = null,
        bool reopenedAndValidated = false) => new(
            virtualPackage, file, exportIndex, export.ObjectName.ToString(), export.SerialSize,
            identity.PathNameIndex, identity.PathNumber, identity.ObjectNameIndex, identity.ObjectNumber,
            identity.PathNameMapValue, identity.ObjectNameMapValue, identity.ArticlePath,
            outputFiles, reopenedAndValidated);


    // Load the Unreal package

    private static UAsset LoadPackage(string file, EngineVersion version, Usmap mappings) =>
        new(file, true, version, mappings,
            CustomSerializationFlags.SkipParsingExports | CustomSerializationFlags.SkipPreloadDependencyLoading);


    // Parse structured package exports

    private static void ParseNonRawExports(UAsset asset, string file)
    {
        using var reader = asset.PathToReader(file, true);
        for (var index = 0; index < asset.Exports.Count; index++)
        {
            if (asset.Exports[index] is NormalExport) continue;
            try { asset.ParseExport(reader, index, true); }
            catch when (asset.Exports[index] is RawExport) { }
        }
    }


    // Check the reward slot

    private static void ValidateSlot(string virtualPackage, RawArticleGrantSlot slot)
    {
        if (!KnownSlots.TryGetValue(slot.DestinationId, out var expected) || slot != expected ||
            !String.Equals(virtualPackage, expected.PackagePath, StringComparison.Ordinal))
            throw new InvalidDataException("Raw Article grant metadata does not match an explicitly researched, fingerprint-bound slot.");
    }


    // Check the source fingerprint

    private static void ValidateSourceFingerprint(string input, RawArticleGrantSlot slot)
    {
        var sidecar = Path.ChangeExtension(input, ".uexp");
        if (!File.Exists(sidecar))
            throw new InvalidDataException("Fingerprint-bound direct reward source is missing its .uexp sidecar.");
        var headerHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(input))).ToLowerInvariant();
        var sidecarHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(sidecar))).ToLowerInvariant();
        if (!String.Equals(headerHash, slot.SourceUassetSha256, StringComparison.Ordinal) ||
            !String.Equals(sidecarHash, slot.SourceUexpSha256, StringComparison.Ordinal))
            throw new InvalidDataException($"Direct reward source fingerprint differs for {slot.DestinationId}.");
    }


    // Check Article path

    private static string ValidateArticlePath(string path)
    {
        if (String.IsNullOrWhiteSpace(path) || !path.StartsWith("/Game/Blueprint/Article/", StringComparison.Ordinal) ||
            path.Contains('\\') || path.EndsWith('/') || path.Contains('.') ||
            path.Split('/').Skip(1).Any(part => String.IsNullOrWhiteSpace(part) || part is "." or ".."))
            throw new InvalidDataException("Assigned reward must be a complete canonical /Game ArticleDataPath.");
        var objectName = path[(path.LastIndexOf('/') + 1)..];
        if (objectName.Length == 0 || objectName.Any(char.IsWhiteSpace))
            throw new InvalidDataException("Assigned Article object name is invalid.");
        return path;
    }


    // Read int 32

    private static int ReadInt32(byte[] data, int offset)
    {
        if (offset < 0 || offset > data.Length - sizeof(int))
            throw new InvalidDataException("direct reward raw reward field offset is outside the export data.");
        return BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset, sizeof(int)));
    }


    // Resolve an output path

    private static string ResolveOwnedPath(string workRoot, string value, bool mustExist)
    {
        var root = Path.GetFullPath(workRoot);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(root, path);
        if (relative is "." or ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new InvalidDataException("Raw Article package path escaped the generation workspace.");
        if (mustExist && !File.Exists(path)) throw new FileNotFoundException("Raw Article source package was not found.", path);
        return path;
    }


    // Collect package files

    private static string[] PackageFiles(string header) =>
        new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" }.Select(extension => Path.ChangeExtension(header, extension)).ToArray();


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
        if (!File.Exists(outputHeader)) throw new InvalidDataException("UAssetAPI omitted the direct reward .uasset header.");
        if (File.Exists(Path.ChangeExtension(inputHeader, ".uexp")) && !File.Exists(Path.ChangeExtension(outputHeader, ".uexp")))
            throw new InvalidDataException("UAssetAPI omitted the direct reward .uexp sidecar.");
    }


    // Describe raw reward request data

    private sealed record RawArticleGrantRequest(
        string VirtualPackage,
        string InputFile,
        string? OutputFile,
        string? AssignedArticlePath,
        RawArticleGrantSlot Slot);


    // Describe a raw reward slot

    private sealed record RawArticleGrantSlot(
        string DestinationId,
        string PackagePath,
        string ExportName,
        int ExportIndex,
        int SerialSize,
        int PathNameIndexOffset,
        int PathNumberOffset,
        int ObjectNameIndexOffset,
        int ObjectNumberOffset,
        int ExpectedPathNameIndex,
        int ExpectedObjectNameIndex,
        string ExpectedPathNameMapValue,
        string ExpectedObjectNameMapValue,
        string ExpectedVanillaArticlePath,
        int ExpectedVanillaNumber,
        string SourceUassetSha256,
        string SourceUexpSha256);


    // Describe Article identity data

    private sealed record RawArticleIdentity(
        int PathNameIndex,
        int PathNumber,
        int ObjectNameIndex,
        int ObjectNumber,
        string PathNameMapValue,
        string ObjectNameMapValue,
        string ArticlePath);


    // Describe inspected reward data

    private sealed record RawArticleInspection(
        string VirtualPackage,
        string InputFile,
        int ExportIndex,
        string ExportName,
        long SerialSize,
        int PathNameIndex,
        int PathNumber,
        int ObjectNameIndex,
        int ObjectNumber,
        string PathNameMapValue,
        string ObjectNameMapValue,
        string ArticlePath,
        string[]? OutputFiles = null,
        bool ReopenedAndValidated = false);


    // Describe fixture test results

    private sealed record FixtureResult(
        string DestinationId,
        int VanillaNumber,
        string RequestedPath,
        string NameMapPath,
        string NameMapObject,
        int PathNumber,
        int ObjectNumber,
        bool UnrelatedRawBytesPreserved);
}
