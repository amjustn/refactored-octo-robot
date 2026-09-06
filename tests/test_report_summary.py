#!/usr/bin/env python3
"""报告结构化摘要（meta.summary）测试。

覆盖：
- save_report 传入 summary 时写入 meta；不传时保持旧形状（无 summary 键）
- /api/reports 列表项在 meta 含 summary 时带出该键，否则不带
- run() 摘要管线：chat_complete 成功 → meta 用 LLM 摘要；
  LLM 异常 → 回退到确定性提取（首个标题 + 首段），报告保存不受影响

Run: cd <repo-root> && venv/bin/python -m pytest tests/test_report_summary.py -v
"""
import os
import sys
import json
import asyncio

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi.testclient import TestClient

import app.web_common as main_module
from app.main import app
from app.core import config as _config

client = TestClient(app)


def _auth_headers():
    if _config.BERKSHIRE_API_TOKEN:
        from app.core.jwt_utils import create_token
        return {"Authorization": f"Bearer {create_token(_config.BERKSHIRE_API_TOKEN)}"}
    return {}


@pytest.fixture
def tmp_repo(monkeypatch, tmp_path):
    """把持久化重定向到临时目录（tasks.db + reports/），与 test_harness_opt 同法。"""
    import app.harness.persist as persist_mod
    from app.core.task_store import TaskStore

    repo = persist_mod.Repository(TaskStore(db_path=tmp_path / "tasks.db"))
    monkeypatch.setattr(persist_mod, "_repo", repo)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    monkeypatch.setattr(persist_mod, "REPORTS_DIR", reports_dir)
    return repo


def _make_spec(skill_name="investment-team", arguments="测试目标"):
    from app.harness.spec import TaskSpec
    from app.skills import get_skill

    return TaskSpec.build(get_skill(skill_name), arguments)


# ==================== save_report 持久化 ====================

class TestSaveReportSummary:
    def test_summary_persisted_into_meta(self, tmp_repo):
        import app.harness.persist as persist_mod

        path = tmp_repo.save_report(
            "buffett", "测试目标", "# 标题\n\n正文", 1.5, {"total_tokens": 10},
            summary="这是一句话结论",
        )
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta["summary"] == "这是一句话结论"
        assert path.parent == persist_mod.REPORTS_DIR

    def test_no_summary_keeps_old_meta_shape(self, tmp_repo):
        path = tmp_repo.save_report("buffett", "测试目标", "# 标题\n\n正文")
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert "summary" not in meta
        assert meta["skill_name"] == "buffett"
        assert meta["arguments"] == "测试目标"


# ==================== /api/reports 列表 ====================

class TestListReportsSummary:
    def test_items_include_summary_only_when_present(self, monkeypatch, tmp_path):
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(main_module, "REPORTS_DIR", reports_dir)

        (reports_dir / "with_summary_20260101_000000.md").write_text("# 报告\n\n正文", encoding="utf-8")
        (reports_dir / "with_summary_20260101_000000.meta.json").write_text(json.dumps({
            "skill_name": "buffett", "arguments": "目标甲",
            "duration_seconds": 1.0, "summary": "核心结论：买入",
        }, ensure_ascii=False), encoding="utf-8")

        (reports_dir / "legacy_20260101_000001.md").write_text("# 旧报告\n\n正文", encoding="utf-8")
        (reports_dir / "legacy_20260101_000001.meta.json").write_text(json.dumps({
            "skill_name": "buffett", "arguments": "目标乙", "duration_seconds": 2.0,
        }, ensure_ascii=False), encoding="utf-8")

        res = client.get("/api/reports", headers=_auth_headers())
        assert res.status_code == 200
        items = {r["name"]: r for r in res.json()["reports"]}
        assert items["with_summary_20260101_000000.md"]["summary"] == "核心结论：买入"
        assert "summary" not in items["legacy_20260101_000001.md"]


# ==================== 摘要管线（run 集成） ====================

class TestSummaryPipeline:
    def test_deterministic_extract(self):
        import app.harness as harness

        assert harness._extract_summary(
            "# 贵州茅台投资价值分析\n\n> 引用行跳过\n\n核心逻辑：品牌护城河稳固。\n\n## 二、估值"
        ) == "贵州茅台投资价值分析：核心逻辑：品牌护城河稳固。"
        # 无标题时退化为首个非空行
        assert harness._extract_summary("正文第一段\n\n第二段") == "正文第一段"
        assert harness._extract_summary("") == ""

    def test_llm_summary_used_on_success(self, tmp_repo, monkeypatch):
        """chat_complete 正常返回 → meta.summary 为 LLM 摘要。"""
        import app.harness as harness
        import app.harness.persist as persist_mod
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kwargs):
            return "# 正式报告\n\n正文段落", []

        async def fake_chat_complete(system, user, **kw):
            return "LLM生成的一句话结论"

        monkeypatch.setattr(harness, "_execute", fake_execute)
        monkeypatch.setattr(harness, "chat_complete", fake_chat_complete)

        result = asyncio.run(harness.run(_make_spec(), EventBus()))
        assert result.status == "completed"

        reports = list(persist_mod.REPORTS_DIR.glob("*.md"))
        assert len(reports) == 1
        meta = json.loads(reports[0].with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta["summary"] == "LLM生成的一句话结论"

    def test_llm_failure_falls_back_to_extract(self, tmp_repo, monkeypatch):
        """chat_complete 抛异常 → 确定性兜底非空，报告照常保存。"""
        import app.harness as harness
        import app.harness.persist as persist_mod
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kwargs):
            return "# 兜底标题\n\n兜底正文段落", []

        async def boom_chat_complete(system, user, **kw):
            raise RuntimeError("LLM 不可用")

        monkeypatch.setattr(harness, "_execute", fake_execute)
        monkeypatch.setattr(harness, "chat_complete", boom_chat_complete)

        result = asyncio.run(harness.run(_make_spec(), EventBus()))
        assert result.status == "completed"

        reports = list(persist_mod.REPORTS_DIR.glob("*.md"))
        assert len(reports) == 1
        meta = json.loads(reports[0].with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta["summary"] == "兜底标题：兜底正文段落"

    def test_summary_failure_never_breaks_save(self, tmp_repo, monkeypatch):
        """摘要管线自身异常（含兜底提取）也不影响报告保存。"""
        import app.harness as harness
        import app.harness.persist as persist_mod
        from app.harness.events import EventBus

        async def fake_execute(spec, bus, **kwargs):
            return "# 报告\n\n正文", []

        async def boom_summarize(report, spec):
            raise RuntimeError("不应逃逸")

        monkeypatch.setattr(harness, "_execute", fake_execute)
        # 直接破坏 _summarize_report 的调用方也救不回来——这里验证的是
        # _summarize_report 内部已捕获所有异常：LLM 与提取同时坏掉时返回 ""。
        monkeypatch.setattr(harness, "chat_complete", boom_summarize)
        monkeypatch.setattr(harness, "_extract_summary", lambda report: "")

        result = asyncio.run(harness.run(_make_spec(), EventBus()))
        assert result.status == "completed"
        reports = list(persist_mod.REPORTS_DIR.glob("*.md"))
        assert len(reports) == 1
        meta = json.loads(reports[0].with_suffix(".meta.json").read_text(encoding="utf-8"))
        assert meta.get("summary", "") == ""
