#!/usr/bin/env python3
"""批C 端到端验收：真实 ContextBuilder.build() 里历史决策注入是否真的进了最终 prompt

用法: ./venv/bin/python scripts/verify_history_injection_e2e.py [--stub]

--stub 时把数据采集( build_full_context )与目标解析( parse_goal )换成桩，
只跑真实注入层 + 真实裁剪/分级路径（不联网、不调模型，确定性可重复）。
默认走真实采集路径（联网，慢，用于最终确认）。

只读生产 decision-log.md（收尾校验 sha256 未变）。
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.harness import context as ctx  # noqa: E402
from app.harness import decision_log as dl  # noqa: E402

STUB = "--stub" in sys.argv
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


LOG = dl.DECISION_LOG_PATH
SHA_BEFORE = hashlib.sha256(LOG.read_bytes()).hexdigest() if LOG.exists() else ""


async def stub_full_context(arguments: str = "", skill_name: str = "", goal_hints=None) -> str:
    return (
        "## 当前时间上下文\n\n**当前日期**：2026年09月11日\n\n"
        "## 最新市场指数\n\n- 上证指数: 3,500.12 (🔴+0.45%)\n"
    )


async def stub_parse_goal(arguments: str, llm_config=None):
    return None


class FakeBus:
    def __init__(self) -> None:
        self.events = []

    def emit(self, task_id, etype, **kw):
        self.events.append((str(etype), kw))


async def run(query: str, bus: FakeBus | None = None):
    builder = ctx.ContextBuilder(bus=bus, task_id="e2e") if bus else ctx.ContextBuilder(task_id="e2e")
    return await builder.build(query)


async def main() -> None:
    if STUB:
        ctx.build_full_context = stub_full_context
        ctx.parse_goal = stub_parse_goal

    # ── 1. 中际旭创：跨写法聚合 + 规范代码 + 时间线进入最终 context ─────
    bus = FakeBus()
    b = await run("中际旭创", bus)
    c = b.context
    check("1-历史决策块进入最终 context", "历史决策复盘" in c)
    check("1b-报告头带规范代码", "（规范代码 300308.SZ）" in c)
    check("1c-时间线在场且含 08/09 两月",
          "### 时间线" in c and "2026-08" in c and "2026-09" in c)
    check("1d-重复合并声明在场", "合并 1 条完全重复" in c)


    def decision_block(text: str) -> str:
        """切出最终 context 里的历史决策块（到下一个 ## 标题为止）。"""
        i = text.find("## 📋 历史决策复盘")
        if i < 0:
            return ""
        j = text.find("\n## ", i + 5)
        return text[i:j] if j > 0 else text[i:]


    def trimmed_sections(text: str) -> list[str]:
        """列出最终 context 里带裁剪标注的 section 标题（证据用）。"""
        out = []
        for sec in text.split("\n## "):
            if "（本节因 token 预算裁剪）" in sec or "已裁剪" in sec:
                out.append(sec.splitlines()[0][:40])
        return out


    db = decision_block(c)
    check("1e-决策块自身未被裁剪",
          bool(db) and "（本节因 token 预算裁剪）" not in db,
          f"决策块 {len(db)} 字符；被裁剪的其它 section={trimmed_sections(c) or '无'}")
    check("1e2-决策块含最新版标记与时间线",
          "★ 最后一版" in db and "### 时间线" in db,
          f"块内最后一版={'★ 最后一版' in db}")
    check("1f-分类计数含 decision_history",
          b.token_counts.get("decision_history", 0) > 0,
          f"token_counts={b.token_counts}")
    check("1g-事件 context_ready 带 token_counts",
          any(kw.get("token_counts", {}).get("decision_history", 0) > 0
              for _, kw in bus.events),
          f"事件数={len(bus.events)}")
    check("1h-注入块在其它资料之前（不被后置资料挤位）",
          c.find("历史决策复盘") < c.find("当前时间上下文"))

    # ── 2. 打代码 / 打名字，聚合在真实 build 路径上仍成立 ────────────
    b_code = await run("300308")
    b_name = await run("中际旭创")

    def body(text: str) -> str:
        i = text.find("### 时间线")
        return text[i:] if i >= 0 else text

    db_code, db_name = decision_block(b_code.context), decision_block(b_name.context)

    def tl(block: str) -> str:
        """决策块的「时间线起」部分（头部含用户输入写法，按设计允许不同）。"""
        i = block.find("### 时间线")
        return block[i:] if i >= 0 else block

    if STUB:
        check("2-名字与代码走 build 路径得到同一段正文",
              body(b_code.context) == body(b_name.context),
              f"名字 {len(body(b_name.context))} 字符 / 代码 {len(body(b_code.context))} 字符")
    else:
        # 真实采集模式下时间线之后的实时资料本就每次不同，
        # 因此只比对决策块（同源生成）：时间线起必须逐字节一致，
        # 头部只允许「用户输入写法」这一处差异（设计如此）。
        check("2-真实采集下决策块时间线逐字节一致（头部仅输入写法不同）",
              bool(db_code) and bool(db_name) and tl(db_code) == tl(db_name)
              and "（规范代码 300308.SZ）" in db_code and "（规范代码 300308.SZ）" in db_name,
              f"时间线 {len(tl(db_name))} / {len(tl(db_code))} 字符；"
              f"头部差异仅输入写法={'中际旭创' in db_name and '300308（' in db_code}")
    check("2b-两种写法都带规范代码与合并声明",
          all("（规范代码 300308.SZ）" in x and "合并 1 条完全重复" in x
              for x in (db_code, db_name)),
          "300308 / 中际旭创 两路一致")

    # ── 3. 英维克：冲突块 + 改口要求进入最终 context ─────────────────
    b_y = await run("英维克")
    cy = b_y.context
    check("3-冲突块进入最终 context", "⚠ 立场冲突" in cy)
    check("3b-冲突块标出最后一版/上一版",
          "最后一版 2026-09-11 05:49" in cy and "上一版" in cy)
    check("3c-报告头写上次立场与改口要求",
          "上次立场: 持有/中性" in cy and "不要沉默改口" in cy)
    dy = decision_block(cy)
    check("3d-冲突信息块自身未被裁剪",
          bool(dy) and "（本节因 token 预算裁剪）" not in dy
          and "以最后一版为准" in dy,
          f"冲突块 {len(dy)} 字符；被裁剪的其它 section={trimmed_sections(cy) or '无'}")

    # ── 4. 无历史标的：不注入空壳 ────────────────────────────────────
    b_none = await run("不存在的标的XYZ123")
    check("4-无历史标的不出现空壳历史块",
          "历史决策复盘" not in b_none.context,
          f"context 长度={len(b_none.context)}")
    check("4b-无历史标的仍正常出上下文（未报错崩溃）",
          len(b_none.context) > 0)

    SHA_AFTER = hashlib.sha256(LOG.read_bytes()).hexdigest()
    check("5-生产 decision-log.md 只读未改", SHA_BEFORE == SHA_AFTER, f"sha256 {SHA_AFTER[:12]}…")

    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  →  " + detail))
    mode = "stub（桩采集）" if STUB else "真实采集（联网）"
    print(f"\n批C 端到端注入检查（{mode}）：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    sys.exit(1 if failed else 0)


asyncio.run(main())
