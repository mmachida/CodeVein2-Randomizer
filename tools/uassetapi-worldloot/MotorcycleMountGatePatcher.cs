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

internal static class MotorcycleMountGatePatcher
{
    private const string ExpectedVirtualPackage = "/Game/Blueprint/Characters/Player/BP_Player";
    private const string ComponentName = "MountControlComponent";
    private const string ConditionName = "AvailableStoryFlagCondition";
    private const string FormulaName = "EvaluationFormula";
    private const string VanillaFormula = "MainEpisode.Beginning >= e10480 || CLear_e10515";
    private const string UnlockedFormula = "BloodMagic_BikeCreator";
    private const string FormulaPropertyPath = "MountControlComponent.AvailableStoryFlagCondition.EvaluationFormula";


    // Patch

    public static int Patch(string[] args)
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
                ?? throw new InvalidDataException("Motorcycle mount gate request is empty.");
            if (request.VirtualPackage != ExpectedVirtualPackage)
                throw new InvalidDataException("Motorcycle mount gate request names an unexpected player package.");

            var input = Path.GetFullPath(request.InputFile);
            var output = Path.GetFullPath(request.OutputFile);
            if (!File.Exists(input) || !IsWithin(input, workRoot) || !IsWithin(output, workRoot) || input == output || File.Exists(output))
                throw new InvalidDataException("Motorcycle player output must be a new staged package inside the generation workspace.");

            var asset = new UAsset(input, engineVersion, mappings);
            var component = ResolveComponent(asset, input);
            var (condition, formula) = ResolveFormula(component);
            var currentFormula = formula.Value.ToString();
            if (!String.Equals(currentFormula, VanillaFormula, StringComparison.Ordinal))
                throw new InvalidDataException($"Motorcycle availability formula changed from the audited value: {currentFormula}");

            var originalSnapshot = SnapshotComponent(component);
            formula.Value = new FString(UnlockedFormula);
            var directory = Path.GetDirectoryName(output)!;
            Directory.CreateDirectory(directory);
            temporaryHeader = Path.Combine(directory,
                Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + ".uasset");
            asset.Write(temporaryHeader);
            CopySidecars(input, temporaryHeader);

            var reopened = new UAsset(temporaryHeader, engineVersion, mappings);
            var reopenedComponent = ResolveComponent(reopened, temporaryHeader);
            ValidatePatchedComponent(originalSnapshot, reopenedComponent);

            var temporaryFiles = PackageFiles(temporaryHeader).Where(File.Exists).ToArray();
            foreach (var source in temporaryFiles)
            {
                var target = Path.ChangeExtension(output, Path.GetExtension(source));
                if (File.Exists(target)) throw new IOException("Refusing to overwrite an existing staged motorcycle player sidecar.");
            }
            foreach (var source in temporaryFiles)
            {
                var target = Path.ChangeExtension(output, Path.GetExtension(source));
                File.Move(source, target);
                committed.Add(target);
            }

            var final = new UAsset(output, engineVersion, mappings);
            ValidatePatchedComponent(originalSnapshot, ResolveComponent(final, output));
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                virtualPackage = request.VirtualPackage,
                outputFiles = committed,
                patchedFlags = Array.Empty<string>(),
                patchedMountGate = true,
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


    // Find the player component

    private static NormalExport ResolveComponent(UAsset asset, string input)
    {
        var matches = asset.Exports.Select((export, index) => (export, index))
            .Where(item => String.Equals(item.export.ObjectName.ToString(), ComponentName, StringComparison.Ordinal))
            .ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"Expected exactly one {ComponentName} export; found {matches.Length}.");
        if (matches[0].export is not NormalExport)
        {
            using var reader = asset.PathToReader(input, true);
            asset.ParseExport(reader, matches[0].index, true);
        }
        return asset.Exports[matches[0].index] as NormalExport
            ?? throw new InvalidDataException($"{ComponentName} did not parse as a normal Blueprint export.");
    }

    private static (StructPropertyData Condition, StrPropertyData Formula) ResolveFormula(NormalExport component)
    {
        var conditions = component.Data.OfType<StructPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), ConditionName, StringComparison.Ordinal))
            .ToArray();
        if (conditions.Length != 1)
            throw new InvalidDataException($"{ComponentName} must contain exactly one {ConditionName} struct.");
        var formulas = conditions[0].Value.OfType<StrPropertyData>()
            .Where(property => String.Equals(property.Name.ToString(), FormulaName, StringComparison.Ordinal))
            .ToArray();
        if (formulas.Length != 1)
            throw new InvalidDataException($"{FormulaPropertyPath} must contain exactly one string property.");
        return (conditions[0], formulas[0]);
    }


    // Capture component state

    private static string SnapshotComponent(NormalExport component) => JsonSerializer.Serialize(
        component.Data.Select(property => SnapshotProperty(property, ComponentName + "." + property.Name)).ToArray());


    // Capture serialized property state

    private static object SnapshotProperty(PropertyData property, string path)
    {
        if (property is StructPropertyData structure)
            return new
            {
                name = property.Name.ToString(),
                type = property.GetType().FullName,
                value = structure.Value.Select(child => SnapshotProperty(child, path + "." + child.Name)).ToArray(),
            };
        if (property is ArrayPropertyData array)
            return new
            {
                name = property.Name.ToString(),
                type = property.GetType().FullName,
                value = array.Value.OfType<PropertyData>().Select((child, index) =>
                    SnapshotProperty(child, $"{path}[{index}]")).ToArray(),
            };

        var value = property.GetType().GetProperty("Value", BindingFlags.Instance | BindingFlags.Public)?.GetValue(property);
        var serialized = path == FormulaPropertyPath ? "<motorcycle-availability-formula>" : Canonicalize(value);
        return new { name = property.Name.ToString(), type = property.GetType().FullName, value = serialized };
    }


    // Normalize serialized data

    private static object? Canonicalize(object? value)
    {
        if (value is null) return null;
        if (value is FName or FString) return value.ToString();
        if (value is Enum enumValue) return enumValue.ToString();
        if (value is string or bool or byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal)
            return value;
        if (value is IEnumerable enumerable)
            return enumerable.Cast<object?>().Select(Canonicalize).ToArray();
        return value.ToString() ?? value.GetType().FullName;
    }


    // Check the patched player component

    private static void ValidatePatchedComponent(string expectedSnapshot, NormalExport component)
    {
        var (_, formula) = ResolveFormula(component);
        if (!String.Equals(formula.Value.ToString(), UnlockedFormula, StringComparison.Ordinal))
            throw new InvalidDataException("Motorcycle mount availability did not serialize as unlocked.");
        if (!String.Equals(expectedSnapshot, SnapshotComponent(component), StringComparison.Ordinal))
            throw new InvalidDataException("Motorcycle mount component changed outside its availability formula.");
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
