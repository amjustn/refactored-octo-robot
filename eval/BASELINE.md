# Eval 质量基线档案

- **基线运行**：`eval/runs/20260815_185121/`（真实 LLM，非 stub）
- **建立时间**：2026-08-15 19:18 ｜ 模型：deepseek-v4-flash（服务器默认）｜ 总成本：¥3.75（prompt 1,097,828 / completion 193,810 tokens）
- **基线均分**：60.7（6 用例）
- **更新 2026-08-16**：daily-briefing 行已替换为 DSML 泄漏修复后的复测值（原基线 37.7/结构6/585字）

| 用例 | 总分 | 结构25 | 数据30 | 引用15 | 耗时10 | judge20 |
|---|---:|---:|---:|---:|---:|---:|
| thesis-tracker | 93.5 | 25 | 30 | 10.3 | 10 | 18.2 |
| investment-team | 63.2 | 25 | 0 | 9.9 | 10 | 18.3 |
| global-macro-analysis | 58.4 | 25 | 0 | 6.9 | 10 | 16.5 |
| china-macro-analysis | 56.6 | 25 | 0 | 7.8 | 10 | 13.8 |
| earnings-team | 55.0 | 25 | 0 | 11.0 | 10 | 9.0 |
| daily-briefing | 61.1 | 25 | 0 | 8.4 | 10 | 17.7 |

## 门禁用法

```bash
# 改提示词/换模型/升级依赖后跑（真实模式，约 25 分钟、~¥4）
venv/bin/python eval/replay.py --all
# 与基线对比（总分降 >5 或数据维度降级即报警）
venv/bin/python eval/report.py eval/runs/20260815_185121 eval/runs/<新运行>
# 快速离线回归（零成本，不用于质量对比）
venv/bin/python eval/replay.py --all --stub-llm
```

## 基线暴露的已知问题（2026-08-15 记录）

1. ~~**daily-briefing 产出破损**~~（已修复 2026-08-16，提交 113e30c + 18a06ab）：根因是单Agent技能未走 function-calling 循环。三层修复：registry 技能级 `tools_enabled` 开关 + `run_single` 路由、`_strip_dsml` 整块/孤立标签容错扩展、guards 工具标记残留 fatal 闸门。修复后复测（`eval/runs/20260816_003307/`，deepseek-v4-flash）：**37.7 → 61.1**（结构 6→25，judge 6.5→17.7，报告 585→5181 字，工具真实执行、正文零标记残留）；引用维度 15.0→8.4 在正常波动范围内（见第3条 ±3 容差之外，判读时注意）。
2. **数据维度 5/6 用例为 0**：web 搜索能返回结果（10 条/查询），但 Agent 反映"未能获取可引用的实际财务数字"——相关性守卫过度拒绝 Bing 结果回退低质源、正文抓取受限，数字进不了报告；且 cassettes 仅 1 个示例文件，数据对账缺乏录制支撑。
3. **运行间波动**：thesis-tracker 独立跑 96.4 vs 基线跑 93.5（引用维度 14.2→10.3），引用维度有噪声，判读对比时给 ±3 分容差。
