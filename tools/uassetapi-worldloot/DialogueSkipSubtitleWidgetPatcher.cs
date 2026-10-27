using System.Text.Json;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.Kismet.Bytecode;
using UAssetAPI.Kismet.Bytecode.Expressions;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;

internal static class DialogueSkipSubtitleWidgetPatcher
{
    private const string PackagePath = "/Game/UI/Subtitles/WDT_Subtitles";
    private const string SetterName = "SetSkipIndicatorPercent_Interact";
    private const string CloseName = "OnForceClose";
    private const string UbergraphName = "ExecuteUbergraph_WDT_Subtitles";
    private const float NativeDebounceSeconds = 0.5f;

    // Patch the configured Interact progress callback
    public static int Patch(string[] args)
    {
        var committed = new List<string>();
        string? temporaryHeader = null;
        try
        {
            var root = Path.GetFullPath(args[1]);
            var input = Path.GetFullPath(args[2]);
            var output = Path.GetFullPath(args[3]);
            var mappings = new Usmap(Path.GetFullPath(args[4]));
            var engineVersion = Enum.Parse<EngineVersion>(args[5], ignoreCase: false);
            if (!IsWithin(input, root) || !IsWithin(output, root) || input == output ||
                Path.GetFileNameWithoutExtension(input) != "WDT_Subtitles" ||
                Path.GetFileNameWithoutExtension(output) != "WDT_Subtitles" ||
                !File.Exists(input) || File.Exists(output))
                throw new InvalidDataException("The subtitle widget paths do not match the audited asset.");

            var asset = new UAsset(input, engineVersion, mappings);
            var setter = FindUniqueExport<StructExport>(asset, SetterName);
            var setterOriginal = setter.ScriptBytecode ?? Array.Empty<KismetExpression>();
            if (setterOriginal.Length != 4 ||
                setterOriginal[0] is not EX_LetValueOnPersistentFrame
                {
                    AssignmentExpression: EX_LocalVariable { Variable: var newPercentPointer },
                } ||
                newPercentPointer.New is null || !FieldNameEquals(newPercentPointer, "NewPercent") ||
                setterOriginal[1] is not EX_LocalFinalFunction updateCall ||
                updateCall.Parameters.Length != 1 || updateCall.Parameters[0] is not EX_IntConst { Value: 2530 } ||
                setter.ScriptBytecodeSize == 0)
                return Unsupported($"The Interact progress callback has an unsupported layout " +
                    $"(expressions={setterOriginal.Length}; types={string.Join(',', setterOriginal.Select(item => item.GetType().Name))}).");

            var close = FindUniqueExport<StructExport>(asset, CloseName);
            var closeOriginal = close.ScriptBytecode ?? Array.Empty<KismetExpression>();
            if (closeOriginal.Length != 3 || closeOriginal[0] is not EX_LocalFinalFunction closeCall ||
                closeCall.Parameters.Length != 1 || closeCall.Parameters[0] is not EX_IntConst { Value: 1225 } ||
                closeOriginal[1] is not EX_Return || close.ScriptBytecodeSize == 0)
                return Unsupported("The force-close event has an unsupported layout.");

            var ubergraph = FindUniqueExport<StructExport>(asset, UbergraphName);
            var graph = ubergraph.ScriptBytecode;
            if (graph is null || graph.Length != 120 ||
                graph[4] is not EX_LetBool { VariableExpression: EX_InstanceVariable { Variable: var delayPointer } } ||
                delayPointer.New is null || !FieldNameEquals(delayPointer, "EndSkip_Delay") ||
                graph[53] is not EX_LetBool
                {
                    AssignmentExpression: EX_Context
                    {
                        ObjectExpression: EX_InstanceVariable { Variable: var subtitlesPointer },
                    },
                } || subtitlesPointer.New is null || !FieldNameEquals(subtitlesPointer, "SubtitlesUIObject"))
                return Unsupported("The audited subtitle skip graph has an unsupported layout.");

            var greater = FindOrAddMathImport(asset, "Greater_DoubleDouble");
            var notBool = FindImport(asset, "Not_PreBool");
            var prohibited = FindImport(asset, "IsInteractSkipProhibited");
            var skipStatement = FindImport(asset, "SkipStatement");
            var timer = FindImport(asset, "Delay");
            if (graph[53] is not EX_LetBool
                {
                    AssignmentExpression: EX_Context
                    {
                        ObjectExpression: EX_InstanceVariable { Variable: var prohibitedObject },
                        ContextExpression: EX_FinalFunction { StackNode: var prohibitedFunction, Parameters.Length: 0 },
                    },
                } || !FieldNameEquals(prohibitedObject, "SubtitlesUIObject") ||
                prohibitedFunction.Index != FPackageIndex.FromImport(prohibited).Index ||
                graph[56] is not EX_LetBool
                {
                    AssignmentExpression: EX_Context
                    {
                        ObjectExpression: EX_InstanceVariable { Variable: var skipObject },
                        ContextExpression: EX_FinalFunction { StackNode: var skipCall, Parameters.Length: 0 },
                    },
                } || !FieldNameEquals(skipObject, "SubtitlesUIObject") ||
                skipCall.Index != FPackageIndex.FromImport(skipStatement).Index)
                return Unsupported("The subtitle widget no longer has the audited skip guards.");
            var timerCalls = graph.OfType<EX_CallMath>()
                .Where(call => call.StackNode.Index == FPackageIndex.FromImport(timer).Index &&
                    call.Parameters.Length == 3 &&
                    call.Parameters[1] is EX_FloatConst { Value: NativeDebounceSeconds })
                .ToArray();
            if (timerCalls.Length != 1 || graph[58] != timerCalls[0] ||
                timerCalls[0].Parameters[1] is not EX_FloatConst)
                return Unsupported($"The subtitle skip-delay timer has an unsupported layout " +
                    $"(matches={timerCalls.Length}; calls={string.Join(';', graph.OfType<EX_CallMath>().Select(call => $"{call.StackNode.Index}/{call.Parameters.Length}/{(call.Parameters.Length > 1 ? call.Parameters[1].GetType().Name : "none")}"))}).");

            var percentPositive = BuildComparison(asset, greater, newPercentPointer, 0.0);
            var ready = new EX_InstanceVariable { Variable = new KismetPropertyPointer(delayPointer.New) };
            var prohibitedCall = BuildProhibitionCheck(subtitlesPointer, prohibited);
            var allowed = new EX_CallMath
            {
                StackNode = FPackageIndex.FromImport(notBool),
                Parameters = [prohibitedCall],
            };
            var jumpOnNonPositive = new EX_JumpIfNot { BooleanExpression = percentPositive };
            var jumpIfAlreadyHandled = new EX_JumpIfNot { BooleanExpression = ready };
            var jumpIfProhibited = new EX_JumpIfNot { BooleanExpression = allowed };
            var closeOnFirstProgress = new EX_LocalVirtualFunction
            {
                VirtualFunctionName = new FName(asset, CloseName),
                Parameters = [],
            };
            var latchAfterSkip = SetDelayFlag(delayPointer, false);
            var jumpToOriginalAfterSkip = new EX_Jump();
            var resetDelayOnRelease = SetDelayFlag(delayPointer, true);
            var jumpToOriginalAfterReset = new EX_Jump();
            KismetExpression[] prefix =
            {
                jumpOnNonPositive,
                jumpIfAlreadyHandled,
                jumpIfProhibited,
                closeOnFirstProgress,
                latchAfterSkip,
                jumpToOriginalAfterSkip,
                resetDelayOnRelease,
                jumpToOriginalAfterReset,
            };
            var prefixSizes = prefix.Select(expression => expression.GetSize(asset)).ToArray();
            var resetOffset = checked(prefixSizes.Take(6).Aggregate(0u, (sum, size) => sum + size));
            var originalOffset = checked(prefixSizes.Aggregate(0u, (sum, size) => sum + size));
            jumpOnNonPositive.CodeOffset = resetOffset;
            jumpIfAlreadyHandled.CodeOffset = originalOffset;
            jumpIfProhibited.CodeOffset = originalOffset;
            jumpToOriginalAfterSkip.CodeOffset = originalOffset;
            jumpToOriginalAfterReset.CodeOffset = originalOffset;
            setter.ScriptBytecode = [.. prefix, .. setterOriginal];

            var outputDirectory = Path.GetDirectoryName(output)!;
            Directory.CreateDirectory(outputDirectory);
            temporaryHeader = Path.Combine(outputDirectory,
                Path.GetFileNameWithoutExtension(output) + ".cv2-" + Guid.NewGuid().ToString("N") + ".uasset");
            asset.Write(temporaryHeader);
            CopySidecars(input, temporaryHeader);
            ValidatePatchedPackage(temporaryHeader, engineVersion, mappings, greater, notBool, prohibited,
                delayPointer, resetOffset, originalOffset, NativeDebounceSeconds);

            var temporaryFiles = PackageFiles(temporaryHeader).Where(File.Exists).ToArray();
            if (temporaryFiles.Length == 0 || temporaryFiles.Any(file => new FileInfo(file).Length <= 0))
                throw new IOException("The patched subtitle widget contains an empty package file.");
            var outputFiles = temporaryFiles.Select(file => Path.ChangeExtension(output, Path.GetExtension(file))).ToArray();
            if (outputFiles.Any(File.Exists)) throw new IOException("Refusing to overwrite a subtitle widget output file.");
            for (var index = 0; index < temporaryFiles.Length; index++)
            {
                File.Move(temporaryFiles[index], outputFiles[index]);
                committed.Add(outputFiles[index]);
            }
            ValidatePatchedPackage(output, engineVersion, mappings, greater, notBool, prohibited,
                delayPointer, resetOffset, originalOffset, NativeDebounceSeconds);
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                status = "patched",
                virtualPackage = PackagePath,
                inputSource = "configured Interact hold-progress",
                activationPoint = $"first positive {SetterName} progress",
                skipGuard = "IsInteractSkipProhibited",
                outputAction = "OnForceClose -> SkipStatement",
                closeHandler = "unchanged",
                inputRecovery = "native 0.5-second debounce and zero-progress reset; prohibited progress does not latch",
                outputFiles = committed,
                reopenedAndValidated = true,
            }));
            return 0;
        }
        catch (InvalidDataException error)
        {
            foreach (var file in committed) TryDelete(file);
            Console.WriteLine(JsonSerializer.Serialize(new { status = "unsupported", reason = error.Message }));
            return 0;
        }
        catch (Exception error)
        {
            foreach (var file in committed) TryDelete(file);
            Console.Error.WriteLine(error);
            return 1;
        }
        finally
        {
            if (temporaryHeader is not null)
                foreach (var file in PackageFiles(temporaryHeader)) TryDelete(file);
        }
    }

    // Compare progress with a threshold
    private static EX_CallMath BuildComparison(
        UAsset asset,
        int functionImport,
        KismetPropertyPointer percentPointer,
        double threshold) => new()
    {
        StackNode = FPackageIndex.FromImport(functionImport),
        Parameters =
        [
            new EX_PrimitiveCast
            {
                ConversionType = (ECastToken)4,
                Target = new EX_LocalVariable { Variable = new KismetPropertyPointer(percentPointer.New) },
            },
            new EX_DoubleConst { Value = threshold },
        ],
    };

    // Set the skip latch
    private static EX_LetBool SetDelayFlag(KismetPropertyPointer delayPointer, bool value) => new()
    {
        VariableExpression = new EX_InstanceVariable { Variable = new KismetPropertyPointer(delayPointer.New!) },
        AssignmentExpression = value ? new EX_True() : new EX_False(),
    };

    // Read the native skip permission
    private static EX_Context BuildProhibitionCheck(KismetPropertyPointer subtitlesPointer, int prohibited) => new()
    {
        ObjectExpression = new EX_InstanceVariable { Variable = new KismetPropertyPointer(subtitlesPointer.New) },
        Offset = 10,
        PropertyType = 0,
        RValuePointer = new KismetPropertyPointer(new FFieldPath(Array.Empty<FName>(), new FPackageIndex(0), 0)),
        ContextExpression = new EX_FinalFunction
        {
            StackNode = FPackageIndex.FromImport(prohibited),
            Parameters = [],
        },
    };

    // Validate the reopened widget package
    private static void ValidatePatchedPackage(
        string path,
        EngineVersion engineVersion,
        Usmap mappings,
        int greater,
        int notBool,
        int prohibited,
        KismetPropertyPointer delayPointer,
        uint resetOffset,
        uint originalOffset,
        float delaySeconds)
    {
        var reopened = new UAsset(path, engineVersion, mappings);
        var setter = FindUniqueExport<StructExport>(reopened, SetterName).ScriptBytecode;
        var close = FindUniqueExport<StructExport>(reopened, CloseName).ScriptBytecode;
        var graph = FindUniqueExport<StructExport>(reopened, UbergraphName).ScriptBytecode;
        var validSetter = setter is not null && setter.Length == 12 &&
            setter[0] is EX_JumpIfNot { CodeOffset: var resetTarget, BooleanExpression: EX_CallMath positive } &&
            resetTarget == resetOffset && positive.StackNode.Index == FPackageIndex.FromImport(greater).Index &&
            setter[1] is EX_JumpIfNot { CodeOffset: var readyTarget, BooleanExpression: EX_InstanceVariable ready } &&
            readyTarget == originalOffset && FieldNameEquals(ready.Variable, "EndSkip_Delay") &&
            setter[2] is EX_JumpIfNot { CodeOffset: var prohibitedTarget, BooleanExpression: EX_CallMath permitted } &&
            prohibitedTarget == originalOffset && permitted.StackNode.Index == FPackageIndex.FromImport(notBool).Index &&
            permitted.Parameters.Length == 1 && permitted.Parameters[0] is EX_Context blocked &&
            IsFunctionCall(blocked, prohibited) && setter[3] is EX_LocalVirtualFunction { Parameters.Length: 0 } closeCall &&
            closeCall.VirtualFunctionName.ToString() == CloseName &&
            setter[4] is EX_LetBool { AssignmentExpression: EX_False, VariableExpression: EX_InstanceVariable skipLatch } &&
            FieldNameEquals(skipLatch.Variable, "EndSkip_Delay") &&
            setter[5] is EX_Jump { CodeOffset: var skipExit } && skipExit == originalOffset &&
            setter[6] is EX_LetBool { AssignmentExpression: EX_True, VariableExpression: EX_InstanceVariable resetFlag } &&
            FieldNameEquals(resetFlag.Variable, "EndSkip_Delay") &&
            setter[7] is EX_Jump { CodeOffset: var resetExit } && resetExit == originalOffset &&
            setter[8] is EX_LetValueOnPersistentFrame &&
            setter[9] is EX_LocalFinalFunction { Parameters.Length: 1 } originalUpdate &&
            originalUpdate.Parameters[0] is EX_IntConst { Value: 2530 };
        var validClose = close is not null && close.Length == 3 &&
            close[0] is EX_LocalFinalFunction { Parameters.Length: 1 } originalClose &&
            originalClose.Parameters[0] is EX_IntConst { Value: 1225 } &&
            close[1] is EX_Return;
        var validGraph = graph is not null && graph.Length == 120 &&
            graph.OfType<EX_CallMath>().SingleOrDefault(call => call.Parameters.Length == 3 &&
                call.Parameters[1] is EX_FloatConst duration && duration.Value == delaySeconds &&
                duration.Value == NativeDebounceSeconds) is not null;
        if (!validSetter || !validClose || !validGraph || new FileInfo(Path.ChangeExtension(path, ".uexp")).Length <= 0)
            throw new InvalidDataException($"The reopened subtitle widget failed validation " +
                $"(setter={validSetter}; close={validClose}; graph={validGraph}; " +
                $"uexp={File.Exists(Path.ChangeExtension(path, ".uexp"))}).");
    }

    // Check one prohibited-state call
    private static bool IsFunctionCall(EX_Context expression, int functionImport) =>
        expression.ContextExpression is EX_FinalFunction call &&
        call.StackNode.Index == FPackageIndex.FromImport(functionImport).Index && call.Parameters.Length == 0;

    // Find one export by name
    private static T FindUniqueExport<T>(UAsset asset, string name) where T : Export
    {
        var matches = asset.Exports.OfType<T>().Where(export => export.ObjectName.ToString() == name).ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"The subtitle widget export {name} does not match the audited asset.");
        return matches[0];
    }

    // Find one math function import
    private static int FindOrAddMathImport(UAsset asset, string name)
    {
        var mathClass = asset.Imports.Select((item, index) => (item, index))
            .Where(pair => pair.item.ObjectName.ToString() == "KismetMathLibrary" &&
                pair.item.ClassPackage.ToString() == "/Script/CoreUObject" &&
                pair.item.ClassName.ToString() == "Class")
            .Select(pair => pair.index).SingleOrDefault(-1);
        if (mathClass < 0)
        {
            var engine = FindOrAddImport(asset, "/Script/Engine", "/Script/CoreUObject", "Package", 0);
            mathClass = FindOrAddImport(asset, "KismetMathLibrary", "/Script/CoreUObject", "Class",
                FPackageIndex.FromImport(engine).Index);
        }
        return FindOrAddImport(asset, name, "/Script/CoreUObject", "Object",
            FPackageIndex.FromImport(mathClass).Index);
    }

    // Find one existing import
    private static int FindImport(UAsset asset, string name)
    {
        var matches = asset.Imports.Select((item, index) => (item, index))
            .Where(pair => pair.item.ObjectName.ToString() == name)
            .Select(pair => pair.index).ToArray();
        if (matches.Length != 1)
            throw new InvalidDataException($"The subtitle widget import {name} is missing or ambiguous.");
        return matches[0];
    }

    // Ensure one script package import
    private static int FindOrAddImport(
        UAsset asset,
        string name,
        string classPackage,
        string className,
        int outerIndex)
    {
        var matches = asset.Imports.Select((item, index) => (item, index))
            .Where(pair => pair.item.ObjectName.ToString() == name &&
                pair.item.ClassPackage.ToString() == classPackage &&
                pair.item.ClassName.ToString() == className && pair.item.OuterIndex.Index == outerIndex)
            .Select(pair => pair.index).ToArray();
        if (matches.Length > 1)
            throw new InvalidDataException($"The subtitle widget import {name} is ambiguous.");
        if (matches.Length == 1) return matches[0];
        var index = asset.Imports.Count;
        asset.Imports.Add(new Import(new FName(asset, classPackage), new FName(asset, className),
            new FPackageIndex(outerIndex), new FName(asset, name), false));
        return index;
    }

    // Compare a field path name
    private static bool FieldNameEquals(KismetPropertyPointer pointer, string name) =>
        pointer.New is not null && pointer.New.Path.Length == 1 && pointer.New.Path[0].ToString() == name;

    // Return unsupported layout result
    private static int Unsupported(string reason)
    {
        Console.WriteLine(JsonSerializer.Serialize(new { status = "unsupported", reason }));
        return 0;
    }

    // Copy untouched package sidecars
    private static void CopySidecars(string input, string temporaryHeader)
    {
        foreach (var source in PackageFiles(input).Where(File.Exists))
        {
            if (String.Equals(source, input, StringComparison.Ordinal)) continue;
            var target = Path.ChangeExtension(temporaryHeader, Path.GetExtension(source));
            if (!File.Exists(target)) File.Copy(source, target);
        }
    }

    // List package sidecars
    private static IEnumerable<string> PackageFiles(string header)
    {
        yield return header;
        foreach (var extension in new[] { ".uexp", ".ubulk", ".uptnl" })
            yield return Path.ChangeExtension(header, extension);
    }

    // Check path ownership
    private static bool IsWithin(string path, string root)
    {
        var relative = Path.GetRelativePath(Path.GetFullPath(root), Path.GetFullPath(path));
        return relative != "." && !Path.IsPathRooted(relative) && relative != ".." &&
            !relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    }

    // Remove temporary outputs
    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch (IOException) { }
    }
}
