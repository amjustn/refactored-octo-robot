"""Goal parser — extract structured hints from free-form analysis goals.

When the user's ``arguments`` is a long free-form goal (not just a company
name / stock code), an LLM call extracts:
- symbol:       stock symbol like 0700.HK / AAPL / 600519.SH (validated)
- company:      Chinese/English company name
- search_query: concise web search query capturing the core topic
- keywords:     3-6 key terms
- intent:       one-sentence summary of what the user wants

Short inputs (< MIN_GOAL_LEN chars) keep the legacy behavior — no LLM call.
Every failure mode (timeout, LLM error, unparseable JSON, junk values)
degrades gracefully to None so context building is never broken.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from .llm import chat_complete

logger = logging.getLogger("ai_berkshire.goal_parser")

MIN_GOAL_LEN = 25          # shorter inputs keep legacy symbol-extraction behavior
PARSE_TIMEOUT_S = 20.0     # hard cap on the LLM parse call

# Same shape as the legacy regex in core.context.build_full_context, plus
# HK numeric tickers (0700.HK / 9988.HK are 1-5 digits, not 6)
_SYMBOL_RE = re.compile(r"^\d{6}(\.(SH|SZ))?$|^\d{1,5}\.HK$|^[A-Za-z]{1,6}(\.HK)?$")

_SYSTEM_PROMPT = (
    "你是投研助手的信息提取器。从用户的分析目标中提取结构化信息。"
    "只输出一个严格的 JSON 对象，不要输出任何其他文字、解释或 markdown 代码块标记。"
)

_USER_TEMPLATE = """从以下用户输入中提取信息，只输出严格 JSON：

用户输入：
\"\"\"{goal}\"\"\"

输出格式（严格遵守，只输出 JSON，不要输出其他任何内容）：
{{
  "symbol": "股票代码，如 0700.HK / AAPL / 600519.SH；无法确定则为 null",
  "company": "公司名（中文或英文）；无法确定则为 null",
  "search_query": "用于网络搜索的简洁查询，概括用户的核心主题",
  "keywords": ["3-6个关键术语"],
  "intent": "一句话概括用户想要什么"
}}"""


def _strip_fences(text: str) -> str:
    """Remove markdown code fences if the model wrapped the JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _sanitize(data) -> Optional[dict]:
    """Keep only sane values; validate the symbol format. None if empty."""
    if not isinstance(data, dict):
        return None

    out: dict = {}

    symbol = data.get("symbol")
    if isinstance(symbol, str):
        symbol = symbol.strip()
        out["symbol"] = symbol if symbol and _SYMBOL_RE.match(symbol) else None
    else:
        out["symbol"] = None

    for key in ("company", "search_query", "intent"):
        val = data.get(key)
        if isinstance(val, str):
            val = val.strip()
            out[key] = val or None
        else:
            out[key] = None

    keywords = data.get("keywords")
    if isinstance(keywords, (list, tuple)):
        out["keywords"] = [str(k).strip() for k in keywords if str(k).strip()][:6]
    else:
        out["keywords"] = []

    if not out["symbol"] and not out["company"] and not out["search_query"] \
            and not out["keywords"] and not out["intent"]:
        return None
    return out


async def parse_goal(arguments: str, llm_config: Optional[dict] = None) -> Optional[dict]:
    """Parse a free-form goal into structured hints. None on any failure.

    Short inputs (< MIN_GOAL_LEN) return None immediately without an LLM
    call so legacy short-form behavior (company map / regex) is untouched.
    """
    goal = (arguments or "").strip()
    if len(goal) < MIN_GOAL_LEN:
        return None

    try:
        raw = await asyncio.wait_for(
            chat_complete(
                system_prompt=_SYSTEM_PROMPT,
                user_message=_USER_TEMPLATE.format(goal=goal[:2000]),
                temperature=0.1,
                llm_config=llm_config,
            ),
            timeout=PARSE_TIMEOUT_S,
        )
    except Exception as e:
        logger.debug(f"parse_goal: LLM call failed: {e}")
        return None

    text = _strip_fences(raw or "")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # Tolerate leading/trailing prose around the JSON object
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except (json.JSONDecodeError, TypeError):
                logger.debug("parse_goal: unparseable JSON from LLM")
                return None
        else:
            logger.debug("parse_goal: unparseable JSON from LLM")
            return None

    hints = _sanitize(data)
    if hints:
        logger.info(
            f"parse_goal: symbol={hints.get('symbol')} company={hints.get('company')} "
            f"query={hints.get('search_query')}"
        )
    return hints
