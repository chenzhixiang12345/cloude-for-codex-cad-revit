# CLAUDE.md：图模一致（TuMo）

从建筑和结构 CAD 施工图自动识别，在 Revit 里生成 LOD350 模型。完整计划见 `DEV_PLAN.md`。

## 自主工作模式（最重要）

用户不希望"做一步、停一步"。**除了下面 4 种情况，不要停下来问用户，不要等确认，连续工作，直到当前阶段完成。**

只有这些情况可以停：
1. **到达测试点**：阶段 2、3、4、5 结束时。停之前准备好安装包和中文测试清单。
2. **缺少只有用户能提供的东西**：图纸、账号授权、安装软件。先把不依赖它的工作全部做完，再停下来一次性列出需要什么。
3. **要删除或覆盖用户的数据**：比如删除样例图纸、改写仓库历史。
4. **对话快到上限**：先更新 `PROGRESS.md` 并提交推送，然后只说一句"请说'继续'"。

其他情况的处理：
- **需求细节不清楚**：选最合理的默认方案，写进 `DECISIONS.md` 的"待确认"列表，继续工作。到测试点时统一问用户。
- **一个问题尝试 3 次还没解决**：写进 `PROGRESS.md` 的"遗留问题"，先做下一个任务。
- **不要逐个任务汇报**：阶段结束时给一份简短的中文阶段报告就够了。

## 必须遵守

- **所有回复都用简体中文，句子短、用词简单。** 用户用翻译器阅读，同时在学中文。
- **每次会话开始**：读 `DEV_PLAN.md`、`PROGRESS.md`、`DECISIONS.md`，然后运行测试和评分。
- **每完成一个任务**，更新 `PROGRESS.md`，提交本次代码和开发记录，推送到 GitHub 的 `main`，并核对远程分支已收到提交。
- **每次会话结束**，在 `PROGRESS.md` 记下已做工作、评分、下一步和遗留问题。按上述规则备份本次会话的剩余变更。
- 准确率和速度的数字只能来自 `eval/score.py` 的实际运行结果，不要估计。
- 改识别逻辑之前，先把出错的例子写成测试。
- 样例图纸、样例标准答案和识别产物只保存在本地。见 `DECISIONS.md` 的 D-005。

## 常用命令

```bash
# 识别引擎
cd engine && pip install -e ".[dev]"
python -m tumo_engine.cli analyze samples/<名>/arch/<图>.dxf      # 图层分析报告
python -m tumo_engine.cli run samples/<名> -o out/                 # 识别，输出 model.json + issues.json
python -m tumo_engine.cli render out/model.json -o out/viz/        # 识别结果图
pytest
python -m eval.score --all                                          # 评分

# Revit 插件（Windows）
dotnet build plugin/TuMo.Revit.sln -c Release
```

## 架构要点

- 识别逻辑全部放在 Python 引擎里，插件保持"薄"。
- 引擎和插件之间只通过 `schema/tumo-model.schema.json` 定义的 JSON 交换数据，单位为 mm。
- 插件支持 Revit 2020～2027。跨版本有差异的 API 只能在 `plugin/src/TuMo.Revit/Compat/` 里调用。族和项目样板用 Revit 2020 格式保存。
- 每个构件都有稳定 ID（如 `W-0012`），结果图、报告、Revit 参数 `TuMo_ID` 里用同一个 ID。
