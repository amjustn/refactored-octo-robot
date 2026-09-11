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

---

# 复核回应（2026-09-11，全部为实测值，非口头）

> 隔周复核：Q1~Q6 相关改动均已入库并跑通本地 CI。复核所用实测命令：
> - `bash scripts/check.sh` → 退出码 0，末行 ALL CHECKS PASSED（ruff 全过 / pytest 416 passed / 脚本测试 18+44 全过 / eval replay 140 cassette 全 HIT、6 用例 score=95.0 / 批C 注入层 15+18 全过），用时 1 分 04 秒
> - `venv/bin/python scripts/verify_ai_berkshire.py --base http://127.0.0.1:8001` → 39 项：PASS 38 / FAIL 0 / PENDING 1（pytest 回归未加 --with-pytest）
> - 同批提交 ea430ca：`data/decision-skip-log.jsonl` 加入 .gitignore（`git check-ignore` 命中 .gitignore:18；文件仍在磁盘，319 行 / 48,649 字节；跑完 check.sh 后工作区仍干净）

## Q1. `_archive/` 历史备份的范围与处置 → 采纳（untrack，保留磁盘）

- 处置：采纳建议的 untrack，未做物理删除。提交 6856198「untrack _archive historical backups (keep files on disk)」。
- 实测：`git ls-files _archive | wc -l` = 0；`ls _archive | wc -l` = 18（磁盘条目全在，可回看旧实现）。
- 同类：`app/main.py.bak_20260817`、`app/main.py.bak_20260908` 均未入库，留在磁盘（`git ls-files | grep -c bak` = 0）。

## Q2. `data/knowledge/*.json` 的 26 个改动不是垃圾 → 采纳（单独 commit，与重构隔离）

- 提交 e664c49「chore: 知识库自动更新快照（4 位大师 + 22 个技能）」，共 26 个文件，全部位于 `data/knowledge/`（非该目录文件数 = 0）。
- 与重构提交 7ef1f4c / 0f90bfd / d2bfa35 完全分离；未执行任何 `git checkout --` 还原。

## Q3. 8001 运行方式与重启验证 → 采纳（验证流程已落成脚本，不留口头）

- 运行方式确认：裸 uvicorn 进程，非 systemd（无 ai_berkshire unit）。当前 PID 324：`uvicorn app.main:app --host 127.0.0.1 --port 8001 --log-level info`；同批 `/home/q/berk_proxy.py` PID 325。
- 重启命令：根目录 `start.sh`（`cd ~/ai_berkshire_web && source venv/bin/activate && exec uvicorn app.main:app --host 0.0.0.0 --port 8001 --log-level info`），后台起 → 等端口就绪 → 再验证。
- 重启前检查：`data/tasks.db` running=0（completed 111 / cancelled 12 / interrupted 10 / failed 1），此刻重启不会打断进行中任务。
- 重启后确认（实测）：`scripts/verify_ai_berkshire.py --base http://127.0.0.1:8001` → 38 PASS / 0 FAIL（含 BasicAuth 两组合 401/200/401、生产未配 BasicAuth 时 `/app` HTTP 200、前端 10 个脚本 `node --check` 语法全过）。
- 实操坑（写进流程）：`/api/health` 走统一鉴权中间件，裸 curl 返回 401 Missing Authorization header —— 不能用未带凭据的 curl 判定服务死活，须用上述脚本或带 Authorization。
- 启动阶段后台任务留痕实测（logs/app.log 2026-09-11 20:17）：`数据源熔断器初始摘除: ['yahoo', 'eastmoney']`、`Circuit breakers seeded from startup health check: ['yahoo', 'eastmoney']`、`Decision-log verify cycle: backfill={'backfilled': 1, 'skipped': 0, 'retry_later': 0, 'failed': 0} verify={'verified': 1, 'unchanged': 24, 'skipped': 1}` —— 种子与决策日志验证在 lifespan 启动阶段确实执行。

## Q4. `.env` 密钥不得出现在汇报中 → 采纳（承诺成立且已核查）

- `.env` 未被 git 跟踪（`git check-ignore -v .env` → .gitignore 第 4 行）。
- 入库的 REVIEW_FINDINGS.md 做过密钥模式扫描（`sk-xxxx` / `api_key=` / `token=`），无命中；本文所有实测输出只含配置项结构与结构性日志，不含任何 secret 值。审查稿入库不含密钥内容。

## Q5. app.js（2880 行）拆分 → 采纳（先定边界、无构建、按序引入、分批验证）

- 模块边界（提交 0f90bfd）：core（共享状态 / helpers / toast，加载第 1）→ skills-nav → llm-config → research → render → report-actions → stats → init（唯一 DOMContentLoaded，最后），加既有 charts.js / skin.js，前端共 10 个业务脚本。
- 无构建：`templates/index.html` 按上述顺序共 13 个 script 标签引入（含 marked / echarts / purify 三个第三方），均带 `?v=YYYYMMDD` 缓存击穿参数；未引入任何 JS 工具链。
- 分批验证：每批后复跑自检脚本；终检 `verify_ai_berkshire.py` 前端静态资产 10 项全 PASS，S10「全部前端脚本语法通过 node --check」（node --check 仅作语法门，不作为构建链）失败=无。
- 全局引用完整性抽查：`window.name='berkshire_deeplink'`（与 8000 仪表盘复用标签页）已迁至 core.js；research.js 保留 `_batchFallback`；无遗漏。

## Q6. 装配期两个已知坑 → 均已纳入并通过回归门

- 坑1（路由注册方式）：未改动，仍为 `app.router.routes.extend(...)`，未引入 `include_router`；pytest 416 passed，依赖 `app.main` re-export 与 routes 列表的用例未受影响。此条对原计划做了调整：路由装配不再作为重构项。
- 坑2（lifespan 与 TestClient）：提交 d2bfa35 将 `@app.on_event("startup")` 整体迁移至 lifespan，删除 on_event 防双跑，并新增启动回归门 `tests/test_lifespan_startup.py`（+42 行，使用 `with TestClient(app)` context manager 触发 lifespan，覆盖幂等与 7 个后台任务等价保留）；全量 `pytest tests/` 416 passed 为总回归门。

## 复核新增的有价值项（1 条，待裁定）

### N1. `static/js/app.js` 已是死代码，但仍被 git 跟踪

- 实测：2880 行 / 118,935 字节；`templates/*.html` 的 13 个 script 标签已不含它；`scripts/verify_ai_berkshire.py` 的 LOADED_JS 白名单（10 个）也不含它；全仓仅剩拆分模块注释与本文提到它（`grep -rn "app\.js"` 无任何加载/引用点）。
- 风险：真实前端入口是 core→…→init 十模块，留着一份 118KB 的旧件会让后来者误判入口，且与拆分前内容重复。
- 建议（未执行，等裁定）：按 Q1 同法 `git rm --cached static/js/app.js`（保留磁盘，随时可回滚）；若确认无人回看，物理删除亦可，拆分前实现可由提交 0f90bfd 从 git 历史取回。
