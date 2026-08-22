"""Skill Knowledge Auto-Updater — Per-skill self-updating knowledge base.

Each of the 21 skills gets its own knowledge file that:
1. Has default (bootstrap) content available on first run
2. Is periodically refreshed via LLM to capture latest frameworks, criteria, and methodologies
3. Gets injected into agent prompts alongside time/market/company context

This ensures ALL capabilities stay current, not just the investment masters' philosophy.

Knowledge files stored in: data/knowledge/skills/{skill_name}.json
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from ..core.config import BASE_DIR
from ..core.llm import chat_complete

logger = logging.getLogger("ai_berkshire.skill_knowledge")

SKILL_KNOWLEDGE_DIR = BASE_DIR / "data" / "knowledge" / "skills"

# How often to refresh skill knowledge (seconds)
SKILL_KNOWLEDGE_REFRESH_INTERVAL = 7 * 86400  # 7 days

# ==================== Default Knowledge for All 21 Skills ====================
# Each skill has domain-specific knowledge that should stay current.
# These defaults ensure agents have useful context from the very first run.

DEFAULT_SKILL_KNOWLEDGE = {
    "investment-research": {
        "skill_name": "investment-research",
        "display_name": "四大师综合分析",
        "latest_frameworks": "7模块分析框架：1)商业模式 2)财务估值 3)行业竞争 4)风险评估 5)管理层 6)估值 7)投资结论",
        "key_criteria": "ROE>15%, 毛利率>40%, 负债率<50%, 自由现金流为正, PE<行业平均",
        "data_sources": "优先级：1)公司年报 2)Wind/Bloomberg 3)akshare/yfinance 4)公开新闻",
        "update_notes": "关注最新会计准则变化对财务指标的影响，以及AI对各行业护城河的重塑",
        "last_updated": "default_bootstrap",
    },
    "investment-team": {
        "skill_name": "investment-team",
        "display_name": "多Agent投研团队",
        "latest_frameworks": "4 Agent并行：商业模式(段永平视角)+财务估值(巴菲特视角)+行业竞争(芒格视角)+风险管理(李录视角)",
        "key_criteria": "每个Agent独立报告，Team Lead综合评分(★1-5)，交叉验证数据偏差<1%",
        "data_sources": "实时行情via get_stock_price工具，财务报表via get_financial_data工具，新闻via fetch_company_news工具",
        "update_notes": "关注多Agent协作模式下的信息冗余和遗漏问题",
        "last_updated": "default_bootstrap",
    },
    "investment-checklist": {
        "skill_name": "investment-checklist",
        "display_name": "巴菲特Checklist",
        "latest_frameworks": "六关筛选：1)能力圈 2)好生意(差异化+定价权) 3)护城河(品牌/转换成本/网络效应/规模) 4)管理层(诚信+能力) 5)安全边际(>30%) 6)决策纪律",
        "key_criteria": "一票否决制：任何一关不通过即排除，不打太极",
        "data_sources": "公司年报+行业研报+竞争对手对比",
        "update_notes": "关注新兴行业(如AI、Web3)对传统护城河定义的挑战",
        "last_updated": "default_bootstrap",
    },
    "earnings-review": {
        "skill_name": "earnings-review",
        "display_name": "财报精读",
        "latest_frameworks": "四维精读：1)核心数据趋势 2)管理层语气变化 3)附注隐藏信息 4)承诺追踪",
        "key_criteria": "关注：营收增速放缓、毛利率变化、经营性vs净利润差异、应收账款异常、存货周转、商誉减值风险",
        "data_sources": "原始财报PDF+交易所公告+业绩说明会纪要",
        "update_notes": "关注最新收入确认准则(IFRS 15/CAS 14)和租赁准则对财报的影响",
        "last_updated": "default_bootstrap",
    },
    "earnings-team": {
        "skill_name": "earnings-team",
        "display_name": "财报精读团队",
        "latest_frameworks": "4研究Agent+编辑+评审分阶段执行，产出可发布文章",
        "key_criteria": "生意本质(段永平)+财务质量(巴菲特)+竞争变化(芒格)+风险信号(李录)",
        "data_sources": "实时财报数据+历史对比+同业benchmark",
        "update_notes": "关注非经常性损益识别和一次性减值的处理方式更新",
        "last_updated": "default_bootstrap",
    },
    "industry-research": {
        "skill_name": "industry-research",
        "display_name": "产业链全景扫描",
        "latest_frameworks": "上中下游拆解：1)原材料 2)中间品 3)成品 4)渠道 5)终端，每环节标注价值分配%",
        "key_criteria": "市场规模>100亿，增速>GDP增速2倍，集中度CR5>40%",
        "data_sources": "行业协会数据+国家统计局+上市公司年报拼接",
        "update_notes": "关注全球供应链重构、地缘政治对产业链的影响",
        "last_updated": "default_bootstrap",
    },
    "industry-funnel": {
        "skill_name": "industry-funnel",
        "display_name": "行业漏斗筛选",
        "latest_frameworks": "四级漏斗：30-60家→粗筛(财务+治理)≤10家→精细分析(护城河+成长)→终选3家(组合互补)",
        "key_criteria": "粗筛红线：连续3年亏损、审计非标、实控人质押>80%、商誉>净资产50%",
        "data_sources": "全市场筛选via akshare/yfinance + 手动行业分类",
        "update_notes": "关注注册制下的退市风险和壳价值归零趋势",
        "last_updated": "default_bootstrap",
    },
    "quality-screen": {
        "skill_name": "quality-screen",
        "display_name": "去劣筛选",
        "latest_frameworks": "7条硬指标：1)ROE>15% 2)FCF>0 3)利息覆盖>5倍 4)毛利率>30% 5)利润质量(经营现金流/净利润>80%) 6)净利率>10% 7)无大幅稀释",
        "key_criteria": "任一指标不达标即标记，不打分直接淘汰",
        "data_sources": "财务报表数据via get_financial_data工具",
        "update_notes": "关注不同行业的指标基准差异（如银行不适用毛利率指标）",
        "last_updated": "default_bootstrap",
    },
    "bottleneck-hunter": {
        "skill_name": "bottleneck-hunter",
        "display_name": "瓶颈猎手",
        "latest_frameworks": "Layer 0-4拆解：0)趋势 1)产业链 2)物理瓶颈(材料/工艺/产能) 3)时间窗口 4)套利机会",
        "key_criteria": "瓶颈确认标准：不可替代性+供需缺口>20%+解决时间>2年",
        "data_sources": "行业专家访谈+产能数据+专利分析",
        "update_notes": "关注AI算力(HBM/CoWoS/液冷)、能源转型(锂/钴/稀土)的物理瓶颈",
        "last_updated": "default_bootstrap",
    },
    "portfolio-review": {
        "skill_name": "portfolio-review",
        "display_name": "组合审视",
        "latest_frameworks": "五维体检：1)仓位分布 2)集中度(Herfindahl指数) 3)机会成本 4)压力测试 5)调仓建议",
        "key_criteria": "单一持仓>30%需强力论点，行业集中度<60%，现金比例5-20%",
        "data_sources": "用户输入持仓+实时价格via get_stock_price",
        "update_notes": "关注极端市场环境下的相关性上升问题(尾部风险)",
        "last_updated": "default_bootstrap",
    },
    "thesis-tracker": {
        "skill_name": "thesis-tracker",
        "display_name": "持仓评估",
        "latest_frameworks": "买入论文纪律系统：1)核心假设清单 2)红线触发(3条) 3)估值季度更新 4)健康度评分(绿/黄/红)",
        "key_criteria": "核心假设验证率<60%触发减仓，红线触发立即清仓",
        "data_sources": "公司公告+财报+行业数据持续追踪",
        "update_notes": "关注买入论点失效的早期信号识别方法",
        "last_updated": "default_bootstrap",
    },
    "news-pulse": {
        "skill_name": "news-pulse",
        "display_name": "股价异动归因",
        "latest_frameworks": "4维并行侦察：1)公司事件 2)监管政策 3)行业对手 4)市场情绪，输出事件时间线",
        "key_criteria": "区分公司Alpha事件vs行业Beta波动，标注与股价异动相关性(高/中/低)",
        "data_sources": "实时新闻via fetch_company_news + 交易所公告 + 社交媒体情绪",
        "update_notes": "关注做空报告、集体诉讼、SEC/CSRC调查等重大风险事件",
        "last_updated": "default_bootstrap",
    },
    "management-deep-dive": {
        "skill_name": "management-deep-dive",
        "display_name": "管理层深研",
        "latest_frameworks": "五维评估：1)能力圈 2)诚信度 3)资本配置 4)战略眼光 5)历史决策复盘",
        "key_criteria": "诚信度一票否决：关联交易、信披违规、承诺不兑现=不信任",
        "data_sources": "年报管理层讨论+股东大会纪要+历史决策追踪+公开访谈",
        "update_notes": "关注创始人接班、核心团队离职等治理风险信号",
        "last_updated": "default_bootstrap",
    },
    "private-company-research": {
        "skill_name": "private-company-research",
        "display_name": "未上市公司研究",
        "latest_frameworks": "侦探式拼凑：1)工商信息 2)融资历史 3)估值变化 4)业务数据(多源) 5)竞品对比 6)置信度标注",
        "key_criteria": "数据来源≥3个独立来源交叉验证，置信度标注(高/中/低)",
        "data_sources": "天眼查/企查查+Crunchbase+行业报告+招聘数据+App流量",
        "update_notes": "关注Pre-IPO公司的财务数据可得性和审计可信度",
        "last_updated": "default_bootstrap",
    },
    "deep-company-series": {
        "skill_name": "deep-company-series",
        "display_name": "深度系列长文",
        "latest_frameworks": "8篇系列：1)公司简史 2)商业模式 3)护城河 4)财务十年 5)管理层 6)竞争格局 7)估值 8)结论",
        "key_criteria": "每篇3000-5000字，数据来源明确，观点鲜明，公众号级深度",
        "data_sources": "实时行情+财务数据+新闻+历史年报，全部通过工具获取",
        "update_notes": "关注长文写作中的信息密度与可读性平衡",
        "last_updated": "default_bootstrap",
    },
    "dyp-ask": {
        "skill_name": "dyp-ask",
        "display_name": "段永平问答",
        "latest_frameworks": "段永平思维模型：1)好生意(差异化+定价权) 2)本分文化 3)用户导向 4)长期主义 5)Stop doing list",
        "key_criteria": "投资判断标准：懂的生意+好生意+好价格+长期持有",
        "data_sources": "段永平雪球发言+步步高/OPPO/苹果投资案例",
        "update_notes": "关注段永平最新雪球发言和投资观点更新",
        "last_updated": "default_bootstrap",
    },
    "financial-data": {
        "skill_name": "financial-data",
        "display_name": "财务数据规范",
        "latest_frameworks": "数据获取规范：1)数据源优先级 2)误差计算(<1%PASS) 3)来源标注 4)时效性检查 5)本福特定律检测",
        "key_criteria": "关键指标必须≥2源交叉验证，偏差>5%标记FAIL",
        "data_sources": "A股:akshare / 美股港股:yfinance / 官方年报为最终基准",
        "update_notes": "关注最新IFRS/US GAAP/CAS准则差异对数据可比性的影响",
        "last_updated": "default_bootstrap",
    },
    "wechat-article": {
        "skill_name": "wechat-article",
        "display_name": "公众号文章",
        "latest_frameworks": "三Agent协作：1)研究员搜集资料 2)编辑改写(段落<4行,每500字小结) 3)读者评审(可读性+信息价值+可信度)",
        "key_criteria": "专业术语用类比解释，数据来源明确，观点有支撑",
        "data_sources": "实时行情+新闻+研究报告，via工具获取",
        "update_notes": "关注公众号内容监管新规和读者偏好变化",
        "last_updated": "default_bootstrap",
    },
    "daily-briefing": {
        "skill_name": "daily-briefing",
        "display_name": "金融日报",
        "latest_frameworks": "A股盘后综述四段式：1)指数表现与量能 2)板块涨跌与轮动 3)资金流向(北向/主力) 4)要闻与次日关注",
        "key_criteria": "数据标注来源与时间，指数涨跌精确到小数点后两位，要闻注明出处，文末附AI免责声明",
        "data_sources": "指数行情via get_indices工具，板块与资金流via web_search，要闻via fetch_company_news/web_search",
        "update_notes": "关注交易制度变化(如涨跌幅限制调整)与北向资金披露口径变化",
        "last_updated": "default_bootstrap",
    },
    "global-macro-analysis": {
        "skill_name": "global-macro-analysis",
        "display_name": "全球宏观分析",
        "latest_frameworks": "3 Agent并行：1)制度与政治经济视角(Acemoglu/Shleifer方法:制度根源+激励+政策可信度) 2)数据驱动预测(FocusEconomics获奖团队/Barraud方法:nowcasting+分项自下而上+点预测带区间+对照一致预期) 3)宏观到资产映射(Berezin/McVey方法:regime识别+price in反推+五类资产+改判路标)",
        "key_criteria": "先框架后结论；点预测必须带置信区间；情景概率合计100%；每个判断注明证伪条件与改判路标(指标+阈值)",
        "data_sources": "高频宏观数据与一致预期via web_search/read_webpage，市场定价via fetch_market_indices",
        "update_notes": "关注主要央行政策框架评估(如美联储框架审查)与一致预期数据源口径变化",
        "last_updated": "default_bootstrap",
    },
    "china-macro-analysis": {
        "skill_name": "china-macro-analysis",
        "display_name": "中国宏观分析",
        "latest_frameworks": "4 Agent并行：1)政策反应函数(彭文生/黄益平/伍戈/邢自强:宽货币vs宽信用+金融周期+政策put/call阈值) 2)高频数据与周期(陆挺/郭磊/陶川·李超/高善文:高频证据链+去季节性+宏微观交叉验证+库存/产能/地产周期定位) 3)汇率与国际收支(管涛/张明/张斌:BOP拆解+利差/结售汇/预期管理+政策工具箱) 4)中长期结构(李迅雷/徐高/樊纲/殷剑峰/苗建军:潜在增长分解+r-g债务算术+结构vs周期拆分)",
        "key_criteria": "区分宽货币与宽信用传导；高频数据先剔除春节错位等季节噪音；结构判断须逻辑闭环(徐高检验)；政策判断须给出触发阈值与证伪条件",
        "data_sources": "政策文件与高频数据via web_search/read_webpage，行情via fetch_market_indices",
        "update_notes": "关注货币政策框架演进(如利率走廊与买卖国债操作)与高频数据口径变化",
        "last_updated": "default_bootstrap",
    },
}


def _get_skill_knowledge_path(skill_name: str) -> Path:
    """Get the JSON file path for a skill's knowledge."""
    return SKILL_KNOWLEDGE_DIR / f"{skill_name}.json"


def _load_skill_knowledge(skill_name: str) -> dict:
    """Load cached knowledge for a skill. Falls back to defaults."""
    path = _get_skill_knowledge_path(skill_name)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Return default knowledge
    default = DEFAULT_SKILL_KNOWLEDGE.get(skill_name, {})
    if default:
        return dict(default)
    return {}


def _save_skill_knowledge(skill_name: str, data: dict):
    """Save knowledge for a skill."""
    SKILL_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    path = _get_skill_knowledge_path(skill_name)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_all_default_knowledge():
    """Ensure default knowledge files exist for all skills on first run."""
    SKILL_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    for skill_name in DEFAULT_SKILL_KNOWLEDGE:
        path = _get_skill_knowledge_path(skill_name)
        if not path.exists():
            default_data = dict(DEFAULT_SKILL_KNOWLEDGE[skill_name])
            default_data["last_updated"] = "default_bootstrap"
            default_data["_is_default"] = True
            _save_skill_knowledge(skill_name, default_data)
            logger.info(f"Created default skill knowledge for: {skill_name}")


def get_skill_knowledge(skill_name: str) -> dict:
    """Get knowledge for a specific skill (synchronous). Always returns data."""
    data = _load_skill_knowledge(skill_name)
    if not data:
        return {
            "skill_name": skill_name,
            "status": "no_knowledge",
            "message": f"暂无{skill_name}的领域知识",
        }
    return data


def get_all_skill_knowledge_status() -> dict:
    """Get status of all skills' knowledge bases."""
    from ..skills import list_skills
    status = {}
    for skill in list_skills():
        name = skill["name"]
        data = _load_skill_knowledge(name)
        status[name] = {
            "display_name": skill.get("display_name", name),
            "initialized": bool(data),
            "last_updated": data.get("last_updated", "never"),
            "is_default": data.get("_is_default", False),
        }
    return status


def get_skill_knowledge_for_prompt(skill_name: str) -> str:
    """Get a skill's knowledge formatted for prompt injection.

    This is injected alongside time/market/company/knowledge context
    so agents have domain-specific frameworks and criteria.
    """
    data = _load_skill_knowledge(skill_name)
    if not data:
        return ""

    parts = []
    if data.get("latest_frameworks"):
        parts.append(f"**分析框架**：{data['latest_frameworks']}")
    if data.get("key_criteria"):
        parts.append(f"**关键标准**：{data['key_criteria']}")
    if data.get("data_sources"):
        parts.append(f"**数据来源**：{data['data_sources']}")
    if data.get("update_notes"):
        parts.append(f"**最新更新**：{data['update_notes']}")

    if not parts:
        return ""

    return f"""## 能力领域知识（{data.get('display_name', skill_name)}）

{chr(10).join(parts)}

*注：此领域知识会定期自动更新，确保分析基于最新框架和标准。*
"""


async def update_skill_knowledge(skill_name: str) -> dict:
    """Update knowledge for a single skill via LLM.

    Uses the LLM to generate updated domain knowledge based on
    current frameworks, criteria, and market conditions.
    """
    from ..skills import get_skill
    skill = get_skill(skill_name)
    if not skill:
        return {"error": f"Unknown skill: {skill_name}"}

    now = datetime.now()

    # Check if update is needed
    existing = _load_skill_knowledge(skill_name)
    if existing and existing.get("last_updated", "") != "default_bootstrap":
        last_updated = existing.get("last_updated", "")
        if last_updated:
            try:
                last_dt = datetime.fromisoformat(last_updated)
                if (now - last_dt).total_seconds() < SKILL_KNOWLEDGE_REFRESH_INTERVAL:
                    logger.info(f"Skill knowledge for {skill_name} is recent, skipping")
                    return existing
            except Exception:
                pass

    display_name = skill.get("display_name", skill_name)
    description = skill.get("description", "")
    existing_data = existing or DEFAULT_SKILL_KNOWLEDGE.get(skill_name, {})

    logger.info(f"Updating skill knowledge for: {skill_name} ({display_name})...")

    system_prompt = f"""你是一位投资研究方法论专家，负责更新分析能力的领域知识。

你正在更新能力：「{display_name}」
描述：{description}

当前时间：{now.strftime('%Y年%m月%d日')}

请基于你的知识库，结合最新的投资分析实践，更新该分析能力的领域知识。

输出格式为 JSON：
{{
    "skill_name": "{skill_name}",
    "display_name": "{display_name}",
    "latest_frameworks": "最新的分析框架和方法论（200-300字）",
    "key_criteria": "关键判断标准和筛选规则（100-200字）",
    "data_sources": "推荐的数据来源和获取方式（100字）",
    "update_notes": "最新的方法论更新要点和注意事项（100-200字）",
    "last_updated": "{now.isoformat()}"
}}

请确保内容准确、实用，能直接指导投资分析。"""

    try:
        response = await chat_complete(system_prompt, f"请更新「{display_name}」的领域知识库。")

        # Try to parse JSON from response
        try:
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(response[json_start:json_end])
                data["last_updated"] = now.isoformat()
                # Ensure required fields exist — fall back to existing/default
                for field in ["latest_frameworks", "key_criteria", "data_sources", "update_notes"]:
                    if not data.get(field):
                        data[field] = existing_data.get(field, "")
                data["skill_name"] = skill_name
                data["display_name"] = display_name
                _save_skill_knowledge(skill_name, data)
                logger.info(f"Skill knowledge updated for {skill_name}")
                return data
            else:
                raise json.JSONDecodeError("No JSON found", response, 0)
        except json.JSONDecodeError:
            # LLM didn't return valid JSON — preserve existing structured data
            # and store raw response for debugging. This prevents skill knowledge
            # from silently disappearing after a bad LLM update.
            data = dict(existing_data)  # Start with existing/default data
            data["skill_name"] = skill_name
            data["display_name"] = display_name
            data["raw_response"] = response[:2000]
            data["last_updated"] = now.isoformat()
            data["_update_failed"] = True
            _save_skill_knowledge(skill_name, data)
            logger.warning(f"Skill knowledge update for {skill_name} produced non-JSON, preserved existing data")
            return data

    except Exception as e:
        logger.error(f"Failed to update skill knowledge for {skill_name}: {e}")
        if existing:
            existing["error"] = str(e)
            return existing
        return {"error": str(e)}


async def update_all_skill_knowledge() -> dict:
    """Update knowledge for all skills."""
    from ..skills import list_skills
    skills = list_skills()
    tasks = [update_skill_knowledge(s["name"]) for s in skills]
    completed = await asyncio.gather(*tasks, return_exceptions=True)

    results = {}
    for skill, result in zip(skills, completed):
        name = skill["name"]
        if isinstance(result, Exception):
            results[name] = {"error": str(result)}
        else:
            results[name] = result

    logger.info(f"Skill knowledge update complete for {len(results)} skills")
    return results
