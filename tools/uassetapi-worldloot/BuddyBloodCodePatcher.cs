using System.Security.Cryptography;
using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class BuddyBloodCodePatcher
{
    private const string ArticlePrefix = "/Game/Blueprint/Article/BloodCode/";
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
            var version = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<InspectManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Buddy Blood Code inspection manifest is empty.");
            ValidateUniquePackages(manifest.Packages.Select(item => item.VirtualPackage));
            var results = manifest.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase)
                .Select(item => InspectPackage(root, mappings, version, item)).ToArray();
            Console.WriteLine(JsonSerializer.Serialize(results, JsonOptions));
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
        var committed = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            var root = Path.GetFullPath(args[1]);
            var mappings = new Usmap(Path.GetFullPath(args[2]));
            var version = Enum.Parse<EngineVersion>(args[3], ignoreCase: false);
            var manifest = JsonSerializer.Deserialize<PatchManifest>(File.ReadAllText(args[4]), JsonOptions)
                ?? throw new InvalidDataException("Buddy Blood Code patch manifest is empty.");
            ValidateUniquePackages(manifest.Packages.Select(item => item.VirtualPackage));
            var results = new List<PackagePatchResult>();
            foreach (var request in manifest.Packages.OrderBy(item => item.VirtualPackage, StringComparer.OrdinalIgnoreCase))
            {
                var result = PatchPackage(root, mappings, version, request);
                foreach (var path in result.OutputFiles) committed.Add(path);
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


    // Inspect one asset package

    private static PackageInspection InspectPackage(
        string root, Usmap mappings, EngineVersion version, PackageInspectRequest request)
    {
        ValidateVirtualPackage(request.VirtualPackage);
        var input = ResolveOwnedPath(root, request.InputFile, mustExist: true);
        var asset = LoadPackage(input, version, mappings);
        var references = request.References.Select(reference => InspectReference(asset, mappings, request.VirtualPackage, reference)).ToArray();
        return new PackageInspection(request.VirtualPackage, input, references.Length, references);
    }


    // Inspect reference

    private static ObjectReferenceHit InspectReference(
        UAsset asset, Usmap mappings, string virtualPackage, ObjectReferenceRequest request)
    {
        ValidateArticleIdentity(request.ExpectedPackage, request.ExpectedAsset, request.ExpectedAssetNameNumber);
        if (request.ReferenceProperty != "BloodCode")
            throw new InvalidDataException("Buddy Blood Code catalog must target the BuddySettings.BloodCode property.");
        var export = GetTargetExport(asset, request.ExportIndex, request.ExportName);
        var className = export.ClassIndex.IsImport()
            ? export.ClassIndex.ToImport(asset).ObjectName.ToString()
            : string.Empty;
        if (className != "BuddySettings")
            throw new InvalidDataException($"{request.ExportName} is not a BuddySettings export.");
        var schema = mappings.Schemas.FirstOrDefault(item => item.Key.Equals("BuddySettings", StringComparison.OrdinalIgnoreCase)).Value
            ?? throw new InvalidDataException("CV2.usmap has no BuddySettings property schema.");
        var matches = schema.Properties.Where(item => item.Value.Name.ToString() == request.ReferenceProperty).ToArray();
        if (matches.Length != 1 || matches[0].Key != request.PropertySchemaIndex ||
            !matches[0].Value.ToString().Contains("ObjectProperty", StringComparison.Ordinal))
            throw new InvalidDataException("BuddySettings.BloodCode does not match the catalogued ObjectProperty schema index.");

        var articleImports = asset.Imports.Select((item, index) => (item, index))
            .Where(item => item.item.ClassName.ToString() == request.ExpectedImportClassName
                && item.item.ClassPackage.ToString() == request.ExpectedImportClassPackage
                && item.item.ObjectName.ToString() == request.ExpectedAsset
                && item.item.ObjectName.Number == request.ExpectedAssetNameNumber)
            .ToArray();
        if (articleImports.Length != 1)
            throw new InvalidDataException("BuddySettings source does not contain one exact BloodCodeArticleData import.");
        var articleImport = articleImports[0];
        var packageImportIndex = ResolvePackageImportIndex(asset, articleImport.item.OuterIndex);
        var packageImport = asset.Imports[packageImportIndex];
        var actualPackage = packageImport.ObjectName.ToString();
        if (actualPackage != request.ExpectedPackage || packageImport.ObjectName.Number != request.ExpectedPackageNameNumber)
            throw new InvalidDataException("BuddySettings BloodCodeArticleData import has an unexpected exact package path/FName number.");
        if (packageImport.ClassName.ToString() != "Package" || packageImport.OuterIndex.Index != 0)
            throw new InvalidDataException("BloodCodeArticleData import has an invalid package outer chain.");

        var expectedDependencyIndex = -articleImport.index - 1;
        var targetExportIndex = request.ExportIndex - 1;
        var owners = asset.Exports.Select((item, index) => (item, index))
            .Where(item => item.item.CreateBeforeSerializationDependencies.Any(dep => dep.Index == expectedDependencyIndex))
            .Select(item => item.index + 1).ToArray();
        if (!export.CreateBeforeSerializationDependencies.Any(dep => dep.Index == expectedDependencyIndex)
            || !owners.SequenceEqual(new[] { request.ExportIndex }))
            throw new InvalidDataException("BloodCodeArticleData import is not uniquely owned by the catalogued BuddySettings export.");

        return new ObjectReferenceHit(
            virtualPackage, request.ExportIndex, export.ObjectName.ToString(), request.ReferenceProperty,
            request.PropertySchemaIndex, articleImport.index, packageImportIndex,
            request.ExpectedImportClassName, request.ExpectedImportClassPackage,
            actualPackage, request.ExpectedPackageNameNumber, articleImport.item.ObjectName.ToString(),
            articleImport.item.ObjectName.Number, owners);
    }


    // Inspect reference

    private static ObjectReferenceHit InspectReference(
        UAsset asset, Usmap mappings, string virtualPackage, ObjectReferencePatchRequest request) =>
        InspectReference(asset, mappings, virtualPackage, new ObjectReferenceRequest(
            request.ExportIndex, request.ExportName, request.ReferenceProperty, request.PropertySchemaIndex,
            request.ExpectedPackage, request.ExpectedAsset, request.ExpectedPackageNameNumber,
            request.ExpectedAssetNameNumber, request.ExpectedImportClassName, request.ExpectedImportClassPackage));


    // Patch one asset package

    private static PackagePatchResult PatchPackage(
        string root, Usmap mappings, EngineVersion version, PackagePatchRequest request)
    {
        ValidateVirtualPackage(request.VirtualPackage);
        if (request.Assignments.Count == 0)
            throw new InvalidDataException($"{request.VirtualPackage} has no changed Buddy Blood Code assignments.");
        var input = ResolveOwnedPath(root, request.InputFile, mustExist: true);
        var output = ResolveOwnedPath(root, request.OutputFile, mustExist: false);
        if (input.Equals(output, StringComparison.OrdinalIgnoreCase) || PackageFiles(output).Any(File.Exists))
            throw new IOException("Buddy Blood Code output must be new and separate from its source.");
        var asset = LoadPackage(input, version, mappings);
        var exportSnapshot = SnapshotExports(asset);
        var referenceSnapshots = request.Assignments.Select(item => InspectReference(asset, mappings, request.VirtualPackage, item)).ToArray();
        if (referenceSnapshots.Select(item => item.ExportIndex).Distinct().Count() != referenceSnapshots.Length)
            throw new InvalidDataException("Buddy Blood Code patch contains duplicate or conflicting export destinations.");

        foreach (var assignment in request.Assignments)
        {
            ValidateArticleIdentity(assignment.AssignedPackage, assignment.AssignedAsset, 0);
            var snapshot = referenceSnapshots.Single(item => item.ExportIndex == assignment.ExportIndex);
            var articleImport = asset.Imports[snapshot.ImportIndex];
            var oldPackageImport = asset.Imports[snapshot.PackageImportIndex];
            var targetPackageImports = asset.Imports.Select((item, index) => (item, index))
                .Where(pair => pair.item.ClassName.ToString() == "Package"
                    && pair.item.ObjectName.ToString() == assignment.AssignedPackage
                    && pair.item.ObjectName.Number == 0
                    && pair.item.OuterIndex.Index == 0).ToArray();
            if (targetPackageImports.Length > 1)
                throw new InvalidDataException("Buddy Blood Code target package import is ambiguous.");
            int targetPackageImportIndex;
            if (targetPackageImports.Length == 1)
            {
                targetPackageImportIndex = targetPackageImports[0].index;
            }
            else
            {
                var oldPackageChildren = asset.Imports.Count(item => item.OuterIndex.Index == -snapshot.PackageImportIndex - 1);
                if (oldPackageChildren == 1)
                {
                    oldPackageImport.ObjectName = new FName(asset, assignment.AssignedPackage);
                    targetPackageImportIndex = snapshot.PackageImportIndex;
                }
                else
                {
                    targetPackageImportIndex = asset.Imports.Count;
                    asset.Imports.Add(new Import(
                        new FName(asset, "/Script/CoreUObject"), new FName(asset, "Package"),
                        new FPackageIndex(0), new FName(asset, assignment.AssignedPackage), false));
                }
            }
            articleImport.ObjectName = new FName(asset, assignment.AssignedAsset);
            articleImport.OuterIndex = FPackageIndex.FromImport(targetPackageImportIndex);
        }

        var outputDirectory = Path.GetDirectoryName(output)!;
        Directory.CreateDirectory(outputDirectory);
        var temporary = Path.Combine(outputDirectory,
            Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + Path.GetExtension(output));
        var moved = new List<string>();
        try
        {
            asset.Write(temporary);
            CopyOpaqueSidecars(input, temporary);
            var sourceUexp = Path.ChangeExtension(input, ".uexp");
            var temporaryUexp = Path.ChangeExtension(temporary, ".uexp");
            if (File.Exists(sourceUexp) && (!File.Exists(temporaryUexp) || !SameBytes(sourceUexp, temporaryUexp)))
                throw new InvalidDataException("Buddy Blood Code import-only patch changed or omitted the package .uexp payload.");

            var reopened = LoadPackage(temporary, version, mappings);
            ValidateExportsUnchanged(exportSnapshot, reopened);
            foreach (var assignment in request.Assignments)
            {
                var expected = assignment with
                {
                    ExpectedPackage = assignment.AssignedPackage,
                    ExpectedAsset = assignment.AssignedAsset,
                    ExpectedPackageNameNumber = 0,
                    ExpectedAssetNameNumber = 0,
                };
                _ = InspectReference(reopened, mappings, request.VirtualPackage, expected);
            }
            var staged = PackageFiles(temporary).Where(File.Exists).ToArray();
            if (!staged.Contains(temporary, StringComparer.OrdinalIgnoreCase))
                throw new InvalidDataException("Buddy Blood Code patch output omitted the package header.");
            var outputs = staged.Select(path => Path.ChangeExtension(output, Path.GetExtension(path))).ToArray();
            if (outputs.Any(File.Exists)) throw new IOException("Refusing to overwrite a Buddy Blood Code output sidecar.");
            for (var index = 0; index < staged.Length; index++)
            {
                File.Move(staged[index], outputs[index]);
                moved.Add(outputs[index]);
            }
            return new PackagePatchResult(request.VirtualPackage, input, output, request.Assignments.Count, moved.ToArray());
        }
        catch
        {
            foreach (var path in moved)
                try { File.Delete(path); } catch (IOException) { }
            throw;
        }
        finally
        {
            foreach (var path in PackageFiles(temporary))
                try { File.Delete(path); } catch (IOException) { }
        }
    }


    // Load the Unreal package

    private static UAsset LoadPackage(string path, EngineVersion version, Usmap mappings) =>
        new(path, true, version, mappings,
            CustomSerializationFlags.SkipParsingExports | CustomSerializationFlags.SkipPreloadDependencyLoading);


    // Find the target export

    private static Export GetTargetExport(UAsset asset, int exportIndex, string exportName)
    {
        if (exportIndex < 1 || exportIndex > asset.Exports.Count)
            throw new InvalidDataException("Buddy Blood Code export index is outside the package export table.");
        var export = asset.Exports[exportIndex - 1];
        if (!String.Equals(export.ObjectName.ToString(), exportName, StringComparison.Ordinal))
            throw new InvalidDataException("Buddy Blood Code export name differs from its catalogued identity.");
        return export;
    }


    // Resolve package import index

    private static int ResolvePackageImportIndex(UAsset asset, FPackageIndex outer)
    {
        if (!outer.IsImport()) throw new InvalidDataException("BloodCodeArticleData import is not nested under a package import.");
        var importIndex = -outer.Index - 1;
        if (importIndex < 0 || importIndex >= asset.Imports.Count)
            throw new InvalidDataException("BloodCodeArticleData import outer index is outside the import table.");
        return importIndex;
    }


    // Capture package exports

    private static string[] SnapshotExports(UAsset asset) => asset.Exports.Select(export =>
    {
        if (export is not RawExport raw)
            throw new InvalidDataException("Buddy Blood Code import patch requires raw, unparsed package exports.");
        return JsonSerializer.Serialize(new
        {
            name = export.ObjectName.ToString(),
            nameNumber = export.ObjectName.Number,
            outer = export.OuterIndex.Index,
            @class = export.ClassIndex.Index,
            template = export.TemplateIndex.Index,
            serialSize = export.SerialSize,
            flags = export.ObjectFlags.ToString(),
            serializationDependencies = export.CreateBeforeSerializationDependencies.Select(item => item.Index).ToArray(),
            createDependencies = export.CreateBeforeCreateDependencies.Select(item => item.Index).ToArray(),
            dataSha256 = Convert.ToHexString(SHA256.HashData(raw.Data)),
        });
    }).ToArray();


    // Check unrelated exports unchanged

    private static void ValidateExportsUnchanged(string[] expected, UAsset observed)
    {
        var actual = SnapshotExports(observed);
        if (!expected.SequenceEqual(actual, StringComparer.Ordinal))
        {
            var index = Enumerable.Range(0, Math.Min(expected.Length, actual.Length))
                .FirstOrDefault(item => !String.Equals(expected[item], actual[item], StringComparison.Ordinal));
            throw new InvalidDataException($"Buddy Blood Code import-only patch changed export {index + 1} header/payload. Before={expected[index]} After={actual[index]}");
        }
    }


    // Copy opaque sidecars

    private static void CopyOpaqueSidecars(string inputHeader, string outputHeader)
    {
        foreach (var extension in new[] { ".uexp", ".ubulk", ".uptnl" })
        {
            var source = Path.ChangeExtension(inputHeader, extension);
            var target = Path.ChangeExtension(outputHeader, extension);
            if (File.Exists(source) && !File.Exists(target)) File.Copy(source, target);
        }
        foreach (var extension in new[] { ".ubulk", ".uptnl" })
        {
            var source = Path.ChangeExtension(inputHeader, extension);
            var target = Path.ChangeExtension(outputHeader, extension);
            if (File.Exists(source) && !SameBytes(source, target))
                throw new InvalidDataException($"Buddy Blood Code patch changed opaque {extension} payload.");
        }
    }


    // Compare byte arrays

    private static bool SameBytes(string left, string right) => File.Exists(left) && File.Exists(right)
        && new FileInfo(left).Length == new FileInfo(right).Length
        && SHA256.HashData(File.ReadAllBytes(left)).AsSpan().SequenceEqual(SHA256.HashData(File.ReadAllBytes(right)));


    // Resolve an output path

    private static string ResolveOwnedPath(string root, string path, bool mustExist)
    {
        var full = Path.GetFullPath(path);
        var prefix = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Buddy Blood Code package paths must stay inside the generation workspace.");
        if (mustExist && !File.Exists(full)) throw new FileNotFoundException("Buddy Blood Code package was not found.", full);
        return full;
    }


    // Collect package files

    private static string[] PackageFiles(string header) => new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" }
        .Select(extension => Path.ChangeExtension(header, extension)).ToArray();


    // Check Article identity

    private static void ValidateArticleIdentity(string package, string asset, int nameNumber)
    {
        if (!package.StartsWith(ArticlePrefix, StringComparison.Ordinal)
            || package.Contains('\\') || package.EndsWith('/')
            || package.Split('/').Any(part => part is "." or "..")
            || package[(package.LastIndexOf('/') + 1)..] != asset || asset.Length == 0 || nameNumber < 0)
            throw new InvalidDataException("Buddy Blood Code Article identity is not a canonical exact /Game path.");
    }


    // Check the virtual package

    private static void ValidateVirtualPackage(string package)
    {
        if (!package.StartsWith("/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/", StringComparison.Ordinal)
            || package.Contains('\\') || package.EndsWith('/')
            || package.Split('/').Any(part => part is "." or ".."))
            throw new InvalidDataException("Buddy Blood Code owner is outside the validated BuddySettings package scope.");
    }


    // Check for duplicate packages

    private static void ValidateUniquePackages(IEnumerable<string> packages)
    {
        var values = packages.ToArray();
        if (values.Length == 0 || values.Distinct(StringComparer.OrdinalIgnoreCase).Count() != values.Length)
            throw new InvalidDataException("Buddy Blood Code operations require unique non-empty package targets.");
    }


    // Read the inspection manifest

    private sealed record InspectManifest(IReadOnlyList<PackageInspectRequest> Packages);
    // Describe package inspection request
    private sealed record PackageInspectRequest(string VirtualPackage, string InputFile, IReadOnlyList<ObjectReferenceRequest> References);
    // Apply the patch manifest
    private sealed record PatchManifest(IReadOnlyList<PackagePatchRequest> Packages);
    // Describe package patch request
    private sealed record PackagePatchRequest(string VirtualPackage, string InputFile, string OutputFile, IReadOnlyList<ObjectReferencePatchRequest> Assignments);
    // Describe an object reference request
    private sealed record ObjectReferenceRequest(
        int ExportIndex, string ExportName, string ReferenceProperty, int PropertySchemaIndex,
        string ExpectedPackage, string ExpectedAsset, int ExpectedPackageNameNumber, int ExpectedAssetNameNumber,
        string ExpectedImportClassName, string ExpectedImportClassPackage);
    // Describe an object reference patch
    private sealed record ObjectReferencePatchRequest(
        int ExportIndex, string ExportName, string ReferenceProperty, int PropertySchemaIndex,
        string ExpectedPackage, string ExpectedAsset, int ExpectedPackageNameNumber, int ExpectedAssetNameNumber,
        string ExpectedImportClassName, string ExpectedImportClassPackage,
        string AssignedPackage, string AssignedAsset);
    // Describe a matching object reference
    private sealed record ObjectReferenceHit(
        string Package, int ExportIndex, string ExportName, string ReferenceProperty, int PropertySchemaIndex,
        int ImportIndex, int PackageImportIndex, string ImportClassName, string ImportClassPackage,
        string RewardPackage, int PackageNameNumber, string RewardAsset, int AssetNameNumber,
        IReadOnlyList<int> OwnerExports);
    // Describe package inspection
    private sealed record PackageInspection(string VirtualPackage, string InputFile, int ReferenceCount, IReadOnlyList<ObjectReferenceHit> References);
    // Describe package patch results
    private sealed record PackagePatchResult(string VirtualPackage, string InputFile, string OutputFile, int PatchedReferences, IReadOnlyList<string> OutputFiles);
}
