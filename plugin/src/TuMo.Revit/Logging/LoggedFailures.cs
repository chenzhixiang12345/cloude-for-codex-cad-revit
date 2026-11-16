using Autodesk.Revit.DB;

namespace TuMo.Revit.Logging;

internal sealed class LoggedFailures : IFailuresPreprocessor
{
    private readonly RunLog log;

    public LoggedFailures(RunLog log) => this.log = log;

    public FailureProcessingResult PreprocessFailures(FailuresAccessor accessor)
    {
        var hasError = false;
        foreach (var entry in accessor.GetFailureMessages())
        {
            log.Write($"Revit {entry.GetSeverity()}，{entry.GetDescriptionText()}");
            if (entry.GetSeverity() == FailureSeverity.Warning)
                accessor.DeleteWarning(entry);
            else
                hasError = true;
        }
        return hasError ? FailureProcessingResult.ProceedWithRollBack : FailureProcessingResult.Continue;
    }
}
