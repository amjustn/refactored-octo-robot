# REVIEW_FINDINGS — 外部监督评审（重构前）

> 来源：外部监督 AI（Hermes）对「代码架构分析与重构流程（清理git、迁移lifespan、拆分app.js）」任务的独立评审，基于对仓库的抽查（git 状态、运行进程、数据库、文件时间戳）+ 对你派发的 3 份只读子报告的复核。
> 定位：**仅供参考与核对，不构成强制指令**。请对每条给出明确回应：采纳 / 部分采纳 / 反驳（附理由），并落到你的步骤清单里。
> 生成时间：2026-09-03 20:57 CST

---

## 总体评价（先说对的）

- 先并行只读深审（harness / 后端 / 测试三路）+ 行号级引用，再汇报方案、等你确认开工顺序——方法正确，继续。
- 测试子报告专门厘清了"重构后如何验证"（TestClient 与 lifespan 的关系、无 JS 工具链），是正确铺垫。
- 启动钩子识别到位：`app/main.py:293-316` 的 `@app.on_event("startup")` 内 7 个 `asyncio.create_task` 后台任务（熔断种子、决策日志验证、清理旧报告/任务/缓存、auto_update_knowledge、auto_update_skill_knowledge）在 lifespan 迁移时必须等价保留，且注意幂等。

## 待回应问题（请逐条回应）

### Q1. "清理 git-tracked junk"的范围与处置方式未定义
- 现状：`_archive/` 下约 27 个被 git 跟踪的文件，包括 `main.bak.py`、`market_data.bak.py`、`demo_*.py`、`fix_*.py`、`*.websearch_bak.py` 等；另有 `app/main.py.bak_20260817`。
- 疑点：这些都是**历史备份**，不是可再生的垃圾。物理删除不可逆，一旦重构中途需要回看旧实现会损失参考。
- 请回应：计划是 untrack（`git rm --cached` 保留本地文件）还是物理删除？若物理删除，依据是什么（已确认无任何引用？git 历史足够？）？建议默认 untrack + 移动到一个明确的 `_archive/` 或直接不动，重构完成后由人工决定。

### Q2. `data/knowledge/*.json` 的 26 个未提交改动不是垃圾，别动
- 现状：`git status` 显示 26 个文件、±164 行；文件 mtime 为 **2026-09-01**（知识库运营更新，非本次任务产生）。
- 疑点：若被当成"工作区噪音"而 `git checkout --` 还原或塞进重构 commit，会丢失 9-01 的知识库内容更新。
- 请回应：计划如何处理？建议单独 commit（如 `chore: knowledge base snapshot 2026-09-01`），与重构改动完全隔离；不要还原、不要混入。

### Q3. 8001 运行方式与重启验证
- 现状：8001 由**裸 uvicorn 进程托管**（PID 334，`venv/bin/uvicorn app.main:app --port 8001`，20:33 启动），不是 systemd（`systemctl` 无 ai_berkshire unit）。同批启动的还有 `/home/q/berk_proxy.py`（PID 335）。
- 补充：`data/tasks.db` 当前**无 running 任务**（95 completed / 12 cancelled / 8 interrupted / 1 failed），此刻重启不会打断进行中任务。
- 疑点：lifespan 迁移完成后的验证流程是什么？谁来重启、用什么命令（保持与现进程一致的参数）、重启后如何确认（health 端点？页面加载？后台任务日志出现 seed/cleanup 记录）？请在步骤清单中写死，别让"验证"停留在口头。

### Q4. `.env` 已被两个子 agent 完整读取——汇报时严禁粘贴密钥
- 现状：harness 子审与后端子审都 read 过 `/home/q/ai_berkshire_web/.env` 全文（审查需要，合理）。
- 疑点：最终给用户的分析汇报、步骤清单若引用 .env 内容（API key、token 值），会泄漏到终端与日志。
- 请回应：承诺汇报中只讲配置项结构（哪些 key 存在、默认值来自哪），**不输出任何实际 secret 值**。

### Q5. `static/js/app.js`（2880 行）拆分——仓库零 JS 工具链，先定边界再动手
- 现状：已确认无 node/npm/eslint/jscpd 等任何 JS 工具；前端另有 `charts.js`、`skin.js`（已独立）。
- 疑点：vanilla JS 多文件拆分最大风险是全局函数互相引用与**初始化/事件绑定顺序**（DOMContentLoaded、loadState 等执行时机）。
- 请回应：拆分的模块边界与文件清单是什么？拆分后浏览器实测步骤是什么（建议：每拆一批 → 让用户在浏览器 Ctrl+F5 实测页面关键交互 → 再继续），而不是全部拆完一次性验证？是否保持无构建、script 标签按序引入？

### Q6. 改动 `app/main.py` 装配时已知的两个坑要写进计划
- 坑1：路由用 `app.router.routes.extend(...)`（`app/main.py:149-157`）而非 `include_router`——tests 侧有依赖 `app.main` re-export 与 routes 列表的用例，改动注册方式会连带破坏测试。
- 坑2：`@app.on_event("startup")` 里的 billing-column 迁移标记为 idempotent（`app/harness/persist.py`），迁移到 lifespan 后该迁移仍须在 lifespan 启动阶段执行一次，且 TestClient 只有作为 context manager（`with TestClient(app) as client`）才会触发 lifespan——测试若只 `TestClient(app)` 裸用，迁移/种子不会跑，可能造成测试环境差异。
- 请回应：这两点是否已纳入你的重构与回归计划？哪些测试作为 startup 改动的回归门？

---

## 期望输出格式

请按「Q1: [采纳/部分采纳/反驳] + 一句话理由 + 落到计划的动作」逐条回应，并给出更新后的步骤清单（标注哪些步骤因上述回应而调整）。
