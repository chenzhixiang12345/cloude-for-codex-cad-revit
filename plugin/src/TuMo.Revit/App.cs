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
        panel.AddItem(new PushButtonData("TuMo.MergeFloors", "从 DXF\n合并楼层",
            Assembly.GetExecutingAssembly().Location, typeof(MergeFloorsCommand).FullName!)
        {
            ToolTip = "按楼层表和共有轴网对齐同一建筑 DXF 内的多张平面图。"
        });
        panel.AddItem(new PushButtonData("TuMo.MergeStructuralSlabs", "从 DXF\n合并结构降板",
            Assembly.GetExecutingAssembly().Location, typeof(MergeStructuralSlabsCommand).FullName!)
        {
            ToolTip = "选择建筑平面和结构顶板图，按共有轴网与楼层表合并有证据的结构降板。"
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
