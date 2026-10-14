using System.Text.Json;
using UAssetAPI;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class ShopPatcher
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };


    // Prepare package NameMaps

    public static int PrepareNameMaps(string[] args)
    {
        var committed = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            var workRoot = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var engineVersion = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<NameMapManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Shop name-map manifest is empty.");
            if (manifest.Packages.Count == 0)
                throw new InvalidDataException("Shop name-map manifest contains no packages.");

            var results = new List<NameMapResult>();
            foreach (var request in manifest.Packages)
            {
                var result = PrepareOne(workRoot, request, engineVersion, mappings);
                foreach (var output in result.OutputFiles) committed.Add(Path.GetFullPath(output));
                results.Add(result);
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


    // Prepare one

    private static NameMapResult PrepareOne(string workRoot, NameMapRequest request, EngineVersion engineVersion, Usmap mappings)
    {
        var inputHeader = ResolveOwnedPath(workRoot, request.InputFile, mustExist: true);
        var outputHeader = ResolveOwnedPath(workRoot, request.OutputFile, mustExist: false);
        if (String.Equals(inputHeader, outputHeader, StringComparison.OrdinalIgnoreCase) || File.Exists(outputHeader))
            throw new IOException($"{request.Family} output header must be new and separate from its source.");
        if (request.Names.Count == 0 || request.Names.Any(String.IsNullOrWhiteSpace))
            throw new InvalidDataException($"{request.Family} has an empty or invalid FName request.");

        var asset = new UAsset(inputHeader, engineVersion, mappings);
        foreach (var name in request.Names.Distinct(StringComparer.Ordinal))
            _ = new FName(asset, name);
        var requestedNames = request.Names.ToHashSet(StringComparer.Ordinal);
        var nameIndices = new Dictionary<string, int>(StringComparer.Ordinal);
        var nameMap = asset.GetNameMapIndexList();
        for (var index = 0; index < nameMap.Count; index++)
        {
            var name = asset.GetNameReference(index).ToString();
            if (requestedNames.Contains(name) && !nameIndices.ContainsKey(name))
                nameIndices.Add(name, index);
        }
        if (nameIndices.Count != requestedNames.Count)
            throw new InvalidDataException("One or more requested FNames were not present after Name Map preparation.");

        var directory = Path.GetDirectoryName(outputHeader)!;
        Directory.CreateDirectory(directory);
        var temporaryHeader = Path.Combine(directory,
            Path.GetFileNameWithoutExtension(outputHeader) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(outputHeader));
        var moved = new List<string>();
        try
        {
            asset.Write(temporaryHeader);
            if (!File.Exists(temporaryHeader))
                throw new InvalidDataException("UAssetAPI omitted the shop package header while extending its name map.");

            // The existing Python parser/patcher keeps the table export byte-accurate.
            // This operation only needs UAssetAPI to serialize the added FName entries.
            foreach (var extension in new[] { ".ubulk", ".uptnl" })
            {
                var source = Path.ChangeExtension(inputHeader, extension);
                var temporary = Path.ChangeExtension(temporaryHeader, extension);
                if (File.Exists(source) && !File.Exists(temporary)) File.Copy(source, temporary);
            }

            var outputFiles = new List<string> { outputHeader };
            foreach (var extension in new[] { ".ubulk", ".uptnl" })
            {
                var temporary = Path.ChangeExtension(temporaryHeader, extension);
                if (File.Exists(temporary)) outputFiles.Add(Path.ChangeExtension(outputHeader, extension));
            }
            if (outputFiles.Any(File.Exists))
                throw new IOException($"Refusing to overwrite a staged {request.Family} name-map file.");

            var temporaryFiles = new List<string> { temporaryHeader };
            temporaryFiles.AddRange(new[] { ".ubulk", ".uptnl" }
                .Select(extension => Path.ChangeExtension(temporaryHeader, extension)).Where(File.Exists));
            for (var index = 0; index < temporaryFiles.Count; index++)
            {
                File.Move(temporaryFiles[index], outputFiles[index]);
                moved.Add(outputFiles[index]);
            }
            return new NameMapResult(request.Family, inputHeader, outputHeader, request.Names.Count, outputFiles, nameIndices);
        }
        catch
        {
            foreach (var path in moved)
                try { File.Delete(path); } catch (IOException) { }
            throw;
        }
        finally
        {
            foreach (var extension in new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" })
                try { File.Delete(Path.ChangeExtension(temporaryHeader, extension)); } catch (IOException) { }
        }
    }


    // Resolve an output path

    private static string ResolveOwnedPath(string rootValue, string value, bool mustExist)
    {
        var root = Path.GetFullPath(rootValue);
        var path = Path.GetFullPath(value);
        var relative = Path.GetRelativePath(root, path);
        if (relative is "." or ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new InvalidDataException("Shop package path escaped the generation workspace.");
        if (mustExist && !File.Exists(path)) throw new FileNotFoundException("Shop package was not found.", path);
        return path;
    }


    // Describe a NameMap manifest

    private sealed record NameMapManifest(IReadOnlyList<NameMapRequest> Packages);
    // Describe a NameMap request
    private sealed record NameMapRequest(string Family, string InputFile, string OutputFile, IReadOnlyList<string> Names);
    // Describe NameMap output
    private sealed record NameMapResult(string Family, string InputFile, string OutputFile, int AddedNames,
        IReadOnlyList<string> OutputFiles, IReadOnlyDictionary<string, int> NameIndices);
}
