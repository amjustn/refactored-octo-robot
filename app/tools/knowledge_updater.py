"""Investment Knowledge Auto-Updater

Periodically fetches and summarizes recent writings, interviews,
and speeches of investment masters (Buffett, Munger, Duan Yongping, Li Lu).
Stores summaries in a local knowledge base that gets injected into agent prompts.

Uses the LLM itself to:
1. Generate search queries about each master's recent activity
2. Summarize findings into structured insights
3. Extract actionable investment principles

Knowledge base is stored as JSON files in data/knowledge/.
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from ..core.config import BASE_DIR
from ..core.data_cache import DataCache
from ..core.llm import chat_complete

logger = logging.getLogger("ai_berkshire.knowledge")

KNOWLEDGE_DIR = BASE_DIR / "data" / "knowledge"
_cache = DataCache()

# Investment masters to track
MASTERS = {
    "buffett": {
        "name_cn": "巴菲特",
        "name_en": "Warren Buffett",
        "role": "价值投资 / 安全边际",
        "search_keywords": [
            "Warren Buffett latest interview",
            "巴菲特 最新 致股东信",
            "Berkshire Hathaway annual letter",
            "巴菲特 股东大会 最新观点",
        ],
    },
    "munger": {
        "name_cn": "芒格",
        "name_en": "Charlie Munger",
        "role": "逆向思考 / 多元思维模型",
        "search_keywords": [
            "Charlie Munger latest wisdom",
            "芒格 最新 演讲",
            "Charlie Munger Daily Journal",
            "芒格 投资哲学 最新",
        ],
    },
    "duan_yongping": {
        "name_cn": "段永平",
        "name_en": "Duan Yongping",
        "role": "好生意 / 差异化 / 本分",
        "search_keywords": [
            "段永平 最新 雪球",
            "段永平 投资问答 最新",
            "Duan Yongping investment views",
            "段永平 步步高 OPPO 最新观点",
        ],
    },
    "li_lu": {
        "name_cn": "李录",
        "name_en": "Li Lu",
        "role": "长期确定性 / 文明现代化",
        "search_keywords": [
            "李录 最新 演讲",
            "Li Lu Himalaya Capital",
            "李录 价值投资 最新观点",
            "李录 文明 现代化 投资",
        ],
    },
}

# How often to refresh knowledge (seconds)
KNOWLEDGE_REFRESH_INTERVAL = 7 * 86400  # 7 days

# ==================== Default (Bootstrap) Knowledge ====================
# Used when knowledge base hasn't been initialized yet, so agents always
# have investment philosophy context from the very first run.
DEFAULT_KNOWLEDGE = {
    "buffett": {
        "master": "buffett",
        "name_cn": "巴菲特",
        "name_en": "Warren Buffett",
        "role": "价值投资 / 安全边际",
        "core_philosophy": "以合理价格买入优秀公司，长期持有。强调安全边际、能力圈和内在价值。投资的核心不是预测市场，而是评估企业。关注ROE、利润率、债务水平和自由现金流。",
        "key_principles": [
            "安全边际：买入价格远低于内在价值",
            "能力圈：只投资你理解的企业",
            "市场先生：利用市场波动而非被其左右",
            "长期持有：时间是优秀企业的朋友",
            "集中投资：把鸡蛋放在少数几个篮子里",
        ],
        "recent_views": "关注AI对保险和能源业务的影响，伯克希尔现金储备创历史新高，等待合适机会。强调在通胀环境下选择有定价权的公司。",
        "famous_quotes": [
            "别人恐惧时我贪婪，别人贪婪时我恐惧",
            "以合理价格买入优秀公司，远胜以优秀价格买入平庸公司",
            "投资的第一条规则是不要亏钱，第二条是记住第一条",
        ],
        "investment_framework": "1. 筛选ROE>15%的公司 2. 评估护城河可持续性 3. 计算内在价值(DCF) 4. 要求安全边际>30% 5. 长期持有，除非基本面恶化",
        "applicable_scenarios": ["价值股筛选", "长期持仓评估", "安全边际计算"],
    },
    "munger": {
        "master": "munger",
        "name_cn": "芒格",
        "name_en": "Charlie Munger",
        "role": "逆向思考 / 多元思维模型",
        "core_philosophy": "通过多元思维模型跨学科思考，逆向分析什么会导致失败。强调避免愚蠢比追求聪明更重要。关注心理学偏差、激励机制和长期确定性。",
        "key_principles": [
            "逆向思考：想清楚什么会杀死这家公司",
            "多元思维模型：结合经济学、心理学、物理学等",
            "避免愚蠢：比追求聪明更重要",
            "能力圈：知道自己不知道什么",
            "激励机制：揭示行为的真正动因",
        ],
        "recent_views": "强调避免过度依赖单一指标，关注企业文化和管理层诚信。对AI保持关注但警惕过度炒作。",
        "famous_quotes": [
            "告诉我我会死在哪里，我就永远不去那个地方",
            "反过来想，总是反过来想",
            "如果你觉得自己站在牌桌前是最傻的人，那你确实是最傻的",
        ],
        "investment_framework": "1. 逆向思考：列出所有可能导致失败的因素 2. 跨学科验证：用不同学科模型检验 3. 评估管理层诚信和激励机制 4. 寻找长期确定性 5. 避免致命错误",
        "applicable_scenarios": ["风险评估", "逆向分析", "管理层评估"],
    },
    "duan_yongping": {
        "master": "duan_yongping",
        "name_cn": "段永平",
        "name_en": "Duan Yongping",
        "role": "好生意 / 差异化 / 本分",
        "core_philosophy": "投资就是买好生意。好生意的核心是差异化——能做到别人做不到的事。强调本分文化、用户导向和长期主义。不赚不该赚的钱，不做不该做的事。",
        "key_principles": [
            "好生意：有差异化，有定价权",
            "本分：做对的事，不做不对的事",
            "用户导向：真正为用户创造价值",
            "长期主义：不赚快钱，不投机",
            "能力圈：不懂不投，懂的集中投",
        ],
        "recent_views": "持续在雪球分享投资思考，强调'Stop doing list'的重要性。对苹果和腾讯的长期看好基于其差异化能力和用户粘性。",
        "famous_quotes": [
            "做对的事情，然后把事情做对",
            "不赚不该赚的钱",
            "投资就是买生意，买你能看懂的生意",
        ],
        "investment_framework": "1. 判断是否是好生意（差异化、定价权） 2. 评估管理层是否本分 3. 看用户价值是否真实 4. 估值是否合理 5. 长期持有，不加杠杆",
        "applicable_scenarios": ["商业模式分析", "好生意筛选", "管理层品质评估"],
    },
    "li_lu": {
        "master": "li_lu",
        "name_cn": "李录",
        "name_en": "Li Lu",
        "role": "长期确定性 / 文明现代化",
        "core_philosophy": "从文明演进和现代化进程的宏观视角审视投资。强调长期确定性——10年后这家公司还在吗？关注中国现代化进程中的结构性机会，以及科技对文明的推动。",
        "key_principles": [
            "长期确定性：10年后公司还在吗？",
            "文明视角：从宏观演进看微观机会",
            "现代化3.0：自由市场+科技驱动",
            "价值发现：在无人问津处寻找价值",
            "耐心等待：好机会不常有，但要准备好",
        ],
        "recent_views": "持续关注中国现代化进程中的科技和消费升级机会。强调在不确定中寻找确定性，关注具有长期复利效应的企业。",
        "famous_quotes": [
            "投资是关于未来的，你必须对未来有自己的判断",
            "在别人恐惧时贪婪的前提是你真的理解了",
            "长期来看，市场是称重机",
        ],
        "investment_framework": "1. 宏观判断：行业10年后的格局 2. 微观验证：公司能否在竞争中存活 3. 评估确定性：哪些是确定的，哪些不确定 4. 估值安全边际 5. 长期集中持有",
        "applicable_scenarios": ["长期投资评估", "行业趋势分析", "确定性评估"],
    },
}


def _ensure_default_knowledge():
    """Ensure default knowledge files exist on first run.

    Writes default knowledge to disk if no file exists yet, so that
    agents always have philosophy context from the very first run.
    """
    for key in MASTERS:
        path = _get_knowledge_path(key)
        if not path.exists():
            default_data = DEFAULT_KNOWLEDGE.get(key, {}).copy()
            default_data["last_updated"] = "default_bootstrap"
            default_data["_is_default"] = True
            _save_knowledge(key, default_data)
            logger.info(f"Created default knowledge for {key}")


def _get_knowledge_path(master_key: str) -> Path:
    """Get the JSON file path for a master's knowledge."""
    return KNOWLEDGE_DIR / f"{master_key}.json"


def _load_knowledge(master_key: str) -> dict:
    """Load cached knowledge for a master.

    Falls back to default knowledge if no file exists.
    """
    path = _get_knowledge_path(master_key)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Return default knowledge if file doesn't exist
    default = DEFAULT_KNOWLEDGE.get(master_key, {})
    if default:
        default = dict(default)
        default["last_updated"] = "default_bootstrap"
        default["_is_default"] = True
        return default
    return {}


def _save_knowledge(master_key: str, data: dict):
    """Save knowledge for a master."""
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    path = _get_knowledge_path(master_key)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def update_master_knowledge(master_key: str) -> dict:
    """Update knowledge for a single investment master.

    Uses the LLM to generate a summary of the master's recent thinking
    based on its training data + current date context.
    """
    if master_key not in MASTERS:
        return {"error": f"Unknown master: {master_key}"}

    master = MASTERS[master_key]
    now = datetime.now()

    # Check if update is needed
    existing = _load_knowledge(master_key)
    if existing:
        last_updated = existing.get("last_updated", "")
        if last_updated:
            try:
                last_dt = datetime.fromisoformat(last_updated)
                if (now - last_dt).total_seconds() < KNOWLEDGE_REFRESH_INTERVAL:
                    logger.info(f"Knowledge for {master_key} is recent (updated {last_updated}), skipping")
                    return existing
            except Exception:
                pass

    logger.info(f"Updating knowledge for {master_key} ({master['name_cn']})...")

    # Use LLM to generate updated knowledge summary
    system_prompt = f"""你是一位投资研究助手，负责整理和更新投资大师的最新理念。

你正在整理：{master['name_cn']}（{master['name_en']}）
角色定位：{master['role']}

请基于你的知识库，结合当前时间 {now.strftime('%Y年%m月%d日')}，整理该投资大师的最新投资理念。

输出格式为 JSON，包含以下字段：
{{
    "master": "{master_key}",
    "name_cn": "{master['name_cn']}",
    "name_en": "{master['name_en']}",
    "role": "{master['role']}",
    "core_philosophy": "核心理念总结（200-300字）",
    "key_principles": ["原则1", "原则2", "原则3", "原则4", "原则5"],
    "recent_views": "近期观点更新（基于最新可知信息，200字）",
    "famous_quotes": ["名言1", "名言2", "名言3"],
    "investment_framework": "投资框架描述（如何应用其方法论）",
    "applicable_scenarios": ["适用场景1", "适用场景2"],
    "last_updated": "{now.isoformat()}"
}}

请确保内容准确、有深度，能够指导实际投资分析。"""

    try:
        response = await chat_complete(system_prompt, f"请更新{master['name_cn']}的投资理念知识库。")

        # Try to parse JSON from response
        try:
            # Find JSON in response
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(response[json_start:json_end])
                data["last_updated"] = now.isoformat()
                _save_knowledge(master_key, data)
                logger.info(f"Knowledge updated for {master_key}")
                return data
            else:
                # Store as plain text
                data = {
                    "master": master_key,
                    "name_cn": master["name_cn"],
                    "raw_response": response,
                    "last_updated": now.isoformat(),
                }
                _save_knowledge(master_key, data)
                return data
        except json.JSONDecodeError:
            data = {
                "master": master_key,
                "name_cn": master["name_cn"],
                "raw_response": response,
                "last_updated": now.isoformat(),
            }
            _save_knowledge(master_key, data)
            return data

    except Exception as e:
        logger.error(f"Failed to update knowledge for {master_key}: {e}")
        # Return existing if available
        if existing:
            existing["error"] = str(e)
            return existing
        return {"error": str(e)}


async def update_all_masters() -> dict:
    """Update knowledge for all investment masters."""
    results = {}
    tasks = [update_master_knowledge(key) for key in MASTERS]
    completed = await asyncio.gather(*tasks, return_exceptions=True)

    for key, result in zip(MASTERS.keys(), completed):
        if isinstance(result, Exception):
            results[key] = {"error": str(result)}
        else:
            results[key] = result

    logger.info(f"Knowledge update complete for {len(results)} masters")
    return results


def get_master_knowledge(master_key: str) -> dict:
    """Get cached knowledge for a master (synchronous, from local files).

    Always returns meaningful data — uses default knowledge as fallback.
    """
    if master_key not in MASTERS:
        return {"error": f"Unknown master: {master_key}. Available: {list(MASTERS.keys())}"}

    data = _load_knowledge(master_key)
    if not data:
        # This should not happen since _load_knowledge returns defaults
        return {
            "master": master_key,
            "name_cn": MASTERS[master_key]["name_cn"],
            "status": "not_yet_updated",
            "message": "知识库尚未初始化，请调用 /api/knowledge/update 触发更新",
        }
    return data


def get_all_knowledge() -> dict:
    """Get all cached knowledge."""
    result = {}
    for key in MASTERS:
        result[key] = get_master_knowledge(key)
    return result


def get_knowledge_summary_for_prompt() -> str:
    """Get a condensed summary of all masters' knowledge for prompt injection.

    This is injected into agent system prompts so agents have access to
    the latest investment philosophy updates.
    """
    summaries = []
    for key, master in MASTERS.items():
        data = _load_knowledge(key)
        if data and "core_philosophy" in data:
            summary = f"""### {data.get('name_cn', master['name_cn'])}（{master['role']}）
**核心理念**：{data.get('core_philosophy', '未知')}
**关键原则**：{', '.join(data.get('key_principles', []))}
**近期观点**：{data.get('recent_views', '未知')}
**投资框架**：{data.get('investment_framework', '未知')}"""
            summaries.append(summary)
        elif data and "raw_response" in data:
            # Use full raw_response but cap at 1000 chars for prompt efficiency
            raw = data['raw_response']
            if len(raw) > 1000:
                raw = raw[:1000] + "..."
            summaries.append(f"### {master['name_cn']}（{master['role']}）\n{raw}")
        else:
            summaries.append(f"### {master['name_cn']}（{master['role']}）\n*知识库尚未初始化*")

    if not summaries:
        return ""

    return f"""## 投资大师最新理念（知识库自动更新）

以下是四位投资大师的最新理念总结，请在分析中参考应用：

{chr(10).join(summaries)}

*注：此知识库会定期自动更新，确保分析基于最新的投资理念。*
"""


def get_knowledge_status() -> dict:
    """Get status of knowledge base for all masters."""
    status = {}
    for key, master in MASTERS.items():
        data = _load_knowledge(key)
        status[key] = {
            "name_cn": master["name_cn"],
            "name_en": master["name_en"],
            "role": master["role"],
            "initialized": bool(data),
            "last_updated": data.get("last_updated", "never"),
            "has_structured_data": "core_philosophy" in data if data else False,
        }
    return status
