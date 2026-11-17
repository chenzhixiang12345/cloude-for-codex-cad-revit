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
            ToolTip = "识别一张 DXF 平面图并生成一个楼层的构件。"
        });
        panel.AddItem(new PushButtonData("TuMo.GenerateStandardFloors", "从 DXF\n生成标准层",
            Assembly.GetExecutingAssembly().Location, typeof(GenerateStandardFloorsCommand).FullName!)
        {
            ToolTip = "按 DXF 图名标明的楼层范围及楼层表生成多个标准层。"
        });
        panel.AddItem(new PushButtonData("TuMo.Review", "从 DXF\n审图",
            Assembly.GetExecutingAssembly().Location, typeof(ReviewCommand).FullName!)
        {
            ToolTip = "在独立绘图视图中导入 CAD 底图，并叠加带编号的识别结果。"
        });
        return Result.Succeeded;
    }

    public Result OnShutdown(UIControlledApplication application) => Result.Succeeded;
}
