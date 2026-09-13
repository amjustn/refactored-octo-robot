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

---

# 复核补充（2026-09-12）

## Q3 更正：8001 已改由 systemd --user 托管

- 上文 Q3 与 09-11 复核回复里记载的"**裸 uvicorn 进程、非 systemd、无 ai_berkshire unit、重启命令为 start.sh**"**均已过期**。
- 2026-09-12 实测：`systemctl --user status ai_berkshire` → active (running)、enabled、`Restart=always`、Main PID 155356；`ExecStart=/home/q/ai_berkshire_web/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001 --log-level info`。unit 由批F（2026-09-11 提交 7ef1f4c）带入仓库根目录，09-12 已安装并启用（`~/.config/systemd/user/ai_berkshire.service`，与仓库内副本内容一致）。
- **重启命令更正为**：`systemctl --user restart ai_berkshire`。
- 踩坑留痕：2026-09-12 按旧说法 kill 进程后手动跑 `start.sh`，与 systemd 的 `Restart=always` 抢端口（手动进程先占住 8001，systemd 拉起失败并重试；再次 kill 后 systemd 才成功接管）。`start.sh` 顶部已加注释，指明它只是手动入口以及正确重启命令。
- 同批实测补充：`/home/q/berk_proxy.py`（8099）实际绑定 `0.0.0.0:8099` 且无任何鉴权，与其自身文档"仅监听本机可访问地址"不符；它持有 `BERKSHIRE_API_TOKEN`，但只代理 `/price/{symbol}`、`/financials/{symbol}` 两个只读端点（不碰研究接口、不烧 LLM Key）。当前安全前提是 WSL2 NAT + Windows `netsh portproxy` 未转发 8099；要对外开放须先加请求头口令并复测 Dify 链路。
- 另：8001 的局域网可达性经实测收窄——`.wslconfig` 无 mirrored 网络设置（NAT 模式），`netsh portproxy` 全表只有 `0.0.0.0:8000` 一条转发，故 8001 仅本机与 Windows 宿主可达；"Basic Auth 未配置 + /app 注入 JWT"在本机面不增加攻击面，但一旦开远程访问即刻升级为 P0。
- 详见 Obsidian `项目/AI_Berkshire/AI_Berkshire_外部审查核验_20260912.md`。

---

# 数据时效缺口修复（2026-09-13 批1/批2/批3）

## 起因

桌面报告（管理层纵深研究，澜起科技）开篇写：

> 公司 **2025 年年报、2026 年半年报应已披露**，但具体数字与最新高管名单、持股变动需要核实

## 实测归因（不改动业务代码，纯测量）

口径：137 个历史任务、144 份报告、98656 行日志（2026-07-11 ~ 09-13）。

| # | 断点 | 实测证据 | 影响面 |
|---|------|----------|--------|
| 1 | 标的解析正则用 `\b` 边界，而 Python 里中文也算 `\w` | `re.search(r'\b(\d{6}...)\b', '688981分析一下是否可以买入')` → `None`；`'分析 688981 的财报'` → `'688981'` | 9/137 任务（6.6%）代码被漏解析 |
| 2 | 硬编码公司名表只有 143 条（A 股 71 条） | 沪深300 成分股按全名命中 **42/300 = 14%**；`_resolve_company_name('澜起科技')` → `None` | 表外公司名一律无数据 |
| 3 | 用户原话整句当搜索词 + 门闸把整句中文当一个 token | 原话 `澜起科技你给我一个深度报告 2026 最新` → 五源全部 0% 拒收、14.6s 后"无结果"；清洗成 `澜起科技` → 6 条、2.0s | 15/137 任务（10.9%）必然拒收；日志累计 764 次源拒绝、334 次无结果 |
| 4 | 兜底抓取"命中不足 3 条就全量注入" | 公司查询注入 **918 字符 10 条**全是爱范儿/IT之家（苹果折叠屏、60W 快充、iPhone 18），目标公司相关 **0 条** | 每次搜索失败都灌噪声 |
| 5 | 单 Agent 技能未开 `tools_enabled` → 无 function-calling | 同标的、同分钟：多 Agent 链路 557,627 prompt tokens 给出 2026H1 营收 33.36 亿/+26.66%/归母+72.33%；单 Agent 链路 3,826 tokens 写"需要核实" | 144 份报告 68 份（47%）含"需要核实/待核实" |
| 6 | 全链路无公告/定期报告数据源 | `grep disclosure_report app/` → 0 处；而平台自带 `get_financial_data('688008.SH')` 本就返回 20260630 营业总收入 3,335,497,521.81（33.35 亿） | 数据在，是接线/分发问题 |

## 修复（3 批，每批带自检）

批1/P0（提交 `2e01993`）
- `app/tools/name_index.py`（新）：全A名称索引（akshare 5562 条，按日缓存于 DataCache，网络失败可用 30 天旧表兜底）；只接受 >=4 字全名子串匹配、同长度多标的判多义并拒绝，避免把"苹果折叠屏产业链"这类主题查询绑到不相干标的。
- `app/tools/text_relevance.py`（新）：搜索词清洗（剥离"给我/一个深度报告/你看一下/还能不能买"等祈使短语）+ 相关性 token 化（>6 字中文串拆 2-gram、泛化字词剔除）。
- `market_data.resolve_symbol()`（新）：统一解析入口，返回 `(symbol, source)`；顺序＝显式代码（环视断言，中文相邻可命中）> 硬编码名表 > 全A名称索引 > 显式美股 ticker（大写 <=5 字母/已知 ticker/整串即 ticker）。`_resolve_company_name` 保留并加索引兜底，向后兼容旧调用方。
- `core/context.py`：改用 `resolve_symbol`；搜索关键词优先用"公司名+代码"；兜底爬取改走核心词过滤，一条不中就整块不注入。
- `app/tools/web_search.py`：门闸 token 化改用共享模块（阈值 0.5 不动）。

批2/P1（提交 `c7d3db4`）
- `app/tools/announcements.py`（新）：巨潮定期报告（披露日+公告链接）、东财个股公告、同花顺高管持股变动。实测 688008：半年报 2026-08-29、公告 1157 条、高管变动 58 条（含杨崇和）。
- `app/tools/hibor_reports.py`（新）：慧博投研频道列表页匿名可抓（精选研报 38/公司调研 132/港美研究 98 条实测），**港美研究补上东财研报中心没有的港股美股研报**；详情页全文被登录墙拦截 → 返回 `available=false` + 实测证据，绝不冒充全文。Wind：本机无终端、无 WindPy 授权，`wind_reports()` 不返回任何数据并要求付费终端授权后才能接入。
- `harness/tools.py`：新增 `fetch_company_announcements` / `fetch_hibor_reports` / `fetch_hibor_report_detail` 三个工具（schema + 执行器 + ToolDef + 熔断分类）。
- `core/context.py`：公告与慧博两个并行注入分支。实测澜起科技上下文由 3,826 tokens 的零注入 → 10,247 字符（行情+研报+公告+高管变动）。
- `registry.py`：`management-deep-dive`、`investment-research` 开 `tools_enabled`（机制早已存在，只是开闸）。

批3/P2（同批提交）
- `harness/freshness_gate.py`（新）+ `guards.py` 第 4 层门闸：报告写"需要核实"而本次注入上下文已有对应数据块 → 告警（不改正文）。判定刻意做窄：缺口措辞须与数据类别名词 ±20 字同现；报告已引用上下文高辨识度数据（>=2 个）则豁免 —— 避免对 `earnings-team` 那种 40 处"需要核实"的诚实证据链标注滥告警。

顺带修复（端到端留痕发现的真实缺陷）
- `read_webpage` 遇 so.com/sogou 搜索跳转壳页（HTTP 200 + JS 跳转）解析不出正文；两次失败即把 read_webpage 熔断，导致巨潮/东财公告正文全读不到，报告里出现"网页直读与进一步网络检索受工具限制未能完全展开"。现在会解析跳转目标再抓一跳，且页面解析失败不再计入熔断失败。实测：两个原失败链接分别取到腾讯新闻与东财财富号正文（含杨崇和履历）。

## 端到端验收（真实 HTTP 链路，`POST /api/research`）

同一查询 `澜起科技你给我一个深度报告`（management-deep-dive），跑了两轮：

| 指标 | 修复前（09-13 12:48） | 修复后第一轮（13:26） | 修复后第二轮（13:38，含 read_webpage 修复） |
|------|----------------------|----------------------|------------------------------------------|
| prompt tokens | 3,826 | 180,667 | 210,746 |
| 耗时 | — | 109s | 252s |
| 行情 | 无 | ¥191.96（-1.73%）、总市值 2,346 亿（verify_market_cap 校验偏差 0.00%） | ¥191.96、2,346.1 亿（校验偏差 0.01%）、PE 57.2/PB 10.9（verify_valuation 复算一致） |
| 财报 | "具体数字需要核实" | 2026 半年报：营收 33.35 亿、归母 19.97 亿、经营现金流 13.28 亿 | 同左，且标注披露日 2026-08-29（3 处）、2025 年报 2026-03-31 |
| "需要核实"措辞 | 18 处 | 5 处 | **1 处** |
| "数据时效声明"块 | 有（开篇必读） | 有 | 改为"数据时效与核查声明"，第 1 条即"财务数据取自 2026 年半年报（2026-08-29 披露）" |
| 来源标注 | 0 | 13 处 | 15 处 |
| 工具调用 | 0（无工具） | 19 次 | 19 次（含 read_webpage 成功读取东财公告页；上一轮此调用被熔断） |

第二轮残留的 1 处"需要核实"指向招股书/年报"董事、监事、高级管理人员情况"章节内的细节，
属上下文确实没有的明细级缺口（新鲜度门闸按设计对"已引用数据后的子科目缺口"豁免）。

## 自检与回归

| 脚本 | 项数 | 结果 |
|------|------|------|
| `scripts/verify_batch1.py` | 47 | PASS 47 / FAIL 0 |
| `scripts/verify_announcements.py` | 23 | PASS 23 / FAIL 0 |
| `scripts/verify_hibor.py` | 35 | PASS 35 / FAIL 0 |
| `scripts/verify_freshness_gate.py` | 27 | PASS 27 / FAIL 0 |
| `scripts/verify_read_webpage_redirect.py` | 15 | PASS 15 / FAIL 0 |
| `scripts/verify_tool_loop_caps.py` | 13 | PASS 13 / FAIL 0 |
| `pytest tests/` | 443 | 443 passed |

## 遗留与未做

- Wind 机构研报：需付费终端 + WindPy 授权，`wind_available()` 当前返回 `(False, 原因)`，接入前不会返回任何数据。
- 慧博研报全文需登录，匿名只能拿列表与摘要预览；中盘个股有时不在当天列表页快照内（返回空块，不编造）。
- 新开工具的技能输出改为整段非流式（机制如此）；单Agent prompt tokens 由 ~4k 涨到 ~180-210k，成本与耗时（109s → 252s）上升需观察。
- 单Agent 每工具调用上限已从 3 放宽到 8（`MAX_SAME_TOOL_CALLS_SINGLE`）；若成本过高可回调该常量。

---

# 静态审计后续收紧（2026-09-13 第1~4步）

外部静态审计（Codex，`C:\Users\13531\Documents\Codex\2026-09-13\...\static-audit-20260913.md`）
结论为"通过，无阻断性问题"，同时提出 3 条收紧项 + 1 条风险项。逐条实测复核后全部成立，按 1→2→3→4
顺序落地（每步跑自检再进下一步）。

## 第 1 步（提交 b4d2b46）：公告正文打通

**审计原文**：read_webpage 验收把"51 字符通用风险声明"判为"公告页可读"，不能证明取得正文。

**实测复核（比审计说的更严重）**：
- 东财公告详情页：read_webpage 得 51 字符"郑重声明…以交易所公告为准"，`quality` 为空判据下算成功。
- 巨潮公告详情页：264 字符全是 `{{stockName}}` 这类 Vue 占位符 —— 也是 JS 空壳。
- 结论：公告正文经网页**根本读不到**，此前"公告源已接入"只对元数据成立。

**修复**：新增 `app/tools/announcement_docs.py` —— 由巨潮链接里的 announcementId +
announcementTime 拼静态 PDF（`http://static.cninfo.com.cn/finalpage/<日期>/<id>.PDF`，实测
2025 年报 248 页 3.72MB、2026 半年报 196 页 3.29MB 均可下载），PyMuPDF 抽全文后按"字符间允许
空白"的锚点正则定位「董事和高级管理人员的情况」章节（含主板/科创板/回退共 7 个锚点变体），
只缓存章节摘录（TTL 30 天，缓存键不含 max_chars）。命中摘录实测含杨崇和、持股变动、薪酬字段；
冷启动 2.2s、缓存命中 <0.01s。

接入：`core/context.py` 公告分支追加正文摘录（仅对 7 个管理层/财务精读技能开启，避免给全部技能
加首次下载成本）；新增工具 `fetch_announcement_fulltext` 供带工具技能按需核实。
`read_webpage` 增加 `quality` 标记（ok/shell/thin）：模板占位符或 <200 字符判 shell、<400 判 thin，
并在 note 指向 PDF 工具 —— 调用方再也不会把落地页当正文。

自检：`scripts/verify_announcement_docs.py` **34 项**（离线 20 + 联网 14）；
`scripts/verify_read_webpage_redirect.py` 断言由"内容非空"收紧为"必须如实标注为非正文"，16 项。

## 第 2 步（提交 d0bf5ca）：慧博列表剔除未来日期条目

**审计原文**：慧博列表出现 2026-09-14（抓取日为 09-13）的条目，建议拒绝/标记并纳入回归。

**实测复核**：港美研究 / 公司调研 / 行业分析 三频道各 60 条里各有 1 条日期为 2026-09-14
（华鑫证券-传媒行业动态研究报告-260914），占 1.7%；是源站自身日期，非解析错误。

**修复**：`is_future_date`（strptime 真解析，空值/非法日期不误删）+ `drop_future_entries`
（剔除并按数量+样例记 warning），接在唯一入口 `fetch_hibor_reports` 的返回路径上，
**缓存路径同样过滤**（否则 TTL 30 分钟的旧缓存会绕过新规则 —— 自检 D10 抓出来的）。
实测三频道由"60 条含 1 条未来"变为"59 条含 0 条"。

自检：`scripts/verify_hibor.py` 新增 D 段 14 项（离线 9 + 联网 3 + 源码级 2），35 → **49 项**。

## 第 3 步（提交 ad8f99b）：报告 meta 记录工具调用汇总

**审计原文**：单Agent 工具链把 prompt 从 ~3.8k 提到 180k–210k tokens、每工具可调 8 次，
应在真实流量上加观测/告警。

**实测复核**：91 份 meta.json 键集合为 skill_name/arguments/duration_seconds/tokens/created_at/
summary/task_id/model/guard_warnings/data_status —— **无任何工具字段**；工具调用只在
`data/scratchpad/<task>.jsonl`（69 文件 2932 条）。审计说得对，而且比它描述的更彻底。

**修复**：runner 在 run() 末尾留痕（含撞上限次数 `limit_hits`，被拒调用不计入计数但"撞上限"
本身要观测）；`persist.save_report` 写 `meta["tool_calls"]`（total / by_tool / by_agent /
agents_with_tools / limit_hits，各表 ≤20 条；total=0 不写字段）。

自检：`scripts/verify_tool_call_meta.py` **25 项**（汇总函数 13 + 落盘 6 + 接线 6）。

## 第 4 步：重新打包审计包（同一 HEAD）

第 3 条审计项指出包内 `manifest.md` 的 HEAD（ab8925a）与交接文档提交（49badfa）不一致 ——
成立，是打包时序问题。重打包后 manifest / patches / handoff 全部基于同一 HEAD，
并把本轮的 4 个提交补丁、全部自检输出一并纳入。
