"""报告导出模块 — Markdown 报告转 PDF + 微信推送封装。

AI Berkshire Web 的报告导出工具:
- markdown_to_pdf():    Markdown -> HTML -> PDF (weasyprint 优先, 中文字体支持好)
- push_report_wechat(): 通过环境变量配置的 webhook 推送报告到微信 (类似 Server酱, urllib 实现)
- export_report():      入口, 读取 .md 报告文件并导出

PDF 生成后端优先级 (运行时探测): weasyprint > reportlab > fpdf。
依赖缺失时骨架函数 raise NotImplementedError('依赖缺失,由主agent安装 ...')。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["markdown_to_pdf", "push_report_wechat", "export_report"]

# 推送内容截断上限 (字符), Server酱风格 webhook 对 content 长度有硬限制
WECHAT_CONTENT_MAX = 8000

# 中文字体探测结果缓存 (避免每次导出都跑 fc-list)
_CJK_FONTS_CACHE: bool | None = None


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------
def _has_cjk_fonts() -> bool:
    """检查系统是否有中文字体 (fc-list :lang=zh)。失败时保守返回 False (CSS 用 serif 兜底)。"""
    global _CJK_FONTS_CACHE
    if _CJK_FONTS_CACHE is not None:
        return _CJK_FONTS_CACHE
    try:
        if shutil.which("fc-list"):
            proc = subprocess.run(
                ["fc-list", ":lang=zh"],
                capture_output=True, text=True, timeout=10,
            )
            _CJK_FONTS_CACHE = bool(proc.stdout and proc.stdout.strip())
        else:
            # 无 fontconfig 工具: 直接探测常见 CJK 字体文件
            candidates = [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            ]
            _CJK_FONTS_CACHE = any(Path(p).exists() for p in candidates)
    except Exception:  # noqa: BLE001 - 探测失败不致命
        _CJK_FONTS_CACHE = False
    return _CJK_FONTS_CACHE


def _build_css() -> tuple[str, str]:
    """生成基础 CSS。返回 (css, note); note 用于注明中文字体情况。"""
    if _has_cjk_fonts():
        font_family = (
            "'Noto Sans CJK SC', 'Noto Sans CJK', "
            "'WenQuanYi Zen Hei', 'Microsoft YaHei', sans-serif"
        )
        note = "使用系统中文字体 (Noto Sans CJK 等)"
    else:
        # 无中文字体: serif 兜底 (系统 serif 通常含 CJK 回退或至少可渲染)
        font_family = "serif"
        note = "未检测到中文字体, CSS 已用 serif 兜底 (中文可能显示为方块, 建议安装 fonts-noto-cjk)"
    css = f"""
    @page {{
        size: A4;
        margin: 2cm 1.8cm;
        @bottom-center {{
            content: counter(page) " / " counter(pages);
            font-size: 9px; color: #888;
        }}
    }}
    body {{
        font-family: {font_family};
        font-size: 12px;
        line-height: 1.7;
        color: #1a1a1a;
        word-wrap: break-word;
    }}
    h1 {{ font-size: 22px; color: #7a5c00; border-bottom: 2px solid #c9a227; padding-bottom: 6px; }}
    h2 {{ font-size: 18px; color: #7a5c00; border-bottom: 1px solid #e0d3a8; padding-bottom: 4px; }}
    h3 {{ font-size: 15px; color: #333; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 11px; }}
    th, td {{ border: 1px solid #bbb; padding: 6px 10px; text-align: left; }}
    th {{ background: #f5efe0; font-weight: bold; }}
    tr:nth-child(even) td {{ background: #fafaf6; }}
    code {{
        font-family: 'Noto Sans Mono CJK SC', 'Sarasa Mono SC', monospace;
        background: #f4f4f4; padding: 1px 4px; border-radius: 3px; font-size: 11px;
    }}
    pre {{
        background: #f4f4f4; padding: 10px 12px; border-radius: 4px;
        white-space: pre-wrap; word-wrap: break-word; border: 1px solid #e0e0e0;
    }}
    pre code {{ background: none; padding: 0; }}
    blockquote {{
        border-left: 4px solid #c9a227; margin: 10px 0; padding: 6px 14px;
        color: #555; background: #faf7ef;
    }}
    img {{ max-width: 100%; }}
    a {{ color: #0b5394; text-decoration: none; }}
    hr {{ border: none; border-top: 1px solid #ddd; margin: 18px 0; }}
    """
    return css, note


def _markdown_to_html(text: str) -> str:
    """Markdown -> HTML。优先 python-markdown, 其次 markdown-it-py (本 venv 已装)。"""
    try:
        import markdown  # type: ignore

        return markdown.markdown(
            text, extensions=["tables", "fenced_code", "toc", "sane_lists"]
        )
    except ImportError:
        pass
    try:
        from markdown_it import MarkdownIt

        md = MarkdownIt("commonmark", {"html": True}).enable("table")
        return md.render(text)
    except ImportError:
        pass
    raise NotImplementedError("依赖缺失,由主agent安装: 需要 markdown 或 markdown-it-py")


def _detect_pdf_backend() -> str:
    """探测可用 PDF 后端: weasyprint > reportlab > fpdf。"""
    try:
        import weasyprint  # noqa: F401

        return "weasyprint"
    except ImportError:
        pass
    try:
        import reportlab  # noqa: F401

        return "reportlab"
    except ImportError:
        pass
    try:
        import fpdf  # noqa: F401

        return "fpdf"
    except ImportError:
        pass
    return ""


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[已截断]"


def _get_wechat_webhook() -> str:
    """读取 webhook: 环境变量 REPORT_WECHAT_WEBHOOK 优先, 其次 WECHAT_WEBHOOK, 再查 config。"""
    for var in ("REPORT_WECHAT_WEBHOOK", "WECHAT_WEBHOOK"):
        val = os.getenv(var, "").strip()
        if val:
            return val
    try:
        from app.core import config  # type: ignore

        for attr in ("REPORT_WECHAT_WEBHOOK", "WECHAT_WEBHOOK"):
            val = getattr(config, attr, None)
            if val:
                return str(val).strip()
    except Exception:  # noqa: BLE001 - 项目外独立使用时 config 可能不可导入
        pass
    return ""


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------
def markdown_to_pdf(markdown_text: str, output_path: str, title: str | None = None) -> dict:
    """将 Markdown 文本转为 PDF。

    返回: {"ok": bool, "path": str, "pages": int|None, "error": str, "note": str|None}
    - weasyprint 后端: markdown->HTML + 基础 CSS(中文等宽字体/表格边框/代码块背景) -> PDF
    - reportlab / fpdf 为降级后端 (本 venv 未安装, 走骨架分支)
    - 无任何可用库: raise NotImplementedError('依赖缺失,由主agent安装 ...')
    """
    backend = _detect_pdf_backend()
    if not backend:
        raise NotImplementedError(
            "依赖缺失,由主agent安装: weasyprint / reportlab / fpdf 均不可用"
        )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    css, note = _build_css()

    try:
        if backend == "weasyprint":
            body_html = _markdown_to_html(markdown_text)
            title_html = (
                f"<h1>{_html_escape(title)}</h1>" if title else ""
            )
            html_doc = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<style>{css}</style></head><body>{title_html}{body_html}</body></html>"
            )
            from weasyprint import HTML  # type: ignore

            document = HTML(string=html_doc).render()
            document.write_pdf(str(out))
            pages = len(document.pages)
            return {"ok": True, "path": str(out), "pages": pages, "error": "", "note": note}

        # ---- 降级后端骨架 (本 venv 未安装 reportlab/fpdf) ----
        raise NotImplementedError(
            f"依赖缺失,由主agent安装: 检测到后端 '{backend}' 但未实现转换路径"
        )
    except NotImplementedError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("PDF 生成失败 (backend=%s)", backend)
        return {"ok": False, "path": str(out), "pages": None, "error": f"PDF 生成失败: {e}", "note": note}


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def push_report_wechat(markdown_text: str, title: str | None = None) -> dict:
    """将报告推送到微信 (Server酱风格 webhook)。

    通过环境变量 REPORT_WECHAT_WEBHOOK (或 config 中的 wechat 配置) 获取 webhook,
    POST JSON {"title", "content"(截断到 8000 字), "md": markdown_text}。
    未配置 webhook 时返回 {"ok": False, "error": "未配置 webhook"}, 不抛异常。
    """
    webhook = _get_wechat_webhook()
    if not webhook:
        return {"ok": False, "error": "未配置 webhook"}

    content = _truncate(markdown_text, WECHAT_CONTENT_MAX)
    # Server酱 (sctapi.ftqq.com) 用 form-urlencoded {title, desp}; 通用 webhook 用 JSON。
    if "sctapi.ftqq.com" in webhook:
        data = urllib.parse.urlencode(
            {"title": title or "AI Berkshire 报告", "desp": content}
        ).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
            method="POST",
        )
    else:
        payload = {
            "title": title or "AI Berkshire 报告",
            "content": content,
            "md": markdown_text,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            resp_text = resp.read(4096).decode("utf-8", "replace")
        if 200 <= status < 300:
            return {"ok": True, "status": status, "response": resp_text[:500], "error": ""}
        return {
            "ok": False,
            "status": status,
            "response": resp_text[:200],
            "error": f"HTTP {status}: {resp_text[:200]}",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"推送失败: {e}"}


def export_report(report_path: str, fmt: str = "pdf") -> dict:
    """导出报告入口: 读取 .md 报告文件并转换。

    - 文件不存在: {"ok": False, "error": "报告文件不存在: ..."}
    - fmt='pdf': 输出到同目录同名 .pdf, 调 markdown_to_pdf
    - 其他 fmt: {"ok": False, "error": "不支持的格式: ..."}
    """
    path = Path(report_path)
    if not path.is_file():
        return {"ok": False, "error": f"报告文件不存在: {report_path}"}

    fmt = (fmt or "pdf").lower()
    if fmt != "pdf":
        return {"ok": False, "error": f"不支持的格式: {fmt} (当前仅支持 pdf)"}

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读取报告失败: {e}"}

    out_path = str(path.with_suffix(".pdf"))
    result = markdown_to_pdf(text, out_path, title=path.stem)
    result.setdefault("source", str(path))
    return result
