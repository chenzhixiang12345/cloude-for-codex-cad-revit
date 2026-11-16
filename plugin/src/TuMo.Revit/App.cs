using System;
using System.Linq;
using System.Reflection;
using Autodesk.Revit.UI;
using TuMo.Revit.Commands;

namespace TuMo.Revit;

public sealed class App : IExternalApplication
{
    public Result OnStartup(UIControlledApplication application)
    {
        const string tab = "TuMo";
        try
        {
            application.CreateRibbonTab(tab);
        }
        catch (Autodesk.Revit.Exceptions.ArgumentException)
        {
            // Revit rejects a second tab with the same name.
        }

        const string panelName = "图模一致";
        var panel = application.GetRibbonPanels(tab).FirstOrDefault(p => p.Name == panelName)
            ?? application.CreateRibbonPanel(tab, panelName);
        panel.AddItem(new PushButtonData("TuMo.Generate", "从 DXF\n生成模型",
            Assembly.GetExecutingAssembly().Location, typeof(GenerateCommand).FullName!)
        {
            ToolTip = "识别一张 DXF 平面图并生成标高、轴网、柱和墙。"
        });
        return Result.Succeeded;
    }

    public Result OnShutdown(UIControlledApplication application) => Result.Succeeded;
}
