"""Multi-agent orchestrator for AI Berkshire Web

Handles parallel agent execution for multi-agent skills,
phased execution for editorial pipelines, series generation
for long-form content, financial tool integration, and
real-time market data integration with context injection.
"""
import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator, Optional

from ..core.llm import chat_stream, chat_complete, chat_complete_with_tools
from ..core.context import build_full_context
from ..skills import get_skill, build_user_prompt
from ..models.schemas import AgentProgress
from ..core.config import MAX_PARALLEL_AGENTS, MAX_REPORT_LENGTH, AGENT_TIMEOUT

# Series reports can be much longer (8 articles × 3000-5000 words each)
MAX_SERIES_REPORT_LENGTH = 200000  # 200k chars for series mode

logger = logging.getLogger("ai_berkshire.orchestrator")

AGENT_ROLE_PROMPTS = {
    "business-analyst": """你是投研团队中的"商业模式分析师"，负责从段永平的投资视角分析目标公司。

核心框架：
1. 商业模式本质：核心生意定义、收入结构拆解
2. 平台/产品飞轮效应如何运转
3. 护城河分析：品牌/转换成本/网络效应/规模效应/技术壁垒，逐一验证
4. 用户/客户价值：为各方创造了什么独特价值
5. 段永平"好生意"标准评估：差异化、定价权、可持续竞争优势

输出要求：
- 使用Markdown表格呈现关键数据
- 每个分析维度有明确结论和评分（★1-5）
- 报告末尾有总体结论""",

    "financial-analyst": """你是投研团队中的"财务估值分析师"，负责从巴菲特的投资视角分析目标公司。

核心框架：
1. 近3-5年营收、净利润、经营利润趋势
2. 盈利能力指标：ROE、ROA、毛利率、经营利润率
3. 现金流分析：经营性现金流、自由现金流、资本开支
4. 资产负债表健康度：现金储备、负债率、流动性
5. 估值分析：PE/PS/PB/EV等，与历史及同业对比
6. 安全边际评估：内在价值 vs 当前股价
7. 三情景估值（乐观/中性/悲观）

输出要求：
- 所有计算必须精确，标注数据来源
- 关键数据至少2个来源交叉验证
- 使用Markdown表格呈现
- 给出明确的估值结论

你可以使用以下工具来辅助分析：
- get_stock_price: 获取目标公司最新股价和行情数据
- get_financial_data: 获取财务报表数据
- fetch_company_news: 获取公司近期新闻
- verify_market_cap: 验证市值 = 股价 × 股本
- verify_valuation: 计算PE/PB/FCF收益率/股息率
- cross_validate: 交叉验证同一指标的多源数据
- three_scenario: 三情景估值计算
- calc: 精确财务计算
- benford: 本福特定律检测数据异常

请在分析中主动调用工具获取最新数据，不要依赖可能过时的训练数据。""",

    "industry-researcher": """你是投研团队中的"行业竞争分析师"，负责从芒格的逆向思考视角分析目标公司。

核心框架：
1. 行业规模与增长：市场规模、增速、渗透率
2. 竞争格局：主要对手市场份额、竞争策略对比
3. 核心竞争者威胁评估：逐个分析主要竞争对手
4. 各细分赛道格局
5. 行业趋势：技术变革、政策影响、新进入者
6. 芒格式逆向思考：什么会杀死这家公司？

输出要求：
- 竞争对手对比表
- 行业趋势判断（改善/持平/恶化）
- 逆向风险清单

你可以使用 get_stock_price 和 fetch_company_news 工具获取竞争对手的最新数据。""",

    "risk-assessor": """你是投研团队中的"风险评估师"，负责从李录的长期确定性视角分析目标公司。

核心框架：
1. 管理层评估：CEO能力圈、诚信度、战略眼光、资本配置能力
2. 监管风险：当前及潜在监管影响
3. 竞争风险：各竞争对手威胁程度评估
4. 业务风险：新业务亏损、扩张不确定性
5. 宏观风险：经济周期、行业周期影响
6. 治理结构：股权结构、关联交易、股东回报政策
7. 长期确定性：10年后公司会怎样？什么可能颠覆其商业模式？

输出要求：
- 管理层可信度评分（★1-5）
- 风险信号清单（致命/严重/警告）
- 永久性资本损失风险评估

你可以使用 fetch_company_news 工具获取公司近期风险事件。""",

    "company-event-scout": """你是新闻脉搏团队中的"公司事件侦察员"。搜索目标公司最近14天内的官方公告、财报、管理层动作、重大业务事件、资本运作和诉讼合规事件。每条事件标注日期/来源/与股价异动的相关性（高/中/低），按时间倒序输出。

请使用 fetch_company_news 工具获取公司最新新闻。""",

    "regulatory-watcher": """你是新闻脉搏团队中的"监管政策观察员"。搜索目标公司相关的行业监管新规、跨境政策（中美关系/关税/出口管制）、税收政策、反垄断和特殊行业政策变化。判断是否有"政策黑天鹅"落地。""",

    "industry-peer-analyst": """你是新闻脉搏团队中的"行业对手分析师"。分析目标公司的直接竞争对手近期动态、产业链上下游变化、行业景气度数据和替代品威胁。核心判断：这是公司个体事件还是行业Beta波动？

你可以使用 get_stock_price 获取竞争对手行情数据。""",

    "sentiment-tracker": """你是新闻脉搏团队中的"市场情绪追踪员"。搜索卖方评级调整、机构持仓变化、做空数据、大V观点和市场传言。核心判断：基本面驱动还是情绪/资金面驱动？""",

    "business-interpreter": """你是财报精读团队中的"生意本质解读者"（段永平视角）。分析收入结构变化、用户价值增减、护城河检测和"好生意"标准评估。每个子项标注改善/持平/恶化。

你可以使用 get_financial_data 和 fetch_company_news 工具获取最新财报和新闻。""",

    "financial-auditor": """你是财报精读团队中的"财务质量审计师"（巴菲特视角）。重点分析现金流质量、利润质量检验、资产负债表健康度和估值安全边际更新。

你可以使用以下工具：
- get_stock_price: 获取最新股价
- get_financial_data: 获取财务报表
- verify_valuation: 计算PE/PB/FCF收益率
- cross_validate: 交叉验证数据
- calc: 精确财务计算
- benford: 本福特定律检测财报数据异常

请主动调用工具获取最新数据进行验证。""",

    "competition-analyst": """你是财报精读团队中的"竞争变化解读者"（芒格视角）。从财报推断竞争变化、竞争对手对比、管理层对竞争的讨论和行业趋势信号。

你可以使用 get_stock_price 获取竞争对手数据。""",

    "risk-hunter": """你是财报精读团队中的"风险信号猎手"（李录视角）。分析管理层语气、承诺追踪、附注隐藏信息和永久性资本损失风险。

你可以使用 fetch_company_news 工具获取公司近期风险事件。""",

    "editor": """你是一位专业的公众号财经编辑。把研究报告改写成读者爱看、能看懂的文章。保留专业深度但提升可读性。用类比解释财务术语，段落不超过4行，每500字插入小结。""",

    "reader-reviewer": """你是一位普通投资者。以"关注价值投资、有基础财务知识"的视角评审文章。从可读性、信息价值、可信度和行动指导性四个维度打分，给出修改建议。""",

    "researcher": """你是公众号文章的研究员。负责搜集主题相关的深度资料、数据和案例，确保文章有扎实的内容基础。

你可以使用 get_stock_price 和 fetch_company_news 工具获取最新数据来支撑文章内容。""",
}

TEAM_LEAD_PROMPT = """你是投研团队的Team Lead，负责综合协调和最终报告产出。

你已经收到了多位专业分析师的独立研究报告。

请综合所有报告，输出以下结构的最终投研报告：

## 综合投研报告

### 一、一句话结论
> 50-100字概括是否值得投资及核心逻辑

### 二、四维评分总表
| 维度 | 分析框架 | 评分(★1-5) | 核心判断 |
|------|---------|------------|---------|

综合评分：X/5

### 三、核心数据速览
关键财务和经营指标表格

### 四、各维度分析摘要
每个维度3-5条最重要的发现

### 五、投资论点（Bull vs Bear）
- 🟢 看多逻辑（5-7条）
- 🔴 看空逻辑（5-7条）

### 六、最终投资建议
| 策略 | 建议 | 价格区间 |
|------|------|---------|
| 激进型 | ... | ... |
| 稳健型 | ... | ... |
| 保守型 | ... | ... |

### 七、关键催化剂
- 加仓信号（3-5条）
- 减仓信号（3-5条）

### 八、总结
100-200字最终总结，包含信息丰富度评级和AI研究局限性声明。
"""

SERIES_SYSTEM_PROMPT = """你是一位资深的财经深度写作专家，擅长撰写公众号级别的深度长文。

你正在撰写一个关于某公司的8篇系列深度文章。每篇文章需要：
- 3000-5000字
- 数据翔实，来源明确
- 观点鲜明，不打太极
- 公众号级深度和可读性
- 每篇独立成文，同时与系列其他篇相互关联

你可以使用 get_stock_price、get_financial_data 和 fetch_company_news 工具获取公司的最新数据，确保文章内容准确、时效性强。

请严格按照指定主题撰写，保持专业深度。"""

# ==================== Financial Tool Schemas ====================

FINANCIAL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "verify_market_cap",
            "description": "验证市值 = 股价 × 股本，返回偏差百分比和通过/失败状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "price": {"type": "number", "description": "当前股价"},
                    "shares": {"type": "number", "description": "总股本（股）"},
                    "reported": {"type": "number", "description": "报告的市值"},
                    "currency": {"type": "string", "description": "货币单位", "default": "USD"},
                },
                "required": ["price", "shares", "reported"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_valuation",
            "description": "计算关键估值比率：PE/PB/FCF收益率/股息率",
            "parameters": {
                "type": "object",
                "properties": {
                    "price": {"type": "number", "description": "当前股价"},
                    "eps": {"type": "number", "description": "每股收益"},
                    "bvps": {"type": "number", "description": "每股净资产"},
                    "fcf_per_share": {"type": "number", "description": "每股自由现金流"},
                    "dividend": {"type": "number", "description": "每股股息"},
                },
                "required": ["price"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cross_validate",
            "description": "交叉验证同一指标的多源数据，返回偏差和通过/警告/失败状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "指标名称"},
                    "values": {"type": "object", "description": "各数据源的值，如 {\"源A\": 1000, \"源B\": 1005}"},
                    "unit": {"type": "string", "description": "单位", "default": ""},
                },
                "required": ["field", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "three_scenario",
            "description": "三情景估值：乐观/中性/悲观，返回目标价和上涨空间",
            "parameters": {
                "type": "object",
                "properties": {
                    "price": {"type": "number", "description": "当前股价"},
                    "eps": {"type": "number", "description": "当前每股收益"},
                    "shares_100m": {"type": "number", "description": "总股本（亿股）"},
                    "growth_rates": {"type": "array", "items": {"type": "number"}, "description": "三个情景的增长率"},
                    "pe_multiples": {"type": "array", "items": {"type": "number"}, "description": "三个情景的PE倍数"},
                    "currency": {"type": "string", "default": "CNY"},
                },
                "required": ["price", "eps", "shares_100m", "growth_rates", "pe_multiples"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": "精确财务计算，支持加减乘除、括号、幂运算。例如: '100 * (1 + 0.1)'",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "benford",
            "description": "本福特定律检测：检查一组数字是否符合本福特定律分布，用于发现财务数据异常",
            "parameters": {
                "type": "object",
                "properties": {
                    "numbers": {"type": "array", "items": {"type": "number"}, "description": "待检测的数字列表"},
                },
                "required": ["numbers"],
            },
        },
    },
]

# ==================== Market Data Tool Schemas ====================

DATA_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_price",
            "description": "获取股票最新实时行情数据（支持A股/港股/美股）。自动检测市场类型。返回股价、涨跌幅、成交量、PE、PB、市值等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 600519 / 0700.HK / AAPL"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_financial_data",
            "description": "获取公司财务报表数据（利润表/资产负债表/现金流量表），返回最近几个季度的数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_company_news",
            "description": "获取公司近期新闻列表，返回标题、内容摘要、日期和来源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                    "days": {"type": "integer", "description": "获取最近多少天的新闻，默认14天", "default": 14},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_market_indices",
            "description": "获取全球主要市场指数（上证指数/深证成指/恒生指数/标普500/纳斯达克），包含最新价格和涨跌幅。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_company",
            "description": "按公司名称或代码搜索股票，返回匹配的股票列表（含代码、名称、市场）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词（公司名或股票代码）"},
                },
                "required": ["query"],
            },
        },
    },
]


# ==================== Web Search Tool Schemas ====================

WEB_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取最新信息。可以搜索公司新闻、行业报告、政策法规、宏观数据等任何主题。返回搜索结果列表（标题、URL、摘要）。使用场景：当你需要查找训练数据中没有的最新信息时，先搜索再阅读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，如'贵州茅台 2025年业绩'或'新能源汽车行业趋势'"},
                    "max_results": {"type": "integer", "description": "最大返回结果数，默认10", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": "读取指定URL的网页内容，提取干净的文本。可以读取新闻文章、研究报告、年报、公告等任何网页。返回标题和正文内容。使用场景：搜索到相关结果后，读取具体页面获取详细信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要读取的网页URL"},
                    "max_chars": {"type": "integer", "description": "最大读取字符数，默认8000", "default": 8000},
                },
                "required": ["url"],
            },
        },
    },
]


# ==================== Industry Chain Tool Schemas ====================

INDUSTRY_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_industry_chain",
            "description": "从专业产业研究网站搜索产业链信息。自动匹配最相关的数据源（艾瑞咨询/头豹研究院/前瞻产业研究院等），执行站内搜索。返回搜索结果列表（标题、URL、摘要、来源）。适用于分析行业上下游、竞争格局、产业链环节时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "行业或产业链关键词，如'新能源汽车产业链'或'半导体上游材料'"},
                    "max_per_source": {"type": "integer", "description": "每个数据源最大结果数，默认3", "default": 3},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_industry_sources",
            "description": "列出所有可用的产业研究数据源（艾瑞咨询/头豹/前瞻等），可按类别筛选。返回数据源列表（名称、域名、覆盖领域、描述）。适用于了解有哪些专业数据源可用于产业链研究。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "按类别筛选，如'新能源'/'半导体'/'消费电子'。为空返回全部。", "default": ""},
                },
            },
        },
    },
]

# All tools available to agents
ALL_TOOL_SCHEMAS = FINANCIAL_TOOL_SCHEMAS + DATA_TOOL_SCHEMAS + WEB_TOOL_SCHEMAS + INDUSTRY_TOOL_SCHEMAS


# ==================== Tool Executor ====================

async def _execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a tool (financial or market data) and return result as string.

    Supports both sync financial tools and async market data tools.
    """
    # --- Financial tools (sync) ---
    from ..tools import (
        verify_market_cap, verify_valuation, cross_validate,
        three_scenario, calc, benford,
    )
    sync_tools = {
        "verify_market_cap": verify_market_cap,
        "verify_valuation": verify_valuation,
        "cross_validate": cross_validate,
        "three_scenario": three_scenario,
        "calc": calc,
        "benford": benford,
    }

    if tool_name in sync_tools:
        try:
            result = sync_tools[tool_name](**arguments)
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # --- Market data tools (async) ---
    from ..tools.market_data import (
        get_stock_price, get_financial_data,
        fetch_company_news, fetch_market_indices,
        search_company,
    )
    # --- Web search tools (async) ---
    from ..tools.web_search import web_search, read_webpage
    # --- Industry chain tools (async) ---
    from ..tools.industry_sources import search_industry_chain, list_industry_sources

    async_tools = {
        "get_stock_price": get_stock_price,
        "get_financial_data": get_financial_data,
        "fetch_company_news": fetch_company_news,
        "fetch_market_indices": fetch_market_indices,
        "search_company": search_company,
        "web_search": web_search,
        "read_webpage": read_webpage,
        "search_industry_chain": search_industry_chain,
        "list_industry_sources": lambda category="": list_industry_sources(category),
    }

    if tool_name in async_tools:
        try:
            result = await async_tools[tool_name](**arguments)
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool: {tool_name}"}, ensure_ascii=False)


# Agents that have tool access (financial + market data)
# All research/analysis agents get tools; editorial agents don't need them
TOOL_ENABLED_AGENTS = {
    "business-analyst", "financial-analyst", "industry-researcher", "risk-assessor",
    "company-event-scout", "industry-peer-analyst",
    "business-interpreter", "financial-auditor", "competition-analyst", "risk-hunter",
    "researcher",
}


# ==================== Agent Execution ====================

async def run_single_agent(
    skill_name: str,
    arguments: str,
    agent_role: str = None,
    context: str = "",
    llm_config: dict = None,
) -> str:
    """Run a single agent analysis (non-streaming).

    Injects real-world context (current date, market data, knowledge base,
    skill-specific domain knowledge) into the system prompt so the agent
    has access to current information.

    If context is not provided (empty string), it is built automatically
    using build_full_context with the skill_name — this ensures ALL callers
    (REST API, WebSocket, multi-agent delegation) get proper context.

    If the agent is in TOOL_ENABLED_AGENTS, uses function calling to allow
    the agent to invoke financial + market data tools during analysis.
    """
    if agent_role and agent_role in AGENT_ROLE_PROMPTS:
        system_prompt = AGENT_ROLE_PROMPTS[agent_role]
        user_msg = f"请分析以下目标：{arguments}"
    else:
        system_prompt = "你是一位精通价值投资的分析师，遵循巴菲特、芒格、段永平、李录四位大师的方法论。"
        user_msg = build_user_prompt(skill_name, arguments)

    # Inject real-world context — build it if not provided by caller
    if not context:
        try:
            context = await build_full_context(arguments, skill_name=skill_name)
        except Exception as e:
            logger.warning(f"Context building failed in run_single_agent: {e}")
            context = ""

    if context:
        system_prompt = f"{system_prompt}\n\n{context}"

    # Use tool-enabled chat for agents that need tools
    if agent_role in TOOL_ENABLED_AGENTS:
        return await chat_complete_with_tools(
            system_prompt, user_msg,
            tools=ALL_TOOL_SCHEMAS,
            tool_executor=_execute_tool,
            llm_config=llm_config,
        )

    return await chat_complete(system_prompt, user_msg, llm_config=llm_config)


async def run_multi_agent(
    skill_name: str,
    arguments: str,
    progress_callback=None,
    llm_config: dict = None,
) -> str:
    """Run multi-agent parallel analysis and synthesize.

    Supports phased execution: if the skill defines 'post_synthesis_agents',
    those agents run AFTER the Team Lead synthesis.

    Injects real-world context into all agent prompts.
    """
    skill = get_skill(skill_name)
    agent_names = skill.get("agents", [])
    post_synthesis_agents = skill.get("post_synthesis_agents", [])

    if not agent_names:
        return await run_single_agent(skill_name, arguments, llm_config=llm_config)

    # Build real-world context once for all agents
    if progress_callback:
        await progress_callback(AgentProgress(
            agent_name="system",
            status="running",
            message="正在获取实时市场数据...",
        ))

    try:
        context = await build_full_context(arguments, skill_name=skill_name)
    except Exception as e:
        logger.warning(f"Context building failed, continuing without: {e}")
        context = ""

    if progress_callback:
        await progress_callback(AgentProgress(
            agent_name="system",
            status="completed",
            message="市场数据获取完成",
            progress=1.0,
        ))

    # Separate research agents from post-synthesis agents
    research_agents = [a for a in agent_names if a not in post_synthesis_agents]

    async def agent_task(name: str) -> tuple[str, str]:
        if progress_callback:
            await progress_callback(AgentProgress(
                agent_name=name,
                status="running",
                message=f"开始分析...",
            ))
        try:
            result = await run_single_agent(skill_name, arguments, agent_role=name, context=context, llm_config=llm_config)
            if progress_callback:
                await progress_callback(AgentProgress(
                    agent_name=name,
                    status="completed",
                    message="分析完成",
                    progress=1.0,
                ))
            return name, result
        except Exception as e:
            if progress_callback:
                await progress_callback(AgentProgress(
                    agent_name=name,
                    status="failed",
                    message=str(e),
                ))
            return name, f"[错误] {str(e)}"

    # Phase 1: Run research agents in batches
    results = []
    for i in range(0, len(research_agents), MAX_PARALLEL_AGENTS):
        batch = research_agents[i:i + MAX_PARALLEL_AGENTS]
        batch_tasks = [agent_task(name) for name in batch]
        try:
            batch_results = await asyncio.wait_for(
                asyncio.gather(*batch_tasks),
                timeout=AGENT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            batch_results = []
            for name in batch:
                batch_results.append((name, f"[错误] Agent 超时 ({AGENT_TIMEOUT}s)"))
                if progress_callback:
                    await progress_callback(AgentProgress(
                        agent_name=name, status="failed", message="超时",
                    ))
        results.extend(batch_results)

    # Build agent reports section
    agent_reports = ""
    success_count = 0
    failed_count = 0
    for name, result in results:
        if result.startswith("[错误]"):
            failed_count += 1
            continue
        success_count += 1
        role_label = AGENT_ROLE_PROMPTS.get(name, "").split("\n")[0].replace("你是", "").replace("你是一位", "")
        if not role_label:
            role_label = name
        agent_reports += f"\n\n---\n## {role_label}\n\n{result}\n"

    if success_count == 0:
        return f"# AI Berkshire 投研报告\n\n**目标**：{arguments}\n\n## 错误\n\n所有 {failed_count} 个 Agent 均执行失败，无法生成报告。\n\n请检查 LLM API 配置和网络连接。"

    # Phase 2: Team Lead synthesis (with context)
    agent_label = f"{success_count} 位分析师"
    if failed_count > 0:
        agent_label += f"（{failed_count} 个 Agent 失败，已排除）"
    synthesis_prompt = TEAM_LEAD_PROMPT
    if context:
        synthesis_prompt += f"\n\n{context}"
    synthesis_prompt += f"\n\n以下是{agent_label}的独立研究报告：\n{agent_reports}"
    synthesis = await chat_complete(synthesis_prompt, f"请综合以上报告，输出最终投研报告。目标：{arguments}", llm_config=llm_config)

    # Phase 3: Post-synthesis agents
    post_synthesis_output = ""
    if post_synthesis_agents:
        for agent_name in post_synthesis_agents:
            if progress_callback:
                await progress_callback(AgentProgress(
                    agent_name=agent_name,
                    status="running",
                    message="处理中...",
                ))
            try:
                role_prompt = AGENT_ROLE_PROMPTS.get(agent_name, "你是一位专业的财经编辑。")
                # Inject real-world context into post-synthesis agents too
                if context:
                    role_prompt = f"{role_prompt}\n\n{context}"
                post_input = f"以下是投研团队的综合研究报告，请基于此进行你的工作：\n\n{synthesis}"
                post_result = await chat_complete(role_prompt, post_input, llm_config=llm_config)
                post_synthesis_output += f"\n\n---\n## {agent_name}\n\n{post_result}\n"
                if progress_callback:
                    await progress_callback(AgentProgress(
                        agent_name=agent_name,
                        status="completed",
                        message="完成",
                        progress=1.0,
                    ))
            except Exception as e:
                logger.warning(f"Post-synthesis agent {agent_name} failed: {e}")
                post_synthesis_output += f"\n\n---\n## {agent_name}\n\n[错误] {str(e)}\n"
                if progress_callback:
                    await progress_callback(AgentProgress(
                        agent_name=agent_name,
                        status="failed",
                        message=str(e),
                    ))

    # Build final report
    all_agent_count = len(agent_names)
    final_report = f"""# AI Berkshire 投研报告

**目标**：{arguments}
**框架**：{skill['display_name']}
**执行模式**：{success_count}/{all_agent_count} Agent 成功{'（' + str(failed_count) + ' 个失败）' if failed_count else ''}{'，分阶段执行' if post_synthesis_agents else ''}
**数据源**：实时市场数据 + 投资大师知识库

{synthesis}

---

## 附录：各Agent独立研究报告
{agent_reports}
"""

    if post_synthesis_output:
        final_report += f"\n---\n## 后期处理（编辑/评审）\n{post_synthesis_output}\n"

    if len(final_report) > MAX_REPORT_LENGTH:
        final_report = final_report[:MAX_REPORT_LENGTH] + "\n\n---\n*报告已截断（超过最大长度限制）*\n"

    return final_report


async def run_series(
    skill_name: str,
    arguments: str,
    progress_callback=None,
    llm_config: dict = None,
) -> str:
    """Run a series generation: produce multiple long-form articles sequentially.

    Injects real-world context into each article to ensure accuracy.
    """
    skill = get_skill(skill_name)
    topics = skill.get("series_topics", [])

    if not topics:
        return await run_single_agent(skill_name, arguments, llm_config=llm_config)

    # Build context once
    try:
        context = await build_full_context(arguments, skill_name=skill_name)
    except Exception as e:
        logger.warning(f"Context building failed: {e}")
        context = ""

    total = len(topics)
    articles = []

    for i, topic in enumerate(topics):
        if progress_callback:
            await progress_callback(AgentProgress(
                agent_name=f"article-{i+1}",
                status="running",
                message=f"撰写：{topic}",
            ))

        context_str = ""
        if articles:
            prev_summaries = []
            for j, (t, a) in enumerate(articles):
                excerpt = a[:200].replace("\n", " ") + "..."
                prev_summaries.append(f"第{j+1}篇《{t}》：{excerpt}")
            context_str = f"\n\n## 前文回顾\n" + "\n".join(prev_summaries)

        user_msg = f"""请撰写关于「{arguments}」的深度系列文章的第 {i+1}/{total} 篇。

## 本篇主题
{topic}

## 写作要求
- 3000-5000字
- 数据翔实，来源明确
- 观点鲜明，不打太极
- 公众号级深度和可读性
- 与系列其他篇相互关联{context_str}

请直接输出文章正文，以 Markdown 格式排版。"""

        system_prompt = SERIES_SYSTEM_PROMPT
        if context:
            system_prompt += f"\n\n{context}"

        try:
            # Use tool-enabled chat so the LLM can actually call get_stock_price etc.
            # The SERIES_SYSTEM_PROMPT tells the LLM it has tools — now we actually provide them.
            article = await asyncio.wait_for(
                chat_complete_with_tools(
                    system_prompt, user_msg,
                    tools=ALL_TOOL_SCHEMAS,
                    tool_executor=_execute_tool,
                    llm_config=llm_config,
                ),
                timeout=AGENT_TIMEOUT,
            )
            articles.append((topic, article))

            if progress_callback:
                await progress_callback(AgentProgress(
                    agent_name=f"article-{i+1}",
                    status="completed",
                    message=f"第{i+1}/{total}篇完成",
                    progress=(i + 1) / total,
                ))
        except asyncio.TimeoutError:
            articles.append((topic, f"[错误] 第{i+1}篇生成超时"))
            if progress_callback:
                await progress_callback(AgentProgress(
                    agent_name=f"article-{i+1}",
                    status="failed",
                    message="超时",
                ))
        except Exception as e:
            articles.append((topic, f"[错误] {str(e)}"))
            if progress_callback:
                await progress_callback(AgentProgress(
                    agent_name=f"article-{i+1}",
                    status="failed",
                    message=str(e),
                ))

    success_count = sum(1 for _, a in articles if not a.startswith("[错误]"))
    failed_count = total - success_count

    report_parts = [
        f"# 深度系列长文：{arguments}\n",
        f"**框架**：{skill['display_name']}",
        f"**执行模式**：系列生成（{success_count}/{total} 篇成功{f'，{failed_count} 篇失败' if failed_count else ''}）",
        f"**数据源**：实时市场数据 + 投资大师知识库\n",
    ]

    for i, (topic, article) in enumerate(articles):
        report_parts.append(f"\n---\n\n## 第{i+1}篇：{topic}\n")
        report_parts.append(article)
        report_parts.append("")

    if success_count > 0:
        report_parts.append("\n---\n\n## 系列总结\n")
        report_parts.append(f"以上 {success_count} 篇文章构成了关于「{arguments}」的完整深度分析系列。"
                             "每篇文章独立成文，同时相互关联，形成对该公司的全方位深度解读。")

    final_report = "\n".join(report_parts)

    # Series reports use a larger length limit (8 articles × 3000-5000 words each)
    if len(final_report) > MAX_SERIES_REPORT_LENGTH:
        final_report = final_report[:MAX_SERIES_REPORT_LENGTH] + "\n\n---\n*报告已截断（超过最大长度限制）*\n"

    return final_report
