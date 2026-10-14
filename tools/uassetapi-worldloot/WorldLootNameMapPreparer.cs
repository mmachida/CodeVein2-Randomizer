using System.Buffers.Binary;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.PropertyTypes.Objects;
using UAssetAPI.PropertyTypes.Structs;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class WorldLootNameMapPreparer
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };


    // Prepare NameMap data

    public static int Prepare(string[] args)
    {
        var workRoot = Path.GetFullPath(args[1]);
        try
        {
            // The shared Python helper invocation contract is:
            // operation, work root, manifest, mappings, engine version.
            var manifest = JsonSerializer.Deserialize<WorldLootManifest>(File.ReadAllText(args[2]), JsonOptions)
                ?? throw new InvalidDataException("World Loot NameMap preparation manifest is empty.");
            var mappings = new Usmap(Path.GetFullPath(args[3]));
            var engineVersion = Enum.Parse<EngineVersion>(args[4], ignoreCase: false);
            if (manifest.Packages.Count == 0)
                throw new InvalidDataException("World Loot NameMap preparation manifest has no packages.");
            var results = new List<object>();
            foreach (var request in manifest.Packages)
            {
                try { results.Add(PrepareOne(workRoot, request, mappings, engineVersion)); }
                catch (Exception exception)
                {
                    // Keep class discovery available when preparation itself fails after the
                    // package request was otherwise valid. Only failed preparations pay for
                    // this recovery open; successful maps share one UAsset lifetime.
                    List<MapClassInspection> classes;
                    try { classes = InspectClasses(workRoot, request, engineVersion, mappings); }
                    catch { classes = new List<MapClassInspection>(); }
                    results.Add(new
                    {
                        PackagePath = request.PackagePath,
                        InputFile = request.InputFile,
                        OutputFile = request.OutputFile,
                        Status = "structural_fallback",
                        FallbackReason = exception.ToString(),
                        ReopenedAndValidated = false,
                        RawUexpByteIdentical = false,
                        Classes = classes,
                        DiscoveryOpenCount = classes.Count > 0 ? 2 : 1,
                        PreparationReloadCount = 0,
                        SerializationReopenCount = 0,
                        ValidationReopenCount = 0,
                        OutputFiles = Array.Empty<string>(),
                    });
                }
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


    // Prepare one

    private static WorldLootPackageResult PrepareOne(
        string workRoot, WorldLootPackageRequest request, Usmap mappings, EngineVersion engineVersion)
    {
        var input = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
        var output = ResolveOwnedPath(workRoot, request.OutputFile, mustExist: false);
        if (String.Equals(input, output, StringComparison.OrdinalIgnoreCase) || File.Exists(output))
            throw new IOException("Prepared NameMap output must be a new path separate from its source.");
        if (!request.PackagePath.StartsWith("/Game/Map/", StringComparison.Ordinal) ||
            Path.GetExtension(input) != ".umap" || Path.GetExtension(output) != ".umap")
            throw new InvalidDataException("WorldLoot inputs must be World Partition map packages with .umap headers.");
        if (request.NameValues.Count == 0 || request.NameValues.Any(String.IsNullOrWhiteSpace) ||
            request.Rewards.Count == 0)
            throw new InvalidDataException("WorldLoot package must include destination names and vanilla reward descriptors.");
        var sourceUexp = Path.ChangeExtension(input, ".uexp");
        if (!File.Exists(sourceUexp)) throw new FileNotFoundException("WorldLoot source .uexp sidecar is missing.", sourceUexp);

        var requestedNames = request.NameValues.Distinct(StringComparer.Ordinal)
            .Order(StringComparer.Ordinal).ToArray();
        var totalTimer = Stopwatch.StartNew();
        var sourceHeaderSize = new FileInfo(input).Length;
        var sourceUexpHash = HashFile(sourceUexp);
        var sourceUexpSize = new FileInfo(sourceUexp).Length;
        var loadTimer = Stopwatch.StartNew();
        var sourceAsset = new UAsset(input, engineVersion, mappings);
        loadTimer.Stop();
        var analysisTimer = Stopwatch.StartNew();
        var classes = InspectClasses(sourceAsset, request);
        var sourceNameMap = SnapshotNameMap(sourceAsset);
        var sourceImports = SnapshotImports(sourceAsset);
        var sourceExports = SnapshotExports(sourceAsset);
        var sourceExportTable = sourceAsset.Exports
            .Select(export => new ExportRecord(export.SerialSize, export.SerialOffset)).ToArray();
        var rawExportSizeRestores = new List<string>();
        var sourceRewards = ValidateVanillaRewards(sourceAsset, request.Rewards, sourceUexp, sourceHeaderSize);
        var fields = DescribeRewardFields(request, sourceExportTable, sourceUexp, sourceHeaderSize, sourceNameMap);
        var existingIndices = FindNameIndices(sourceNameMap, requestedNames, rejectAmbiguous: true);
        analysisTimer.Stop();
        var nameMapTimer = Stopwatch.StartNew();
        var expectedAddedNames = requestedNames.Where(name => !existingIndices.ContainsKey(name)).ToArray();
        if (expectedAddedNames.Length == 0)
        {
            nameMapTimer.Stop();
            totalTimer.Stop();
            using var process = Process.GetCurrentProcess();
            process.Refresh();
            return new WorldLootPackageResult(
                request.PackagePath, input, input, "raw_binary_candidate", null,
                sourceNameMap.Length, sourceNameMap.Length, Array.Empty<string>(),
                sourceHeaderSize, sourceHeaderSize, sourceUexpSize, sourceUexpSize,
                HashFile(input), sourceUexpHash, totalTimer.Elapsed.TotalMilliseconds,
                process.WorkingSet64, existingIndices, fields, new List<string>(),
                Array.Empty<string>(), true, true, classes,
                loadTimer.Elapsed.TotalMilliseconds, analysisTimer.Elapsed.TotalMilliseconds,
                nameMapTimer.Elapsed.TotalMilliseconds, 0, 0, 0, 1, 0, 0, 0);
        }
        foreach (var name in expectedAddedNames)
            _ = new FName(sourceAsset, name);
        nameMapTimer.Stop();

        var outputDirectory = Path.GetDirectoryName(output)!;
        Directory.CreateDirectory(outputDirectory);
        var temporaryHeader = Path.Combine(outputDirectory,
            Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + ".umap");
        var outputs = new List<string>();
        var serializationTimer = Stopwatch.StartNew();
        try
        {
            sourceAsset.Write(temporaryHeader);
            if (!File.Exists(temporaryHeader))
                throw new InvalidDataException("UAssetAPI omitted the prepared map header.");
            var serializedHeader = File.ReadAllBytes(temporaryHeader);
            var serializedValidationTimer = Stopwatch.StartNew();
            var serializedAsset = new UAsset(temporaryHeader, engineVersion, mappings);
            if (!sourceExports.SequenceEqual(SnapshotExports(serializedAsset)))
                throw new InvalidDataException("NameMap preparation changed export names, classes, or order.");
            if (!String.Equals(sourceImports, SnapshotImports(serializedAsset), StringComparison.Ordinal))
                throw new InvalidDataException("NameMap preparation changed package imports.");
            var serializedNames = SnapshotNameMap(serializedAsset);
            if (serializedNames.Length < sourceNameMap.Length ||
                !sourceNameMap.SequenceEqual(serializedNames.Take(sourceNameMap.Length)))
                throw new InvalidDataException("NameMap preparation changed existing NameMap entries or indices.");
            var appended = serializedNames.Skip(sourceNameMap.Length).ToArray();
            if (!expectedAddedNames.SequenceEqual(appended, StringComparer.Ordinal))
                throw new InvalidDataException("NameMap preparation appended unexpected or nondeterministically ordered names.");
            if (sourceExportTable.Length != serializedAsset.Exports.Count)
                throw new InvalidDataException("NameMap preparation changed the export count.");
            for (var index = 0; index < sourceExportTable.Length; index++)
            {
                if (sourceExportTable[index].SerialSize != serializedAsset.Exports[index].SerialSize)
                {
                    var exportName = serializedAsset.Exports[index].ObjectName.ToString();
                    var sourceSize = sourceExportTable[index].SerialSize;
                    var serializedSize = serializedAsset.Exports[index].SerialSize;
                    var sizeDelta = sourceSize - serializedSize;
                    if (sizeDelta != 2 || exportName is not ("MapFeature" or "MapFeatureComponent"))
                        throw new InvalidDataException(
                            $"NameMap preparation changed export #{index + 1} ({exportName}) " +
                            $"serialized size from {sourceSize} to {serializedSize}; " +
                            $"source offset {sourceExportTable[index].SerialOffset}, serialized offset {serializedAsset.Exports[index].SerialOffset}.");
                    rawExportSizeRestores.Add(
                        $"#{index + 1} {exportName}: restored raw size {sourceSize} (UAssetAPI emitted {serializedSize})");
                }
            }
            serializedValidationTimer.Stop();

            var temporaryUexp = Path.ChangeExtension(temporaryHeader, ".uexp");
            if (!File.Exists(temporaryUexp))
                throw new InvalidDataException("UAssetAPI omitted the prepared map .uexp sidecar.");
            var headerDelta = new FileInfo(temporaryHeader).Length - sourceHeaderSize;
            var rowPatches = new List<(int Offset, byte[] Bytes)>();
            var claimedRanges = new HashSet<int>();
            for (var index = 0; index < sourceExportTable.Length; index++)
            {
                var sourceRecord = sourceExportTable[index];
                if (sourceRecord.SerialOffset < sourceHeaderSize || sourceRecord.SerialSize < 0)
                    throw new InvalidDataException($"Source export #{index + 1} does not use a supported external payload layout.");
                var serializedRecord = serializedAsset.Exports[index];
                var expectedOffset = checked(sourceRecord.SerialOffset + headerDelta);
                if (serializedRecord.SerialSize == sourceRecord.SerialSize && serializedRecord.SerialOffset == expectedOffset)
                    continue;
                var needle = EncodeExportTablePair(serializedRecord.SerialSize, serializedRecord.SerialOffset);
                var rowOffset = FindUniqueByteSequence(serializedHeader, needle, $"ExportMap row #{index + 1}");
                if (!claimedRanges.Add(rowOffset))
                    throw new InvalidDataException("Multiple ExportMap rows resolved to the same header bytes.");
                rowPatches.Add((rowOffset, EncodeExportTablePair(sourceRecord.SerialSize, expectedOffset)));
            }
            foreach (var (offset, bytes) in rowPatches)
                bytes.CopyTo(serializedHeader.AsSpan(offset, bytes.Length));
            File.WriteAllBytes(temporaryHeader, serializedHeader);

            File.Copy(sourceUexp, temporaryUexp, overwrite: true);
            foreach (var extension in new[] { ".ubulk", ".uptnl" })
            {
                var source = Path.ChangeExtension(input, extension);
                var temporary = Path.ChangeExtension(temporaryHeader, extension);
                if (File.Exists(source)) File.Copy(source, temporary, overwrite: true);
                else if (File.Exists(temporary)) File.Delete(temporary);
            }

            foreach (var (temporary, final) in PackageFilePairs(temporaryHeader, output))
            {
                if (!File.Exists(temporary)) continue;
                if (File.Exists(final)) throw new IOException("Refusing to overwrite a prepared World Loot output sidecar.");
                File.Move(temporary, final);
                outputs.Add(final);
            }

            var validationTimer = Stopwatch.StartNew();
            var reopened = new UAsset(output, engineVersion, mappings);
            if (!sourceExports.SequenceEqual(SnapshotExports(reopened)))
                throw new InvalidDataException("Reopened prepared package changed export names, classes, or order.");
            if (!String.Equals(sourceImports, SnapshotImports(reopened), StringComparison.Ordinal))
                throw new InvalidDataException("Reopened prepared package changed imports.");
            var finalNameMap = SnapshotNameMap(reopened);
            if (!sourceNameMap.SequenceEqual(finalNameMap.Take(sourceNameMap.Length)) ||
                !expectedAddedNames.SequenceEqual(finalNameMap.Skip(sourceNameMap.Length), StringComparer.Ordinal))
                throw new InvalidDataException("Reopened prepared package failed append-only NameMap validation.");
            var nameIndices = FindNameIndices(finalNameMap, requestedNames, rejectAmbiguous: true);
            var reopenedRewards = ValidateVanillaRewards(reopened, request.Rewards,
                Path.ChangeExtension(output, ".uexp"), new FileInfo(output).Length);
            if (!sourceRewards.SequenceEqual(reopenedRewards))
                throw new InvalidDataException("Reopened prepared package changed a vanilla ArticleDataPath, Count, or notification field.");
            var preparedUexpHash = HashFile(Path.ChangeExtension(output, ".uexp"));
            if (!preparedUexpHash.Equals(sourceUexpHash, StringComparison.Ordinal))
                throw new InvalidDataException("Prepared package .uexp differs from the source bytes.");
            ValidateCopiedSidecar(input, output, ".ubulk");
            ValidateCopiedSidecar(input, output, ".uptnl");
            ValidateExportOffsets(sourceExportTable, reopened, sourceHeaderSize, new FileInfo(output).Length);
            validationTimer.Stop();

            serializationTimer.Stop();
            totalTimer.Stop();
            using var process = Process.GetCurrentProcess();
            process.Refresh();

            return new WorldLootPackageResult(
                request.PackagePath, input, output, "prepared_binary_candidate", null,
                sourceNameMap.Length, finalNameMap.Length, appended,
                sourceHeaderSize, new FileInfo(output).Length,
                sourceUexpSize, new FileInfo(Path.ChangeExtension(output, ".uexp")).Length,
                HashFile(output), preparedUexpHash,
                totalTimer.Elapsed.TotalMilliseconds, process.WorkingSet64,
                nameIndices, fields, outputs, rawExportSizeRestores.ToArray(), true, true, classes,
                loadTimer.Elapsed.TotalMilliseconds, analysisTimer.Elapsed.TotalMilliseconds,
                nameMapTimer.Elapsed.TotalMilliseconds, serializationTimer.Elapsed.TotalMilliseconds,
                serializedValidationTimer.Elapsed.TotalMilliseconds,
                validationTimer.Elapsed.TotalMilliseconds, 1, 0, 1, 1);
        }
        catch
        {
            foreach (var path in outputs)
                if (File.Exists(path)) File.Delete(path);
            throw;
        }
        finally
        {
            foreach (var extension in new[] { ".umap", ".uexp", ".ubulk", ".uptnl" })
                try { File.Delete(Path.ChangeExtension(temporaryHeader, extension)); } catch (IOException) { }
        }
    }


    // Describe serialized reward fields

    private static List<WorldLootBinaryField> DescribeRewardFields(
        WorldLootPackageRequest request, IReadOnlyList<ExportRecord> exportTable,
        string sourceUexp, long sourceHeaderSize, string[] sourceNameMap)
    {
        var fields = new List<WorldLootBinaryField>();
        foreach (var reward in request.Rewards.OrderBy(item => item.ExportIndex).ThenBy(item => item.RewardIndex))
        {
            if (reward.ExportIndex < 1 || reward.ExportIndex > exportTable.Count)
                throw new InvalidDataException("World Loot reward export index is outside the package.");
            var record = exportTable[reward.ExportIndex - 1];
            var payloadStart = checked((int)(record.SerialOffset - sourceHeaderSize));
            var payload = ReadRange(sourceUexp, payloadStart, record.SerialSize);
            var pathOffset = FindUniqueSoftObjectPath(payload, sourceNameMap, reward.Identity,
                $"source export #{reward.ExportIndex}, reward #{reward.RewardIndex}");
            fields.Add(new WorldLootBinaryField(reward.ExportIndex, reward.RewardIndex, reward.ExportName,
                checked(payloadStart + pathOffset), Convert.ToHexString(payload.AsSpan(pathOffset, 16)), reward.Identity));
        }
        return fields;
    }


    // Check original reward values

    private static List<RewardValue> ValidateVanillaRewards(
        UAsset asset, IReadOnlyList<WorldLootRewardRequest> requests, string sidecarPath, long headerSize)
    {
        var values = new List<RewardValue>();
        foreach (var request in requests.OrderBy(item => item.ExportIndex).ThenBy(item => item.RewardIndex))
        {
            if (request.ExportIndex < 1 || request.ExportIndex > asset.Exports.Count)
                throw new InvalidDataException("WorldLoot reward export index is outside the package.");
            var export = asset.Exports[request.ExportIndex - 1];
            if (!String.Equals(export.ObjectName.ToString(), request.ExportName, StringComparison.Ordinal))
                throw new InvalidDataException($"WorldLoot reward export #{request.ExportIndex} name differs: parsed={export.ObjectName}, catalog={request.ExportName}.");
            if (export is RawExport raw)
            {
                // Research-only support for a raw World Loot actor export: require the
                // catalogued ArticleDataPath FName pair to occur exactly once in the
                // untouched export payload. The entire .uexp is then preserved byte-for-byte.
                var sidecarStart = checked((int)(raw.SerialOffset - headerSize));
                var payload = ReadRange(sidecarPath, sidecarStart, raw.SerialSize);
                _ = FindUniqueSoftObjectPath(payload, SnapshotNameMap(asset), request.Identity,
                    $"raw source export #{request.ExportIndex}, reward #{request.RewardIndex}");
                values.Add(new RewardValue(request.ArticlePath, request.Count, request.Notification, request.Identity));
                continue;
            }
            if (export is not NormalExport actor)
                throw new InvalidDataException($"WorldLoot reward export #{request.ExportIndex} parsed as {export.GetType().Name}, not NormalExport or uniquely-scannable RawExport ({export.ObjectName}).");
            var actual = ReadReward(actor, request.RewardIndex);
            if (actual.Path != request.ArticlePath || actual.Count != request.Count || actual.Notification != request.Notification ||
                actual.Identity != request.Identity)
                throw new InvalidDataException($"Vanilla reward mismatch at {request.ExportName}#{request.RewardIndex}: {actual.Path}.");
            values.Add(actual);
        }
        return values;
    }


    // Read the serialized reward

    private static RewardValue ReadReward(NormalExport actor, int rewardIndex)
    {
        var articles = actor.Data.OfType<StructPropertyData>()
            .Single(property => property.Name.ToString() == "ArticleBulkInfo")
            .Value.OfType<ArrayPropertyData>()
            .Single(property => property.Name.ToString() == "Articles");
        if (rewardIndex < 0 || rewardIndex >= articles.Value.Count())
            throw new InvalidDataException("Catalog reward index is outside the parsed Articles array.");
        var article = articles.Value.ElementAt(rewardIndex) as StructPropertyData
            ?? throw new InvalidDataException("Catalog Article entry is not a struct.");
        var path = (FSoftObjectPath)article.Value.OfType<SoftObjectPropertyData>()
            .Single(property => property.Name.ToString() == "ArticleDataPath").Value!;
        var package = path.AssetPath.PackageName;
        var asset = path.AssetPath.AssetName;
        var identity = new SoftPathIdentity(
            new FNameIdentity(package.Value.ToString(), package.Number),
            new FNameIdentity(asset.Value.ToString(), asset.Number));
        var logicalPath = Resolve(identity.PackageName);
        var logicalAsset = Resolve(identity.AssetName);
        if (logicalAsset != logicalPath[(logicalPath.LastIndexOf('/') + 1)..])
            throw new InvalidDataException("ArticleDataPath contains a subobject or package/object path mismatch.");
        var count = article.Value.OfType<IntPropertyData>()
            .Single(property => property.Name.ToString() == "Count").Value;
        var notification = article.Value.OfType<BoolPropertyData>()
            .Single(property => property.Name.ToString() == "bNotification").Value;
        return new RewardValue(logicalPath, count, notification, identity);
    }


    // Check export offsets

    private static void ValidateExportOffsets(
        IReadOnlyList<ExportRecord> source, UAsset reopened, long sourceHeaderSize, long outputHeaderSize)
    {
        if (source.Count != reopened.Exports.Count) throw new InvalidDataException("Prepared export count changed.");
        var delta = outputHeaderSize - sourceHeaderSize;
        for (var index = 0; index < source.Count; index++)
            if (source[index].SerialSize != reopened.Exports[index].SerialSize ||
                source[index].SerialOffset != reopened.Exports[index].SerialOffset - delta)
                throw new InvalidDataException($"Prepared ExportMap offset mismatch at export #{index + 1}.");
    }


    // Check copied package sidecars

    private static void ValidateCopiedSidecar(string input, string output, string extension)
    {
        var source = Path.ChangeExtension(input, extension);
        var target = Path.ChangeExtension(output, extension);
        if (File.Exists(source) != File.Exists(target))
            throw new InvalidDataException($"Prepared sidecar presence changed for {extension}.");
        if (File.Exists(source) && !HashFile(source).Equals(HashFile(target), StringComparison.Ordinal))
            throw new InvalidDataException($"Prepared sidecar bytes changed for {extension}.");
    }


    // Find NameMap indices

    private static Dictionary<string, int> FindNameIndices(string[] nameMap, IEnumerable<string> names, bool rejectAmbiguous)
    {
        var result = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var name in names)
        {
            var hits = Enumerable.Range(0, nameMap.Length)
                .Where(index => String.Equals(nameMap[index], name, StringComparison.Ordinal)).ToArray();
            if (rejectAmbiguous && hits.Length > 1)
                throw new InvalidDataException($"NameMap literal {name} is ambiguous ({hits.Length} entries).");
            if (hits.Length == 1) result.Add(name, hits[0]);
        }
        return result;
    }


    // Capture the NameMap entries

    private static string[] SnapshotNameMap(UAsset asset) =>
        asset.GetNameMapIndexList().Select((_, index) => asset.GetNameReference(index).ToString()).ToArray();


    // Inspect classes

    private static List<MapClassInspection> InspectClasses(UAsset asset, WorldLootPackageRequest request)
    {
        var results = new List<MapClassInspection>();
        foreach (var oneBasedIndex in (request.ClassExportIndices ?? new List<int>()).Distinct().Order())
        {
            var index = oneBasedIndex - 1;
            if (index < 0 || index >= asset.Exports.Count)
                throw new ArgumentOutOfRangeException(nameof(request.ClassExportIndices));
            var export = asset.Exports[index];
            var (className, packagePath) = ResolveClassImport(asset, export.ClassIndex);
            results.Add(new MapClassInspection(export.ObjectName.ToString(), oneBasedIndex, className, packagePath));
        }
        return results;
    }


    // Inspect classes

    private static List<MapClassInspection> InspectClasses(
        string workRoot, WorldLootPackageRequest request, EngineVersion engineVersion, Usmap mappings)
    {
        var input = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
        return InspectClasses(new UAsset(input, engineVersion, mappings), request);
    }

    private static (string ClassName, string PackagePath) ResolveClassImport(UAsset asset, FPackageIndex packageIndex)
    {
        if (!packageIndex.IsImport())
            return (packageIndex.IsExport() ? "ExportClass" : "None", String.Empty);
        var first = packageIndex.ToImport(asset);
        var className = first.ClassName.ToString();
        var current = packageIndex;
        var visited = new HashSet<int>();
        while (current.IsImport() && visited.Add(current.Index))
        {
            var import = current.ToImport(asset);
            if (import.ClassName.ToString() == "Package")
                return (className, import.ObjectName.ToString());
            current = import.OuterIndex;
        }
        return (className, String.Empty);
    }

    private static (string Name, string? Type)[] SnapshotExports(UAsset asset) =>
        asset.Exports.Select(export => (export.ObjectName.ToString(), export.GetType().FullName)).ToArray();


    // Capture package imports

    private static string SnapshotImports(UAsset asset) => String.Join("\0", asset.Imports.Select(item =>
        $"{item.ClassPackage}\u001f{item.ClassName}\u001f{item.ObjectName}\u001f{item.OuterIndex.Index}"));


    // Find a unique soft object path

    private static int FindUniqueSoftObjectPath(byte[] payload, string[] nameMap, SoftPathIdentity identity, string label)
    {
        var matches = new List<int>();
        for (var offset = 0; offset <= payload.Length - 16; offset++)
        {
            var packageIndex = BinaryPrimitives.ReadInt32LittleEndian(payload.AsSpan(offset, 4));
            var packageNumber = BinaryPrimitives.ReadInt32LittleEndian(payload.AsSpan(offset + 4, 4));
            var assetIndex = BinaryPrimitives.ReadInt32LittleEndian(payload.AsSpan(offset + 8, 4));
            var assetNumber = BinaryPrimitives.ReadInt32LittleEndian(payload.AsSpan(offset + 12, 4));
            if (packageIndex < 0 || packageIndex >= nameMap.Length || assetIndex < 0 || assetIndex >= nameMap.Length) continue;
            if (packageNumber == identity.PackageName.Number && assetNumber == identity.AssetName.Number &&
                String.Equals(nameMap[packageIndex], identity.PackageName.Value, StringComparison.Ordinal) &&
                String.Equals(nameMap[assetIndex], identity.AssetName.Value, StringComparison.Ordinal))
                matches.Add(offset);
        }
        if (matches.Count != 1)
            throw new InvalidDataException($"{label} ArticleDataPath is not unique in raw export ({matches.Count} matches).");
        return matches[0];
    }


    // Read range

    private static byte[] ReadRange(string path, int offset, long length)
    {
        if (length < 0 || length > Int32.MaxValue) throw new InvalidDataException("Raw export is too large for the pilot helper.");
        var result = new byte[(int)length];
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        stream.Position = offset;
        stream.ReadExactly(result);
        return result;
    }


    // Encode export table offsets

    private static byte[] EncodeExportTablePair(long size, long offset)
    {
        var bytes = new byte[16];
        BinaryPrimitives.WriteInt64LittleEndian(bytes.AsSpan(0, 8), size);
        BinaryPrimitives.WriteInt64LittleEndian(bytes.AsSpan(8, 8), offset);
        return bytes;
    }


    // Find an exact byte sequence

    private static int FindUniqueByteSequence(byte[] source, byte[] needle, string label)
    {
        var first = source.AsSpan().IndexOf(needle);
        if (first < 0 || source.AsSpan(first + 1).IndexOf(needle) >= 0)
            throw new InvalidDataException($"Could not uniquely locate {label}.");
        return first;
    }


    // Resolve the requested asset

    private static string Resolve(FNameIdentity name) =>
        name.Number == 0 ? name.Value : $"{name.Value}_{name.Number - 1}";


    // Hash file

    private static string HashFile(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }


    // Resolve an output path

    private static string ResolveOwnedPath(string rootValue, string value, bool mustExist)
    {
        var root = Path.GetFullPath(rootValue);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(root, path);
        if (relative is "." or ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new InvalidDataException("WorldLoot package path escaped its isolated workspace.");
        if (mustExist && !File.Exists(path)) throw new FileNotFoundException("WorldLoot package was not found.", path);
        return path;
    }

    private static IEnumerable<(string Temporary, string Final)> PackageFilePairs(string temporaryHeader, string outputHeader)
    {
        yield return (temporaryHeader, outputHeader);
        foreach (var extension in new[] { ".uexp", ".ubulk", ".uptnl" })
            yield return (Path.ChangeExtension(temporaryHeader, extension), Path.ChangeExtension(outputHeader, extension));
    }


    // Describe a World Loot manifest

    private sealed record WorldLootManifest(List<WorldLootPackageRequest> Packages);
    // Describe a package request
    private sealed record WorldLootPackageRequest(
        string PackagePath, string InputFile, string OutputFile,
        List<string> NameValues, List<WorldLootRewardRequest> Rewards,
        List<int>? ClassExportIndices = null);
    // Map class inspection
    private sealed record MapClassInspection(string ExportName, int ExportIndex, string ClassName, string PackagePath);
    // Describe a reward request
    private sealed record WorldLootRewardRequest(
        int ExportIndex, int RewardIndex, string ExportName, string ArticlePath,
        int Count, bool Notification, SoftPathIdentity Identity);
    // Describe a NameMap identity
    private sealed record FNameIdentity(string Value, int Number);
    // Describe a soft path identity
    private sealed record SoftPathIdentity(FNameIdentity PackageName, FNameIdentity AssetName);
    // Describe a serialized reward
    private sealed record RewardValue(string Path, int Count, bool Notification, SoftPathIdentity Identity);
    // Describe a package export
    private sealed record ExportRecord(long SerialSize, long SerialOffset);
    // Describe a binary reward field
    private sealed record WorldLootBinaryField(
        int ExportIndex, int RewardIndex, string ExportName, int SidecarOffset,
        string ExpectedBytesHex, SoftPathIdentity ExpectedIdentity);
    // Describe a package patch result
    private sealed record WorldLootPackageResult(
        string PackagePath, string InputFile, string OutputFile, string Status, string? FallbackReason,
        int NameMapCountBefore, int NameMapCountAfter, string[] NamesAdded,
        long UassetSizeBefore, long UassetSizeAfter, long UexpSizeBefore, long UexpSizeAfter,
        string PreparedUassetSha256, string PreparedUexpSha256,
        double PreparationMilliseconds, long WorkingSetBytes,
        Dictionary<string, int> NameMapIndices, List<WorldLootBinaryField> Fields,
        List<string> OutputFiles, string[] RawExportSizeRestores,
        bool ReopenedAndValidated, bool RawUexpByteIdentical,
        List<MapClassInspection> Classes,
        double LoadMilliseconds, double AnalysisMilliseconds, double NameMapMilliseconds,
        double SerializationMilliseconds, double SerializationReopenMilliseconds,
        double ValidationReopenMilliseconds,
        int DiscoveryOpenCount, int PreparationReloadCount,
        int SerializationReopenCount, int ValidationReopenCount);
}
