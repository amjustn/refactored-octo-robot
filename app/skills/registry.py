# Auto-generated skill registry for AI Berkshire Web
# Maps skill names from ai-berkshire to web-compatible metadata

SKILLS = [
    {
        "name": "investment-research",
        "display_name": "四大师综合分析",
        "description": "巴菲特·芒格·段永平·李录四大师综合深度分析一家公司，7模块顺序执行",
        "category": "深度研究",
        "is_multi_agent": False,
        "agent_count": 1,
        # 2026-09-13 批2/P1：单Agent技能开工具（第1批试点 2 个技能）。
        # 该技能 17 份历史报告里 9 份含"需要核实"，prompt 中位数仅 4237 tokens
        # （等于没有数据注入）→ 开闸让模型自己能核实。
        "tools_enabled": True,
        "input_hint": "描述你的分析目标，可包含公司名/代码，如：分析腾讯的护城河与长期投资价值",
    },
    {
        "name": "investment-team",
        "display_name": "多Agent投研团队",
        "description": "4个Agent并行研究：商业模式+财务估值+行业竞争+风险管理，Team Lead综合",
        "category": "深度研究",
        "is_multi_agent": True,
        "agent_count": 4,
        # P5：交叉辩论开放受限核对工具（复算/原始数据白名单，评审者≤2次）
        "debate_tools": True,
        # P5：被评审者一轮答辩权，筛掉截断/误读产生的假分歧
        "debate_rebuttal": True,
        # P5：立场卡（防漂移）+ 法官分离（结构化证据优先、辩词原文降级附录）
        "debate_stance": True,
        "agents": ["business-analyst", "financial-analyst", "industry-researcher", "risk-assessor"],
        "input_hint": "描述你的分析目标，可包含公司名/代码，如：分析美团外卖业务的竞争壁垒",
    },
    {
        "name": "management-deep-dive",
        "display_name": "管理层深研",
        "description": "管理层纵深研究：能力圈+诚信度+资本配置+战略眼光+历史决策",
        "category": "深度研究",
        "is_multi_agent": False,
        "agent_count": 1,
        # 2026-09-13 批2/P1：单Agent技能开工具（第1批试点 2 个技能）。
        # 实测依据：该技能原本 prompt 仅 3826 tokens（零数据注入），报告开篇写
        # "2025年报、2026半年报应已披露，但具体数字与最新高管名单需核实"；
        # 同一时刻同一标的的多Agent链路（557k tokens，带工具）给出精确数字。
        # 机制早已存在（harness/runner.py skill_tools_enabled 分支），此处只是开闸。
        "tools_enabled": True,
        "input_hint": "管理层名+公司名，如：深研王兴及美团管理层的资本配置能力",
    },
    {
        "name": "private-company-research",
        "display_name": "未上市公司研究",
        "description": "未上市公司侦探式研究，多源拼凑+置信度标注",
        "category": "深度研究",
        "is_multi_agent": False,
        "agent_count": 1,
        "input_hint": "描述研究目标，如：侦探式研究字节跳动的商业模式与估值水平",
    },
    {
        "name": "broker-reports",
        "display_name": "券商研报",
        "description": "机构研报检索与分析：评级分布+一致预期+主流观点归纳+独立判断",
        "category": "深度研究",
        "is_multi_agent": False,
        "agent_count": 1,
        "tools_enabled": True,
        # 结构门闸只认显式声明的报告章节；技能文档里的"第一步/第二步…"
        # 是执行流程，不是报告必备章节。
        "output_sections": ["机构观点总览", "主流机构观点归纳", "独立判断"],
        "input_hint": "A股公司名或代码，如：查看贵州茅台(600519)的最新券商研报与机构观点",
    },
    {
        "name": "deep-company-series",
        "display_name": "深度系列长文",
        "description": "8篇长文系列拆解一家公司，公众号级深度(12万字)",
        "category": "深度研究",
        "is_multi_agent": False,
        "agent_count": 1,
        # Series mode: each topic is a separate LLM call to avoid truncation
        "series_mode": True,
        "series_topics": [
            "公司简史：从创立到今天的里程碑",
            "商业模式深度拆解",
            "护城河全景分析",
            "财务数据十年回顾",
            "管理层全貌",
            "竞争格局与行业趋势",
            "估值方法与合理区间",
            "投资结论与风险清单",
        ],
        "input_hint": "公司名即可，如：为拼多多写一套8篇深度系列长文",
    },
    {
        "name": "global-macro-analysis",
        "display_name": "全球宏观分析",
        "description": "3个Agent并行研究：制度与政治经济+数据预测纪律+宏观资产映射，Team Lead综合",
        "category": "深度研究",
        "is_multi_agent": True,
        "agent_count": 3,
        # P5：交叉辩论开放受限核对工具 + 被评审者一轮答辩权
        "debate_tools": True,
        "debate_rebuttal": True,
        "agents": ["macro-institutional-economist", "macro-forecaster", "macro-strategist"],
        "input_hint": "描述你想分析的全球宏观问题，如：美联储2026年降息路径与全球资产影响",
    },
    {
        "name": "china-macro-analysis",
        "display_name": "中国宏观分析",
        "description": "4个Agent并行研究：政策反应函数+高频数据周期+汇率国际收支+中长期结构，Team Lead综合",
        "category": "深度研究",
        "is_multi_agent": True,
        "agent_count": 4,
        # P5：交叉辩论开放受限核对工具 + 被评审者一轮答辩权
        "debate_tools": True,
        "debate_rebuttal": True,
        "agents": ["cn-policy-framework", "cn-data-cycle", "cn-fx-external", "cn-structure-trend"],
        "input_hint": "描述你想分析的中国宏观问题，如：2026年地产企稳对信用周期和A股的影响",
    },
    {
        "name": "earnings-review",
        "display_name": "财报精读",
        "description": "一手资料深度解读财报：核心数据+管理层语气+附注挖掘+承诺追踪",
        "category": "财报分析",
        "is_multi_agent": False,
        "agent_count": 1,
        "input_hint": "公司名+季度，如：精读腾讯2025Q4财报，关注管理层语气变化",
    },
    {
        "name": "earnings-team",
        "display_name": "财报精读团队",
        "description": "四大师并行解读财报+编辑润色+读者评审，产出可发布文章",
        "category": "财报分析",
        "is_multi_agent": True,
        "agent_count": 6,
        # P5：交叉辩论开放受限核对工具 + 被评审者一轮答辩权
        "debate_tools": True,
        "debate_rebuttal": True,
        "agents": ["business-interpreter", "financial-auditor", "competition-analyst", "risk-hunter", "editor", "reader-reviewer"],
        # Phased execution: these agents run AFTER Team Lead synthesis
        # Phase 1: 4 research agents run in parallel
        # Phase 2: Team Lead synthesizes research into report
        # Phase 3: editor rewrites, reader-reviewer gives feedback (sequential)
        "post_synthesis_agents": ["editor", "reader-reviewer"],
        "input_hint": "公司名+季度/年报，如：精读腾讯2025Q4财报并产出可发布文章",
    },
    {
        "name": "financial-data",
        "display_name": "财务数据规范",
        "description": "财务数据获取与交叉验证规范：数据源优先级+误差计算+来源标注",
        "category": "财报分析",
        "is_multi_agent": False,
        "agent_count": 1,
        "input_hint": "描述需要的数据，如：获取并交叉验证PDD / 0700.HK 的财务数据",
    },
    {
        "name": "industry-research",
        "display_name": "产业链全景扫描",
        "description": "按行业环节切片分析，上中下游格局+主要玩家+价值分配",
        "category": "行业筛选",
        "is_multi_agent": False,
        "agent_count": 1,
        "input_hint": "描述想研究的行业，如：扫描AI算力产业链的上中下游格局与价值分配",
    },
    {
        "name": "industry-funnel",
        "display_name": "行业漏斗筛选",
        "description": "全市场30-60家→粗筛≤10家→精细分析→终选3家，按组合互补性配置",
        "category": "行业筛选",
        "is_multi_agent": False,
        "agent_count": 1,
        "input_hint": "描述筛选方向，如：从机器人行业筛出3家最值得配置的公司",
    },
    {
        "name": "bottleneck-hunter",
        "display_name": "瓶颈猎手",
        "description": "从超级趋势寻找产业链物理瓶颈和套利机会，Layer 0-4拆解",
        "category": "行业筛选",
        "is_multi_agent": False,
        "agent_count": 1,
        "input_hint": "描述一个超级趋势，如：寻找AI基础设施产业链中的物理瓶颈环节",
    },
    {
        "name": "portfolio-review",
        "display_name": "组合审视",
        "description": "仓位体检+集中度分析+机会成本评估+压力测试+调仓建议",
        "category": "持仓管理",
        "is_multi_agent": False,
        "agent_count": 1,
        "input_hint": "描述你的持仓，如：腾讯30%、美团20%、茅台20%、现金30%，做一次仓位体检",
    },
    {
        "name": "thesis-tracker",
        "display_name": "持仓评估",
        "description": "持仓健康度评估：核心假设检查+红线触发+估值更新+买卖信号",
        "category": "持仓管理",
        "is_multi_agent": False,
        "agent_count": 1,
        "input_hint": "公司名/代码即可，首次建立投资论文、后续追踪检查，如：追踪腾讯的核心投资假设",
    },
    {
        "name": "news-pulse",
        "display_name": "股价异动归因",
        "description": "股价异动归因：4维并行侦察(公司+监管+行业+情绪)+事件时间线，信息不实时立即报错",
        "category": "持仓管理",
        "is_multi_agent": True,
        "agent_count": 4,
        # P5：交叉辩论开放受限核对工具 + 被评审者一轮答辩权
        "debate_tools": True,
        "debate_rebuttal": True,
        "agents": ["company-event-scout", "regulatory-watcher", "industry-peer-analyst", "sentiment-tracker"],
        # 输出章节（multi 策略下结构门闸只校验顶层标题，此处登记以声明契约）
        "output_sections": ["一句话归因", "事件时间线", "异动归因表", "性质判断", "行动建议"],
        "input_hint": "描述异动情况，如：腾讯今天跌了5%，帮我归因",
    },
    {
        "name": "daily-briefing",
        "display_name": "金融日报",
        "description": "每日金融日报：自动上网搜索最新行情+新闻+行业动态，生成结构化日报",
        "category": "日报",
        "is_multi_agent": False,
        "agent_count": 1,
        # 单Agent技能级工具开关：提示词明确要求调用 web_search /
        # fetch_market_indices / get_stock_price，需走 function-calling 循环，
        # 否则模型只会输出 <tool_calls> 文本标记（DSML 泄漏根因）。
        "tools_enabled": True,
        # 报告模板章节（见 daily-briefing.md「报告结构（严格按此模板）」）
        "output_sections": ["一、数据快照", "二、补充说明", "三、信息边界说明"],
        "input_hint": "留空生成今日日报，或描述关注方向，如：关注新能源和腾讯的今日动态",
    },

]
