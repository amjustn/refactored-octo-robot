"""搜索关键词清洗 + 查询相关性判定（web_search / site_crawler 共用）。

背景（2026-09-13 实测，见 scripts/verify_batch1.py）：
- core/context.build_web_search_context 原先把用户原话整句当搜索词发送，
  例如"澜起科技你给我一个深度报告 2026 最新"。
- web_search 的批次相关度门闸用 re.findall(r"[一-鿿]{2,}") 切 token，中文没
  空格 → 整句话算【一个】token，门闸要求每条结果同时命中 2 个 token →
  搜狗/360/百度/Bing/头条全部 0%，判"无结果"。
  实测：原话 → 无结果、耗时 14.6s；清洗成"澜起科技 2026 最新" → 6 条、2.0s。
  日志累计：源批量拒绝 764 次、"搜索无结果"334 次（07-11 至 09-13）。
- site_crawler 的兜底抓取不过门闸，公司查询注入的"网络最新信息"块实测是
  918 字符的爱范儿/IT之家消费电子新闻（苹果折叠屏、60W 快充、iPhone 18），
  与目标公司 0 相关 —— 纯噪声，还挤占上下文。

本模块只做纯文本处理，不发起网络请求，便于离线自检。
"""
from __future__ import annotations

import re

# ── 搜索词清洗 ────────────────────────────────────────────────
# 祈使/口语短语：出现在用户原话里但没有检索价值，直接剥离
_IMPERATIVE_PATTERNS = [
    r"给我(来)?(一个|一份|一份儿)?", r"帮我", r"帮忙", r"麻烦你?", r"请你?", r"请",
    r"你看一下", r"你看看", r"看一下", r"看看", r"讲讲", r"说一下", r"聊聊",
    r"你觉得", r"我想", r"我要", r"我想要", r"我需要", r"能不能给我",
    r"一个深度报告", r"一份深度报告", r"一个报告", r"一份报告", r"深度报告",
    r"深度研究", r"深度分析", r"研究报告", r"投研报告", r"研究报告",
    r"分析一下", r"研究一下", r"点评一下", r"解读一下", r"总结一下",
    r"怎么样", r"如何", r"还能不能买", r"还能不能", r"还能买", r"还能", r"能不能买", r"能买吗", r"可以买吗", r"可不可以",
    r"是否可以买入", r"是否可以买", r"是否可以", r"能不能",
    r"值不值得", r"该不该", r"要不要", r"好还是不好",
    r"最新的", r"最新", r"现在的", r"目前", r"目前来看", r"现在时点",
    r"这家公司", r"这个公司", r"该公司", r"这只股票", r"这个股票",
    r"给我看", r"帮我看看", r"帮我分析", r"帮我研究",
]
_IMPERATIVE_RE = re.compile("|".join(_IMPERATIVE_PATTERNS))

# 人称代词：做主语的"你/您/我"没有检索价值，整句清洗后可能残留，单独去掉
_PRONOUN_RE = re.compile(r"[你您我咱]")

# 标点/语气词（清洗后头部尾部都要去掉）
_PUNCT_RE = re.compile(r"[，。、；：！？,.;:!?~～\s]+")
# 尾部的语气词与孤立动词（"还能不能买"清掉"还能不能"后会剩一个"买"）
_TAIL_PARTICLES = re.compile(r"(?:[啊呀吧呢嘛哦哟的了吗]|[买卖]|[好差])+$")

# 保留：字母、数字、中文、常见符号（含 _ - & . HK 币种等）
_KEEP_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9\s\.\-_&+/%]")


def clean_search_keyword(text: str) -> str:
    """把用户原话清洗成可检索的关键词。

    '澜起科技你给我一个深度报告' → '澜起科技'
    '002155你看一下还能不能买'   → '002155'
    '商业航天'                   → '商业航天'
    清洗后不足 2 字返回 ''（调用方据此跳过搜索，而不是拿噪声词去搜）。
    """
    if not text:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    s = _IMPERATIVE_RE.sub(" ", s)
    s = _PRONOUN_RE.sub(" ", s)
    s = _KEEP_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = s.strip()
    s = _TAIL_PARTICLES.sub("", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    # 逐词过滤：残留的泛化词/单字没有检索价值（如"002155 还能"→"002155"）
    pieces = [p for p in s.split() if len(p) >= 2 and p not in GENERIC_TOKENS]
    s = " ".join(pieces).strip()
    return s if len(s) >= 2 else ""


# ── 相关性 token 化 ───────────────────────────────────────────
# 泛化词：几乎出现在所有金融查询里，做相关性判定没有区分度
GENERIC_TOKENS = {
    "最新", "分析", "预测", "怎么样", "如何", "给我", "帮我", "你看", "看一下", "一个", "一份",
    "深度", "报告", "研究", "这个", "这家", "可以", "能不能", "是否", "买入", "买吗", "卖出",
    "持有", "情况", "数据", "一下", "请", "麻烦", "谢谢", "问题", "方面", "关于", "现在",
    "目前", "今天", "多少", "怎么", "为什么", "哪些", "什么", "公司", "股票", "个股", "估值",
    "财报", "年报", "半年报", "季报", "报告", "点评", "解读", "值得", "还是", "以及", "但是",
    "如果", "因为", "所以", "而且", "已经", "没有", "不是", "就是", "这个", "那个",
}

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]{2,}")

_LONG_RUN_CUTOFF = 6   # 超过该长度的连续中文串视为"整句"，拆 2-gram

# 泛化单字：整句拆 2-gram 时，含这些字的组合（"度报""技你""个深"）没有区分度，
# 直接丢弃（公司名/行业词一般不含这些字，如"澜起/科技/宁德/时代"都保留）。
GENERIC_CHARS = set(
    "的了呢吗吧啊我你他她它请给帮看说要想要能可是有和与在层这那个份报告研究"
    "分析深度最新怎么如何现目买持估值财公司股票点评解读值得还是以及但因为所以"
    "已经没不就问题方面关于今天多少哪些什么情况数据一下来去把被让使或者更加"
)


def relevance_tokens(query: str, max_tokens: int = 8) -> list:
    """把查询切成用于相关性判定的核心 token（去重保序）。

    - 连续中文串 <= 6 字（公司名/行业词，如"澜起科技""半导体设备"）整体保留；
    - 超过 6 字（整句话）拆成 2-gram 并剔除泛化词，避免"整句当一个 token"；
    - 数字/字母 token（代码、ticker、年份）保留；
    - 剔除泛化词，最多保留 max_tokens 个。
    """
    if not query:
        return []
    tokens: list = []
    for run in _CJK_RUN_RE.findall(str(query)):
        if len(run) <= _LONG_RUN_CUTOFF:
            if run not in GENERIC_TOKENS:
                tokens.append(run)
        else:
            for i in range(len(run) - 1):
                gram = run[i:i + 2]
                if gram in GENERIC_TOKENS:
                    continue
                # 含泛化字的组合（"度报""技你"）丢弃，降低噪声 token
                if gram[0] in GENERIC_CHARS or gram[1] in GENERIC_CHARS:
                    continue
                tokens.append(gram)
    for t in _ALNUM_RE.findall(str(query)):
        if t in GENERIC_TOKENS:
            continue
        tokens.append(t)

    seen = set()
    out = []
    for t in tokens:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_tokens:
            break
    return out


def relevance_ratio(results: list, query: str, min_tokens: int = 2) -> float:
    """批次相关度：命中足够 token 的结果占比。无 token 时返回 1.0（放行）。"""
    tokens = relevance_tokens(query)
    if not tokens:
        return 1.0
    if not results:
        return 0.0
    need = min(min_tokens, len(tokens))
    q_norm = re.sub(r"\s+", "", query or "")
    hits = 0
    for r in results:
        if not isinstance(r, dict):
            continue
        title = r.get("title", "") or ""
        snippet = r.get("snippet", "") or ""
        # 标题整串回声查询 → 只信摘要（头条/视频源常见投毒手法）
        title_norm = re.sub(r"\s+", "", title)
        blob = snippet if (q_norm and q_norm in title_norm) else title + snippet
        if sum(1 for t in tokens if t in blob) >= need:
            hits += 1
    return hits / len(results)
