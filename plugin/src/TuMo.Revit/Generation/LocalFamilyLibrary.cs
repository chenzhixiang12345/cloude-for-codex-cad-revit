using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Autodesk.Revit.ApplicationServices;
using TuMo.Revit.Logging;

namespace TuMo.Revit.Generation;

internal static class LocalFamilyLibrary
{
    public static string[] Find(Application application, string[] names, RunLog log)
    {
        var roots = new HashSet<string>(application.GetLibraryPaths().Values, StringComparer.OrdinalIgnoreCase)
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "Autodesk",
                $"RVT {application.VersionNumber}", "Libraries")
        };
        if (Directory.Exists(application.FamilyTemplatePath))
        {
            for (var directory = new DirectoryInfo(application.FamilyTemplatePath); directory != null; directory = directory.Parent)
            {
                if (!directory.Name.Equals("Family Templates", StringComparison.OrdinalIgnoreCase) || directory.Parent == null)
                    continue;
                roots.Add(Path.Combine(directory.Parent.FullName, "Libraries"));
                break;
            }
        }

        var paths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var root in roots.Where(Directory.Exists))
        {
            try
            {
                foreach (var path in Directory.EnumerateFiles(root, "*.rfa", SearchOption.AllDirectories))
                {
                    if (Preference(names, Path.GetFileNameWithoutExtension(path)) < names.Length)
                        paths.Add(path);
                }
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                log.Write($"无法搜索 Revit 族库 {root}：{error.Message}");
            }
        }
        return paths.OrderBy(path => Preference(names, Path.GetFileNameWithoutExtension(path)))
            .ThenBy(path => path, StringComparer.OrdinalIgnoreCase).ToArray();
    }

    public static int Preference(string[] names, string familyName)
    {
        var index = Array.FindIndex(names, name => string.Equals(name, familyName, StringComparison.OrdinalIgnoreCase));
        return index < 0 ? names.Length : index;
    }
}
