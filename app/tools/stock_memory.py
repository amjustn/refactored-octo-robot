"""AI Berkshire Web - 标的级记忆模块 (Stock-level Memory).

按标的名/代码检索 data/reports/ 下的历史研究报告与 thesis-tracker 报告,
组装成可注入 LLM prompt 的摘要片段, 让后续研究站在历史记忆之上。

报告文件约定: {skill_name}_{YYYYMMDD}_{HHMMSS}.md
配套元数据: 同名 .meta.json, 内容形如 {"skill_name", "arguments", "created_at", ...}
  - arguments 是用户输入的标的名/代码, 如 "腾讯" 或 "600519 贵州茅台"
meta.json 缺失时从文件名解析 skill_name 与时间戳。
standalone/ 子目录 (前端独立渲染的 .html 归档) 不参与检索。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..core.config import BASE_DIR

logger = logging.getLogger(__name__)

REPORTS_DIR: Path = BASE_DIR / "data" / "reports"

EXCERPT_CHARS = 600

# 文件名形如 thesis-tracker_20260802_112828.md
_FILENAME_RE = re.compile(
    r"^(?P<skill>.+?)_(?P<ts>\d{8}_\d{6})\.md$"
)
_TS_FORMAT = "%Y%m%d_%H%M%S"


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------
def _parse_filename(path: Path) -> Dict[str, str]:
    """从文件名解析 skill_name 与 created_at (meta.json 缺失时的兜底)."""
    m = _FILENAME_RE.match(path.name)
    if not m:
        return {"skill_name": path.stem, "created_at": ""}
    ts = m.group("ts")
    try:
        created_at = datetime.strptime(ts, _TS_FORMAT).isoformat()
    except ValueError:
        created_at = ""
    return {"skill_name": m.group("skill"), "created_at": created_at}


def _load_meta(path: Path) -> Dict:
    """读取配套 .meta.json, 失败/缺失时返回空 dict."""
    meta_path = path.with_suffix(".meta.json")
    if not meta_path.exists():
        return {}
    try:
        with meta_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("meta 解析失败 %s: %s", meta_path, e)
        return {}


def _report_info(path: Path) -> Dict:
    """汇总单份报告的元信息: file/skill_name/created_at/arguments."""
    meta = _load_meta(path)
    parsed = _parse_filename(path)
    return {
        "file": str(path),
        "skill_name": str(meta.get("skill_name") or parsed["skill_name"] or path.stem),
        "created_at": str(meta.get("created_at") or parsed["created_at"] or ""),
        "arguments": str(meta.get("arguments") or ""),
    }


def _iter_reports() -> List[Path]:
    """顶层 data/reports/*.md, 排除 standalone/ 子目录 (glob 天然只扫一层)."""
    if not REPORTS_DIR.is_dir():
        return []
    return sorted(REPORTS_DIR.glob("*.md"))


def _ts_sort_key(info: Dict) -> tuple:
    """created_at 排序键; 解析失败回退到文件 mtime, 保证可排序."""
    ts = info["created_at"]
    try:
        return (datetime.fromisoformat(ts).timestamp(), info["file"])
    except (ValueError, TypeError):
        try:
            return (Path(info["file"]).stat().st_mtime, info["file"])
        except OSError:
            return (0.0, info["file"])


def _read_excerpt(path: Path, limit: int = EXCERPT_CHARS) -> str:
    """取报告正文前 limit 字符 (去除首尾空白)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("读取报告失败 %s: %s", path, e)
        return ""
    return text.strip()[:limit]


# ---------------------------------------------------------------------------
# 标的归一（2026-09-20）
# ---------------------------------------------------------------------------
# 旧匹配只有"字符串双向包含"，同一只票换个写法就落到不同结果，实测：
#   '中际旭创' -> 1 篇（arguments='300308 中际旭创'）
#   '300308'   -> 3 篇（8 月那批 arguments 只写代码，打名字永远召不回）
#   '中际旭创未来三年' -> 0 篇（带修饰词的整句，两边谁也不含谁 -> 静默 0 注入）
# 现补一条：两边都用项目的统一入口 resolve_symbol 解析成规范代码，代码相同即同组。
# 三条不可退让的约束：
#   1) 只增不减 —— 旧规则命中一律保留，归一命中是 OR 上去的，绝不挤掉原有命中；
#   2) 不抛异常 —— resolve_symbol 依赖 name_index（当日缓存过期时实测冷启动 22.6 秒），
#      任何失败都退回旧规则，绝不让记忆检索把研究请求带崩或拖慢；
#   3) 写法一致 —— 同一标的的各种写法必须落到同一组（由自检脚本逐条对账）。
# 明确不做：arguments 为空的 56 篇（无标的信息，对任何写法都召不回，不是写法问题）；
#          错别字（'中际讯创'）解析为 None，维持现状。
_SYMBOL_MEMO: Dict[str, Optional[str]] = {}


def symbol_group_key(text: str) -> Optional[str]:
    """把一段文字解析成规范标的代码（进程内记忆化；解析不出返回 None，绝不抛异常）。"""
    key = (text or "").strip()
    if not key:
        return None
    if key not in _SYMBOL_MEMO:
        sym: Optional[str] = None
        try:
            from .market_data import resolve_symbol

            sym, _src = resolve_symbol(key)
        except Exception as e:  # 解析器坏了也不能影响召回
            logger.debug("symbol_group_key 解析失败 %r: %s", key, e)
            sym = None
        _SYMBOL_MEMO[key] = sym
    return _SYMBOL_MEMO[key]


def reset_symbol_memo() -> None:
    """清空解析记忆（自检脚本用，也便于运行期强制重新解析）。"""
    _SYMBOL_MEMO.clear()


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def list_recent_reports(limit: int = 30) -> List[dict]:
    """扫描 data/reports/ 下所有 .md (排除 standalone/), 按时间戳倒序.

    返回 [{file, skill_name, created_at, arguments}]。
    """
    infos = [_report_info(p) for p in _iter_reports()]
    infos.sort(key=_ts_sort_key, reverse=True)
    return infos[:limit]


def find_stock_reports(query: str, limit: int = 5) -> List[dict]:
    """按标的名或代码匹配历史报告.

    匹配规则:
      - query 与 arguments 双向包含 (如 query='茅台' 命中 arguments='600519 贵州茅台');
      - query 为 6 位数字代码且与 arguments/文件名中的代码一致。

    返回 [{file, skill_name, created_at, arguments, excerpt}],
    excerpt 为报告正文前 600 字符。

    匹配（2026-09-20 起）：旧字符串规则 OR 标的归一归组 —— 见 symbol_group_key。
    """
    query = (query or "").strip()
    if not query:
        return []

    is_code = len(query) == 6 and query.isdigit()
    q_group = symbol_group_key(query)  # 名字/代码/带修饰词整句 -> 同一把钥匙

    matched: list[dict] = []
    for path in _iter_reports():
        info = _report_info(path)
        args = info["arguments"]
        if not args and not is_code:
            continue

        if is_code:
            hit = query in args or query in path.name
        else:
            # 双向包含; 空 arguments 直接跳过
            hit = bool(args) and (query in args or args in query)

        # 归一归组：同一规范代码即同组（只增不减，见文件头说明）
        if not hit and q_group and args:
            hit = symbol_group_key(args) == q_group

        if not hit:
            continue

        info["excerpt"] = _read_excerpt(path)
        matched.append(info)

    matched.sort(key=_ts_sort_key, reverse=True)
    return matched[:limit]


def build_memory_context(query: str, limit: int = 5) -> str:
    """组装可注入 LLM prompt 的标的记忆片段; 无匹配报告时返回空字符串.

    命中 0 篇时也留 INFO 日志（2026-09-20）：这条通道原先静默 —— 召回到 0 篇
    与"注入被跳过"在报告与日志里都看不出来，事后无法追查（用户口径：数据管道
    不能静默失败）。留痕后可对账"哪次研究带了几篇历史"。
    """
    reports = find_stock_reports(query, limit=limit)
    if not reports:
        logger.info("记忆注入: 标的=%r 命中 0 篇（无同标的历史报告，未注入）", query)
        return ""

    lines = [
        "=== 历史研究记忆 ===",
        f"标的: {query}",
        f"相关历史报告 {len(reports)} 篇:",
    ]
    for i, r in enumerate(reports, 1):
        lines.append(
            f"{i}. [{r['skill_name']}] {r['created_at']} 参数: {r['arguments']}"
        )
        excerpt = (r.get("excerpt") or "").strip()
        if excerpt:
            indent = "   "
            lines.append(indent + excerpt.replace("\n", "\n" + indent))
    lines.append("=== 记忆结束 ===")
    text = "\n".join(lines)
    logger.info(
        "记忆注入: 标的=%r 命中 %d 篇 注入 %d 字符", query, len(reports), len(text)
    )
    return text


def get_stock_memory_status() -> dict:
    """返回记忆库概况 {total_reports, per_skill, last_report_time}."""
    infos = [_report_info(p) for p in _iter_reports()]
    per_skill: Dict[str, int] = {}
    for info in infos:
        per_skill[info["skill_name"]] = per_skill.get(info["skill_name"], 0) + 1

    last_report_time: Optional[str] = None
    if infos:
        infos.sort(key=_ts_sort_key, reverse=True)
        last_report_time = infos[0]["created_at"] or None

    return {
        "total_reports": len(infos),
        "per_skill": dict(sorted(per_skill.items())),
        "last_report_time": last_report_time,
    }
