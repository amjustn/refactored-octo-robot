"""Sector Router — 板块动态识别器（双通道）。

「当分析某个板块（或属于某板块的公司）时，模型该调用哪份板块知识」
由本模块回答。双通道设计：

通道① 确定性快路径（零 LLM 开销）：
  a. 别名关键词匹配 —— arguments + 公司名 命中板块别名（长别名优先）
  b. 个股 → 行业映射（东财/巨潮公司资料 → 证监会分类 → 板块 key）
通道② LLM 兜底：快路径无命中时，让模型从 31 个板块注册表里判断
  「这是什么板块、该调哪份知识」，可返回 0~2 个；返回空 = 诚实不注入。

防误配规则：
  - 别名长度 <2 不参与自动匹配（"铜"/"铝" 这类单字误伤面大）
  - "综合" 不参与自动匹配（"综合分析" 是高频词，会误命中综合板块）
  - 单字板块（zonghe）只能靠 LLM 显式判断
  - 最多返回 2 个板块（token 预算约束），按匹配强度排序

缓存：个股→行业 24h；文本→板块判定 1h（重复研究同参数不重复烧 LLM）。
"""
import asyncio
import hashlib
import json
import logging
import re
from typing import Optional

from ..core.config import DATA_CACHE_TTL_FINANCIAL
from ..core.data_cache import DataCache
from ..core.llm import chat_complete
from .sector_knowledge import SECTORS

logger = logging.getLogger("ai_berkshire.sector_router")

_cache = DataCache()

_DETECT_CACHE_TTL = 3600          # 文本判定结果缓存 1h
_INDUSTRY_CACHE_TTL = DATA_CACHE_TTL_FINANCIAL  # 个股→行业 24h
_MAX_SECTORS = 2                  # 注入板块数上限（token 预算）
_LLM_TIMEOUT_S = 20.0

# 不参与自动匹配的别名：单字（误伤面大）与高频通用词
_AUTOMATCH_BLOCK = {"综合"}


def _alias_index() -> list:
    """(alias, sector_key) 列表，按别名长度降序（长别名优先命中）。"""
    pairs = []
    for key, meta in SECTORS.items():
        for alias in meta.get("aliases", []):
            a = (alias or "").strip()
            if len(a) < 2 or a in _AUTOMATCH_BLOCK:
                continue
            pairs.append((a, key))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


_ALIAS_PAIRS = _alias_index()


def _alias_match(text: str, max_sectors: int = _MAX_SECTORS) -> list:
    """文本别名匹配 → [(sector_key, matched_alias)]，按匹配强度排序去重。"""
    if not text:
        return []
    hits = []  # (alias_len, first_pos, key, alias)
    for alias, key in _ALIAS_PAIRS:
        pos = text.find(alias)
        if pos >= 0:
            hits.append((len(alias), -pos, key, alias))
    hits.sort(reverse=True)
    out, seen = [], set()
    for _, _, key, alias in hits:
        if key in seen:
            continue
        seen.add(key)
        out.append((key, alias))
        if len(out) >= max_sectors:
            break
    return out


# ── 证监会行业分类 → 板块 key 映射 ─────────────────────────────
# 巨潮 stock_profile_cninfo 返回的「所属行业」是证监会分类（如
# "酒、饮料和精制茶制造业"），与申万口径不同，需显式映射。
# 粒度不足以区分的大类（如"电气机械和器材制造业"同时覆盖家电与
# 电力设备）刻意不映射 —— 交回 LLM 兜底通道判断，宁可缺不可错。
_CSRC_INDUSTRY_MAP = {
    "酒、饮料和精制茶制造业": "shipin", "食品制造业": "shipin",
    "农副食品加工业": "shipin", "烟草制品业": "shipin",
    "纺织业": "fangzhi", "纺织服装、服饰业": "fangzhi",
    "皮革、毛皮、羽毛及其制品和制鞋业": "fangzhi",
    "造纸和纸制品业": "qinggong", "家具制造业": "qinggong",
    "文教、工美、体育和娱乐用品制造业": "qinggong", "印刷和记录媒介复制业": "qinggong",
    "医药制造业": "yiyao", "卫生": "yiyao",
    "专用设备制造业": "jixie", "通用设备制造业": "jixie", "仪器仪表制造业": "jixie",
    "汽车制造业": "qiche",
    "铁路、船舶、航空航天和其他运输设备制造业": "guofang",
    "黑色金属冶炼和压延加工业": "gangtie", "金属制品业": "gangtie",
    "有色金属冶炼和压延加工业": "youse",
    "非金属矿物制品业": "jiancai",
    "化学原料和化学制品制造业": "huagong", "化学纤维制造业": "huagong",
    "橡胶和塑料制品业": "huagong",
    "石油加工、炼焦和核燃料加工业": "shiyou", "石油和天然气开采业": "shiyou",
    "煤炭开采和洗选业": "meitan",
    "电力、热力生产和供应业": "gongyong", "燃气生产和供应业": "gongyong",
    "水的生产和供应业": "gongyong",
    "货币金融服务": "yinhang",
    "资本市场服务": "feiyin", "保险业": "feiyin", "其他金融业": "feiyin",
    "房地产业": "fangdichan",
    "房屋建筑业": "jianzhu", "土木工程建筑业": "jianzhu",
    "建筑安装业": "jianzhu", "建筑装饰和其他建筑业": "jianzhu",
    "零售业": "shangmao", "批发业": "shangmao",
    "住宿业": "shehui", "餐饮业": "shehui", "商务服务业": "shehui", "教育": "shehui",
    "航空运输业": "jiaotong", "铁路运输业": "jiaotong", "道路运输业": "jiaotong",
    "水上运输业": "jiaotong", "装卸搬运和运输代理业": "jiaotong",
    "仓储业": "jiaotong", "邮政业": "jiaotong",
    "电信、广播电视和卫星传输服务": "tongxin",
    "互联网和相关服务": "jisuanji", "软件和信息技术服务业": "jisuanji",
    "计算机、通信和其他电子设备制造业": "dianzi",
    "广播、电视、电影和录音制作业": "chuanmei", "新闻出版业": "chuanmei",
    "农业": "nonglin", "林业": "nonglin", "畜牧业": "nonglin",
    "渔业": "nonglin", "农、林、牧、渔服务业": "nonglin",
    "生态保护和环境治理业": "huanbao", "废弃资源综合利用业": "huanbao",
}


def _industry_of_symbol(symbol: str) -> str:
    """个股 → 板块 key，24h 缓存。降级链：东财 akshare → 巨潮公司资料（本机主力）。

    返回板块 key（如 'shipin'）或 ''。同步阻塞调用，调用方需放线程里跑。
    """
    code = (symbol or "").strip().upper()
    m = re.match(r"^(\d{6})(\.(SH|SZ))?$", code)
    if not m:
        return ""
    code6 = m.group(1)
    cached = _cache.get("sector_industry", code6, max_age_seconds=_INDUSTRY_CACHE_TTL)
    if cached:
        return str(cached)
    sector = ""
    csrc_name = ""
    # ① 东财个股信息（其他部署环境可用；本机被断连时静默降级）
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=code6)
        if df is not None and not df.empty:
            row = df[df["item"] == "行业"]
            if not row.empty:
                csrc_name = str(row.iloc[0]["value"] or "").strip()
    except Exception as e:
        logger.debug(f"东财行业查询失败 {code6}: {e}")
    # ② 巨潮公司资料（本机主力通道，与公告链同源，实测可用）
    if not csrc_name:
        try:
            import akshare as ak
            df = ak.stock_profile_cninfo(symbol=code6)
            if df is not None and not df.empty:
                csrc_name = str(df.iloc[0].get("所属行业") or "").strip()
        except Exception as e:
            logger.debug(f"巨潮行业查询失败 {code6}: {e}")
    if csrc_name:
        sector = _CSRC_INDUSTRY_MAP.get(csrc_name, "")
        if not sector:
            # 未映射/刻意不映射的证监会大类：直接交回 LLM 兜底。
            # 注意不能对行业名做别名匹配——"电气机械和器材制造业"含"机械"，
            # 会把宁德时代/美的误配到机械板块（2026-09-22 实测踩中）。
            logger.debug(f"证监会行业未映射: {code6} {csrc_name!r} -> LLM兜底")
    if sector:
        _cache.set("sector_industry", code6, sector)
    return sector


async def _llm_detect(text: str, llm_config: Optional[dict] = None) -> list:
    """LLM 兜底判定：让模型自己判断「这是什么板块、该调哪份知识」。"""
    catalog = "、".join(f"{k}({v['name']})" for k, v in SECTORS.items())
    system_prompt = (
        "你是 A 股板块分类器。给定一段投研分析目标，判断它最相关的是哪个（或哪几个）板块。\n"
        f"可选板块（key(名称)）：{catalog}\n"
        "规则：\n"
        "1. 只输出 JSON：{\"sectors\": [\"key1\"]} 或 {\"sectors\": []}\n"
        "2. 最多 2 个 key，必须来自上面的列表；按相关度排序\n"
        "3. 目标是公司时，按该公司主营业务所属板块判断\n"
        "4. 拿不准、或与任何板块都无关（如宏观/组合/纯概念），返回空列表——宁缺毋滥"
    )
    try:
        resp = await asyncio.wait_for(
            chat_complete(system_prompt, f"分析目标：{text[:300]}", temperature=0.2,
                          llm_config=llm_config),
            timeout=_LLM_TIMEOUT_S,
        )
        start = resp.find('{')
        end = resp.rfind('}') + 1
        if start < 0 or end <= start:
            return []
        data = json.loads(resp[start:end])
        keys = [k for k in (data.get("sectors") or []) if k in SECTORS]
        return keys[:_MAX_SECTORS]
    except Exception as e:
        logger.info(f"LLM sector detect failed: {e}")
        return []


async def detect_sectors(arguments: str = "", symbol: str = "",
                         company_name: str = "", llm_config: Optional[dict] = None,
                         max_sectors: int = _MAX_SECTORS) -> dict:
    """识别分析目标所属板块。返回 {"sectors": [key...], "source": ..., "matched": ...}。

    source: alias（别名命中）/ industry（个股行业映射）/ llm（模型判断）/ none。
    任何失败都降级为空结果——不注入板块知识，绝不影响主链路。
    """
    text = " ".join(x for x in [(arguments or "").strip(), (company_name or "").strip()] if x)
    if not text and not symbol:
        return {"sectors": [], "source": "none", "matched": ""}

    # 判定结果缓存（文本+标的粒度）：重复研究不重复判定
    cache_key = hashlib.md5(f"{text}|{symbol}".encode("utf-8")).hexdigest()[:16]
    try:
        cached = _cache.get("sector_detect", cache_key, max_age_seconds=_DETECT_CACHE_TTL)
        if cached and isinstance(cached, dict) and "sectors" in cached:
            return cached
    except Exception:
        pass

    result = {"sectors": [], "source": "none", "matched": ""}

    # ①a 别名匹配（用户原话 + 公司名）
    hits = _alias_match(text, max_sectors)
    if hits:
        result = {"sectors": [k for k, _ in hits], "source": "alias",
                  "matched": ",".join(a for _, a in hits)}

    # ①b 个股 → 行业映射（东财/巨潮 → 证监会分类 → 板块 key）
    if not result["sectors"] and symbol:
        try:
            sector = await asyncio.wait_for(
                asyncio.to_thread(_industry_of_symbol, symbol), timeout=20.0)
        except Exception:
            sector = ""
        if sector in SECTORS:
            result = {"sectors": [sector], "source": "industry", "matched": symbol}

    # ② LLM 兜底：模型自己判断
    if not result["sectors"] and text:
        keys = await _llm_detect(text, llm_config=llm_config)
        if keys:
            result = {"sectors": keys, "source": "llm", "matched": "model-judged"}

    try:
        _cache.set("sector_detect", cache_key, result)
    except Exception:
        pass
    logger.info(
        f"板块识别: {text[:30]!r} -> {result['sectors'] or '无'} "
        f"(source={result['source']}, matched={result['matched']!r})"
    )
    return result
