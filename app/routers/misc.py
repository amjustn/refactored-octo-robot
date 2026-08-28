"""Assorted API routes: auth, skills, follow-up, llm, upload, reports, pdf, stats, tools."""
import asyncio
import json
import os
from datetime import datetime
from hmac import compare_digest
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import web_common as _wc  # late-bound: tests monkeypatch web_common attrs
from ..core.config import BERKSHIRE_API_TOKEN
from ..core.jwt_utils import create_token
from ..harness.persist import get_repository
from ..skills import get_skill, list_skills, load_skill_prompt
from ..web_common import _sanitize_filename, _validate_llm_config, logger

router = APIRouter()


# ==================== Auth API ====================

@router.post("/api/auth/login")
async def api_auth_login(request: Request):
    """Exchange the server secret for a JWT.

    Request body:  {"token": "<BERKSHIRE_API_TOKEN>"}
    Response:      {"access_token": "<jwt>", "token_type": "bearer"}
    """
    if BERKSHIRE_API_TOKEN is None:
        return {"access_token": "", "token_type": "bearer", "note": "Auth disabled"}

    try:
        body = await request.json()
    except Exception:
        body = {}

    secret = (body.get("token") or "").strip()
    if not compare_digest(secret, BERKSHIRE_API_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")

    jwt_token = create_token(BERKSHIRE_API_TOKEN)
    return {"access_token": jwt_token, "token_type": "bearer"}


@router.get("/api/skills")
async def api_list_skills():
    skills = list_skills()
    return {
        "skills": [
            {
                "name": s["name"],
                "display_name": s["display_name"],
                "description": s["description"],
                "category": s["category"],
                "is_multi_agent": s.get("is_multi_agent", False),
                "agent_count": s.get("agent_count", 1),
                "agents": s.get("agents", []),
                "series_mode": s.get("series_mode", False),
                "series_topics": s.get("series_topics", []),
                "input_hint": s.get("input_hint", ""),
            }
            for s in skills
        ]
    }


@router.get("/api/skills/{skill_name}")
async def api_get_skill(skill_name: str):
    skill = get_skill(skill_name)
    if not skill:
        return {"error": f"Skill not found: {skill_name}"}
    prompt = load_skill_prompt(skill_name)
    return {
        "skill": skill,
        "prompt_preview": prompt[:500] + "..." if len(prompt) > 500 else prompt,
    }

# ==================== Follow-up Q&A API ====================

class FollowUpRequest(BaseModel):
    report: str
    question: str
    skill_name: str = ""
    llm_config: Optional[dict] = None
    attachments: Optional[str] = Field(default=None, max_length=20000, description="追问附带的上传文件提取文本")
    history: Optional[list] = Field(default=None, description="本轮报告的既往追问对话 [{role, content}]")


@router.post("/api/follow-up")
async def api_follow_up(request: "Request", req: FollowUpRequest):
    """Answer a follow-up question about a generated report.

    Uses the report as context, sends the question to the LLM,
    and returns a concise answer. Uses the same LLM config as the
    original research if provided. Supports optional attachments
    (extracted file text) and multi-turn history.
    """
    if not req.report or not req.question:
        raise HTTPException(status_code=400, detail="report and question are required")

    if not _wc._check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    # Sanitize history: keep only well-formed user/assistant turns,
    # cap at 10 turns and 2000 chars each
    history_msgs = []
    if req.history:
        for item in req.history[-10:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                continue
            history_msgs.append({"role": role, "content": content[:2000]})

    # Trim report context to stay within limits; shrink when attachments present
    attachments = (req.attachments or "").strip()
    ctx_limit = 6000 if attachments else 8000
    report_ctx = req.report[-ctx_limit:] if len(req.report) > ctx_limit else req.report

    attach_section = f"""
## 用户上传的参考资料
{attachments}
""" if attachments else ""

    prompt = f"""你是一位专业的投资研究分析师。用户正在阅读你的报告并提出了追问。

## 报告内容（最后{ctx_limit}字）
{report_ctx}
{attach_section}
## 用户追问
{req.question}

## ⚠️ 核心规则（违反即不合格）
1. **永远不要只说"报告未涉及"就结束。** 这是最低标准。即使报告没有，你也必须用你的专业知识给出有价值的回答。
2. **报告有的→引用+展开。报告没有的→用你的知识回答，开头标注"📝 以下基于通用知识补充："。**
3. **给出实质内容**：具体数据、案例对比、逻辑推理。不要"可能""或许"打太极。
4. **回答你的用户真正想问的**：不要复述报告，不要回避问题。如果用户质疑某个观点，正面回应这个质疑。
5. 300-1000字，中文。"""

    try:
        from ..core.llm import chat_complete
        llm_cfg = _validate_llm_config(req.llm_config) if req.llm_config else None
        answer = await chat_complete(
            system_prompt="你是专业的投资研究分析师。面对追问：永远不要只说'报告未涉及'。报告不够时，用你的专业知识补充并标注来源。正面回应用户的质疑，给出实质内容。",
            user_message=prompt,
            temperature=0.8,
            llm_config=llm_cfg,
            history=history_msgs or None,
        )
        return {"answer": answer}
    except Exception as e:
        logger.error(f"Follow-up failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"追问失败: {str(e)}")

# ==================== LLM Config API ====================

@router.get("/api/llm/default")
async def api_llm_default():
    """Expose the server defaults — never the API key.

    Returns the model list the server's gateway actually supports so the
    frontend can build a per-provider model picker instead of a single
    free-form field.
    """
    from ..core.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_SUPPORTED_MODELS
    provider = "deepseek"  # could become config-driven later
    return {
        "provider": provider,
        "model": LLM_MODEL,
        "base_url": LLM_BASE_URL,
        "has_key": bool(LLM_API_KEY),
        "supported_models": LLM_SUPPORTED_MODELS,
    }


@router.post("/api/llm/test")
async def api_llm_test(request: "Request"):
    """Test a user-supplied LLM config with a minimal completion call."""
    # F4: each call spends a real LLM completion — rate-limit it.
    if not _wc._check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    try:
        body = await request.json()
    except Exception:
        body = {}
    llm_config = _validate_llm_config(body.get("llm_config") if isinstance(body, dict) else None)
    if not llm_config:
        return {"ok": False, "error": "配置为空或格式不正确（需要 api_key / base_url / model）"}
    if not llm_config.get("model"):
        return {"ok": False, "error": "请填写模型名称"}
    from ..core.llm import chat_complete, probe_tool_calling
    try:
        reply = await asyncio.wait_for(
            chat_complete(
                "你是一个连接测试助手。只回复一个词：pong",
                "ping",
                llm_config=llm_config,
                temperature=0,
            ),
            timeout=30.0,
        )
        # Validate: must return non-empty and not be the default server reply
        reply_text = (reply or "").strip()
        if not reply_text or len(reply_text) > 100:
            return {"ok": False, "error": f"收到异常回复: {reply_text[:50]}"}
        # Second probe: research agents need structured function calling to
        # fetch real market data. Surface unsupported models BEFORE the user
        # runs a research task and gets a hollow "无法获取实时数据" report.
        tools_supported, tools_detail = False, "工具调用探测未执行"
        try:
            tools_supported, tools_detail = await asyncio.wait_for(
                probe_tool_calling(llm_config), timeout=30.0
            )
        except asyncio.TimeoutError:
            tools_detail = "工具调用探测超时（30秒）"
        except Exception as e:
            tools_detail = f"工具调用探测失败: {str(e)[:120]}"
        return {
            "ok": True,
            "model": llm_config.get("model", ""),
            "reply": reply_text[:50],
            "using_server_key": not llm_config.get("api_key"),
            "tools_supported": tools_supported,
            "tools_detail": tools_detail,
        }
    except asyncio.TimeoutError:
        return {"ok": False, "error": "连接超时（30秒），请检查 base_url 是否可达"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

# ==================== File Upload & Multimodal ====================

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # per-file cap
_MAX_UPLOAD_FILES = 10


async def _read_upload_limited(f, limit: int):
    """Read an UploadFile in 1MB chunks, aborting as soon as limit is
    exceeded — the partial buffer is discarded so oversize files never sit
    in memory.  Returns (data, ok); ok=False means the limit was exceeded."""
    buf = bytearray()
    while True:
        chunk = await f.read(1024 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            return b"", False
    return bytes(buf), True


@router.post("/api/upload")
async def api_upload_file(request: "Request"):
    """Upload image/PDF/Excel, extract text content for research injection."""
    import base64
    import io

    if not _wc._check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    # Fast reject on declared body size before multipart parsing.  The
    # ceiling allows the maximum legitimate combination (10 files x 20MB)
    # plus multipart framing overhead; the per-file cap is enforced by
    # _read_upload_limited while streaming each part.
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > _MAX_UPLOAD_BYTES * _MAX_UPLOAD_FILES + 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="上传总大小超限（单个文件≤20MB，单次最多10个文件）")

    try:
        form = await request.form()
    except Exception:
        raise HTTPException(status_code=400, detail="需要 multipart/form-data")

    files = form.getlist("files")
    if not files:
        raise HTTPException(status_code=400, detail="未选择文件")
    if len(files) > _MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail="单次最多上传 10 个文件")

    results = []
    for f in files:
        filename = getattr(f, "filename", "unknown")
        content_type = getattr(f, "content_type", "") or ""
        data, within_limit = await _read_upload_limited(f, _MAX_UPLOAD_BYTES)
        item = {"filename": filename, "type": content_type}

        if not within_limit:
            item["error"] = "文件超过20MB限制"
            results.append(item)
            continue

        try:
            if content_type.startswith("image/"):
                b64 = base64.b64encode(data).decode()
                vision_model = form.get("vision_model", "") or None
                vision_base_url = form.get("vision_base_url", "") or None
                vision_api_key = form.get("vision_api_key", "") or None
                text = await _extract_image_text(b64, content_type, vision_model, vision_base_url, vision_api_key)
                item["text"] = text
                item["method"] = "vision"

            elif filename.lower().endswith(".pdf") or content_type == "application/pdf":
                # 首选 MarkItDown — 保留表格/标题层级为结构化 Markdown，
                # 对研报/财报这类表格密集的 PDF 分析质量远高于纯文本提取。
                item["text"] = ""
                item["method"] = ""
                try:
                    import tempfile
                    from markitdown import MarkItDown
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp.write(data)
                        tmp_path = tmp.name
                    try:
                        _md = MarkItDown()
                        _res = _md.convert(tmp_path)
                        item["text"] = (_res.text_content or "").strip()
                        if item["text"]:
                            item["method"] = "pdf-markitdown"
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                except Exception as e:
                    logger.warning(f"MarkItDown failed ({filename}), falling back to PyMuPDF: {e}")
                    item["text"] = ""
                    item["method"] = ""
                # 兜底：MarkItDown 失败/无输出 → PyMuPDF 纯文本
                if not item["text"]:
                    import fitz
                    doc = fitz.open(stream=data, filetype="pdf")
                    parts = []
                    for page in doc:
                        parts.append(page.get_text())
                    doc.close()
                    item["text"] = "\n".join(parts)[:50000]
                    item["method"] = "pdf-extract"
                item["text"] = item["text"][:50000]
                if len(item["text"].strip()) < 100:
                    # Likely a scanned PDF (no text layer) → try vision OCR fallback
                    try:
                        ocr_text = await _ocr_scanned_pdf(data, form)
                        if ocr_text is None:
                            item.pop("text", None)
                            item.pop("method", None)
                            item["error"] = "该PDF为扫描件（无文字层），当前模型不支持多模态，无法识别"
                        else:
                            item["text"] = ocr_text
                            item["method"] = "pdf-vision-ocr"
                    except Exception as oe:
                        item.pop("text", None)
                        item.pop("method", None)
                        item["error"] = f"扫描件识别失败: {str(oe)[:200]}"
                        logger.error(f"Scanned PDF OCR failed ({filename}): {oe}", exc_info=True)
                if item.get("text") and len(item["text"]) > 500:
                    summary = await _summarize_data(item["text"], filename, form)
                    if summary:
                        item["summary"] = summary

            elif filename.lower().endswith((".xlsx", ".xls")):
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
                parts = []
                for sn in wb.sheetnames:
                    ws = wb[sn]
                    parts.append(f"## Sheet: {sn}")
                    rows = []
                    for row in ws.iter_rows(values_only=True):
                        rows.append("\t".join(str(c) if c is not None else "" for c in row))
                    parts.append("\n".join(rows[:500]))
                wb.close()
                item["text"] = "\n".join(parts)[:50000]
                item["method"] = "excel-extract"

            elif filename.lower().endswith(".docx"):
                from docx import Document
                docx_doc = Document(io.BytesIO(data))
                parts = []
                for para in docx_doc.paragraphs:
                    if para.text.strip():
                        parts.append(para.text)
                for table in docx_doc.tables:
                    for row in table.rows:
                        parts.append("\t".join(cell.text for cell in row.cells))
                item["text"] = "\n".join(parts)[:50000]
                item["method"] = "docx-extract"
                if len(item["text"]) > 500:
                    summary = await _summarize_data(item["text"], filename, form)
                    if summary:
                        item["summary"] = summary

            elif filename.lower().endswith(".pptx"):
                from pptx import Presentation
                prs = Presentation(io.BytesIO(data))
                parts = []
                for slide_idx, slide in enumerate(prs.slides, 1):
                    parts.append(f"## Slide {slide_idx}")
                    for shape in slide.shapes:
                        if shape.has_text_frame:
                            shape_text = shape.text_frame.text.strip()
                            if shape_text:
                                parts.append(shape_text)
                        if shape.has_table:
                            for row in shape.table.rows:
                                parts.append("\t".join(cell.text for cell in row.cells))
                item["text"] = "\n".join(parts)[:50000]
                item["method"] = "pptx-extract"
                if len(item["text"]) > 500:
                    summary = await _summarize_data(item["text"], filename, form)
                    if summary:
                        item["summary"] = summary

            elif filename.lower().endswith((".doc", ".ppt")):
                item["error"] = "旧版 .doc/.ppt 格式暂不支持，请另存为 .docx/.pptx 后重新上传"

            elif filename.lower().endswith((".csv", ".txt", ".md", ".json")):
                raw_text = data.decode("utf-8", errors="replace")[:50000]
                item["text"] = raw_text
                item["method"] = "text-extract"
                if len(raw_text) > 500:
                    summary = await _summarize_data(raw_text, filename, form)
                    if summary:
                        item["summary"] = summary
            else:
                item["error"] = f"不支持的文件类型: {content_type}"

        except Exception as e:
            item["error"] = str(e)[:300]
            logger.error(f"File upload error ({filename}): {e}", exc_info=True)

        results.append(item)

    return {"files": results}



async def _summarize_data(raw_text: str, filename: str, form) -> str:
    """Use LLM to summarize extracted file data into key insights.

    Honors a per-request llm_config override carried in the upload form.
    Any failure (bad config, LLM error) returns "" — the caller then
    simply omits the summary instead of surfacing an error string.
    """
    try:
        from ..core.llm import chat_complete
        llm_config = None
        raw_cfg = form.get("llm_config") if form is not None else None
        if raw_cfg:
            try:
                llm_config = _validate_llm_config(json.loads(raw_cfg))
            except Exception:
                llm_config = None
        # Trim to avoid token overflow
        sample = raw_text[:6000]
        summary = await chat_complete(
            system_prompt="你是一位数据分析师。对上传的文件数据提取关键洞察，用中文简洁输出。",
            user_message=f"文件名: {filename}\n\n数据样本:\n{sample}\n\n请分析并输出:\n1. 数据概况（行数/列数/时间范围）\n2. 关键趋势或异常\n3. 3-5条核心洞察\n控制在300字以内",
            temperature=0.3,
            llm_config=llm_config,
        )
        return (summary or "")[:600]
    except Exception as e:
        logger.warning(f"Summarization failed for {filename}: {e}")
        return ""

async def _extract_image_text(b64_data: str, content_type: str, vision_model: str = None, vision_base_url: str = None, vision_api_key: str = None) -> str:
    """Use vision-capable LLM to describe image content."""
    try:
        from ..core.llm import get_client, resolve_llm_config
        if vision_model:
            # Use user's vision config if provided
            base_url = vision_base_url or resolve_llm_config(None)[0]
            api_key = vision_api_key or resolve_llm_config(None)[1]
            model = vision_model
        else:
            base_url, api_key, model = resolve_llm_config(None)
        # Detect: non-vision models (DeepSeek, etc.) cannot process images
        _NON_VISION = {'deepseek', 'kimi', 'hunyuan'}
        _model_lower = (model or '').lower()
        if not vision_model and (not model or any(kw in _model_lower for kw in _NON_VISION)):
            return f'[图片识别失败: 当前模型 "{model}" 不支持多模态。请在模型设置中将模型更换为支持图片的多模态模型（如 gpt-4o / glm-4v / claude-sonnet-4）]'
        client = get_client(base_url, api_key)

        resp = await client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "请详细描述这张图片中的所有文字、数据、图表内容。如果是财报截图，提取所有可见数字。如果是图表，描述趋势和数据点。"},
                    {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64_data}"}},
                ]
            }],
            max_tokens=2000,
        )
        return resp.choices[0].message.content or "(无内容)"
    except Exception as e:
        _err = str(e)[:200]
        logger.warning(f"Vision extraction failed: {_err}")
        # Give user a clear fix instruction
        if '400' in _err or 'deserialize' in _err or 'invalid' in _err.lower():
            return f'[图片识别失败: 模型 "{model}" 不支持图片识别。请在模型设置中将模型更换为支持多模态的模型（如 gpt-4o / glm-4v / claude-sonnet-4）]'
        return f"[图片识别失败: {_err}]"


async def _ocr_scanned_pdf(data: bytes, form) -> "str | None":
    """OCR a scanned PDF (no text layer) via the vision model.

    Rasterizes up to the first 5 pages at ~150 dpi and runs the same
    vision extraction used for image uploads on each page. Returns the
    concatenated page text (capped at 50000 chars), or None when no
    usable vision model is configured.
    """
    import base64

    import fitz

    from ..core.llm import resolve_llm_config

    # Same vision-config resolution as _extract_image_text
    vision_model = form.get("vision_model", "") or None
    vision_base_url = form.get("vision_base_url", "") or None
    vision_api_key = form.get("vision_api_key", "") or None
    if vision_model:
        base_url = vision_base_url or resolve_llm_config(None)[0]
        api_key = vision_api_key or resolve_llm_config(None)[1]
        model = vision_model
    else:
        base_url, api_key, model = resolve_llm_config(None)
    _NON_VISION = {'deepseek', 'kimi', 'hunyuan'}
    _model_lower = (model or '').lower()
    if not vision_model and (not model or any(kw in _model_lower for kw in _NON_VISION)):
        return None

    doc = fitz.open(stream=data, filetype="pdf")
    parts = []
    try:
        for page_idx, page in enumerate(doc):
            if page_idx >= 5:
                break
            pix = page.get_pixmap(dpi=150)
            b64 = base64.b64encode(pix.tobytes("png")).decode()
            page_text = await _extract_image_text(b64, "image/png", model, base_url, api_key)
            parts.append(f"## 第 {page_idx + 1} 页\n{page_text}")
    finally:
        doc.close()
    return "\n\n".join(parts)[:50000]

# ==================== Reports & Tasks ====================

@router.get("/api/reports")
async def api_list_reports():
    reports = []
    if _wc.REPORTS_DIR.exists():
        for f in sorted(_wc.REPORTS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            item = {
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "skill_name": "",
                "arguments": "",
                "duration_seconds": 0,
            }
            meta_path = f.with_suffix(".meta.json")
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    item["skill_name"] = meta.get("skill_name", "")
                    item["arguments"] = meta.get("arguments", "")
                    item["duration_seconds"] = meta.get("duration_seconds", 0)
                    if meta.get("summary"):
                        item["summary"] = meta["summary"]
                    if meta.get("partial"):
                        # 失败/取消任务的部分成果 — 历史列表提供"继续任务"入口
                        item["partial"] = True
                    if meta.get("task_id"):
                        item["task_id"] = meta["task_id"]
                except Exception:
                    pass
            if not item["skill_name"]:
                # Legacy reports: derive skill from filename prefix
                item["skill_name"] = f.name.rsplit("_", 2)[0] if "_" in f.name else ""
            reports.append(item)

    # 断点续跑标记：partial + multi-agent 技能 + 有可复用的成功 artifact。
    # meta 没有 task_id 的旧报告按 skill+arguments 模糊匹配最近的终态任务。
    repo = get_repository()
    for item in reports:
        if not item.get("partial"):
            continue
        tid = item.get("task_id") or repo.find_terminal_task(item["skill_name"], item["arguments"])
        if not tid:
            continue
        item["task_id"] = tid
        skill = get_skill(item["skill_name"])
        if not skill or not skill.get("is_multi_agent"):
            continue
        try:
            artifacts = repo.get_artifacts(tid)
        except Exception:
            continue
        if any(not c.startswith("[错误]") for _, c in artifacts):
            item["resumable"] = True
    return {"reports": reports[:50]}


@router.get("/api/reports/{filename}")
async def api_get_report(filename: str):
    safe_name = _sanitize_filename(filename)
    report_path = (_wc.REPORTS_DIR / safe_name).resolve()
    try:
        report_path.relative_to(_wc.REPORTS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path outside reports directory")
    if not report_path.exists():
        return {"error": "Report not found"}
    content = report_path.read_text(encoding="utf-8")
    result = {"filename": safe_name, "content": content}
    meta_path = report_path.with_suffix(".meta.json")
    if meta_path.exists():
        try:
            result["meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return result


@router.delete("/api/reports/{filename}")
async def api_delete_report(filename: str):
    """Delete a report and its metadata sidecar."""
    safe_name = _sanitize_filename(filename)
    report_path = (_wc.REPORTS_DIR / safe_name).resolve()
    try:
        report_path.relative_to(_wc.REPORTS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path outside reports directory")
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    report_path.unlink()
    meta_path = report_path.with_suffix(".meta.json")
    if meta_path.exists():
        meta_path.unlink()
    return {"deleted": safe_name}

# ── P3: decision log API ───────────────────────────────────────

@router.get("/api/decisions")
async def api_list_decisions():
    """P3: list past investment decisions from decision-log.md, newest first."""
    from ..harness.decision_log import DECISION_LOG_PATH, list_decisions
    entries = list_decisions(max_entries=100)
    pending = sum(1 for e in entries if e.get("status") == "pending")
    return {
        "decisions": entries,
        "total": len(entries),
        "pending": pending,
        "log_exists": DECISION_LOG_PATH.exists(),
    }


@router.post("/api/decisions/{task_id}/resolve")
async def api_resolve_decision(task_id: str):
    """P3-闭环(2026-08-28): manually resolve a pending decision by task ID."""
    from ..harness.decision_log import resolve_decision_by_task_id
    ok = resolve_decision_by_task_id(task_id)
    if not ok:
        return {"resolved": False,
                "message": "未找到该任务ID对应的待验证决策，或其已销账"}
    return {"resolved": True, "task_id": task_id}


# ── P4: PDF export ──────────────────────────────────────────────

# 独立 HTML 查看页（双击报告 → 新标签页）内联样式。
# 与 style.css 的 :root 变量 + .markdown-body 规则保持一致，
# 保证新页面字体/配色/排版与 app 内完全一致，且不依赖主站 CSS。
_REPORT_VIEW_CSS = """
:root {
  --bg: #f7f3e9; --bg-secondary: #f0ebe0; --bg-card: #fdfaf5;
  --border: #d4c5a9; --border-light: #e5dac8;
  --text: #3d3d3d; --text-secondary: #8c8273;
  --accent: #6b8e6b; --accent-yellow: #c4a77d; --ink: #3d3d3d;
  --radius: 4px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: 'Noto Serif SC', 'Source Han Serif SC', 'STSong', 'SimSun', 'Songti SC', Georgia, 'Times New Roman', serif;
  background: var(--bg);
  background-image:
    radial-gradient(ellipse at 85% 15%, rgba(196,167,125,0.06) 0%, transparent 60%),
    radial-gradient(ellipse at 15% 90%, rgba(107,142,107,0.04) 0%, transparent 50%);
  color: var(--text);
  line-height: 1.85;
  min-height: 100vh;
}
.report-shell { max-width: 1100px; margin: 0 auto; padding: 36px 28px 80px; }
.report-shell h1 { font-size: 1.35rem; color: var(--ink); margin: 32px 0 14px; padding-bottom: 10px; border-bottom: 1px solid var(--border-light); font-weight: 400; letter-spacing: 0.04em; }
.report-shell h2 { font-size: 1.1rem; margin: 28px 0 10px; font-weight: 400; color: var(--accent); letter-spacing: 0.03em; }
.report-shell h3 { font-size: 0.95rem; margin: 20px 0 8px; color: var(--accent-yellow); font-weight: 400; }
.report-shell p { margin: 10px 0; }
.report-shell table { width: 100%; border-collapse: collapse; margin: 14px 0; font-size: 0.78rem; }
.report-shell th { background: var(--bg-secondary); padding: 9px 14px; text-align: left; border-bottom: 1px solid var(--border); font-weight: 400; color: var(--text-secondary); letter-spacing: 0.04em; font-size: 0.72rem; text-transform: uppercase; }
.report-shell td { padding: 7px 14px; border-bottom: 1px solid var(--border-light); }
.report-shell code { background: var(--bg-secondary); padding: 2px 6px; border-radius: 2px; font-size: 0.85em; color: var(--accent); }
.report-shell pre { background: var(--bg-secondary); padding: 16px; border-radius: var(--radius); overflow-x: auto; border: 1px solid var(--border-light); margin: 14px 0; }
.report-shell blockquote { border-left: 2px solid var(--accent-yellow); padding: 8px 18px; margin: 14px 0; color: var(--text-secondary); font-style: italic; }
.report-shell hr { border: none; border-top: 1px solid var(--border-light); margin: 28px 0; }
.report-shell ul, .report-shell ol { padding-left: 22px; margin: 8px 0; }
.report-shell li { margin: 5px 0; }
.report-shell strong { color: var(--ink); font-weight: 600; }
.report-shell .ai-disclaimer { border-top: 1px solid var(--border-light); margin-top: 40px; padding-top: 12px; font-size: 0.8rem; color: var(--text-secondary); text-align: center; }
.report-toolbar { position: sticky; top: 0; background: var(--bg); padding: 10px 0; margin-bottom: 8px; border-bottom: 1px solid var(--border-light); display: flex; gap: 10px; align-items: center; font-size: 0.78rem; color: var(--text-secondary); }
.report-toolbar a, .report-toolbar button { background: none; border: 1px solid var(--border); color: var(--accent); padding: 5px 14px; border-radius: var(--radius); font-size: 0.78rem; cursor: pointer; text-decoration: none; font-family: inherit; }
.report-toolbar a:hover, .report-toolbar button:hover { border-color: var(--accent); }
/* ── 打印分页控制：表格/代码块整体不切断、行不劈开、标题不孤悬页底 ── */
.report-shell table, .report-shell pre, .report-shell blockquote { break-inside: avoid; page-break-inside: avoid; }
.report-shell tr { break-inside: avoid; page-break-inside: avoid; }
.report-shell h1, .report-shell h2, .report-shell h3 { break-after: avoid; page-break-after: avoid; }
@media print {
  .report-toolbar { display: none !important; }
  .report-shell { max-width: 100%; padding: 0; }
}
"""


@router.post("/api/report/view")
async def api_report_view(request: Request):
    """把报告的 markdown 渲染成独立 HTML 文件（内联完整样式），落盘到
    data/reports/standalone/，返回新标签页 URL。

    Request: {"markdown": "...", "filename": "可选建议名"}
    Response: {"url": "/report-view/<file>.html", "filename": ...}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    markdown_text = body.get("markdown", "")
    if not markdown_text or len(markdown_text) < 50:
        raise HTTPException(status_code=400, detail="Markdown content too short")

    # 文件名：前端建议名 → sanitize → 加时间戳防覆盖
    base_name = _sanitize_filename(body.get("filename") or "report") or "report"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{base_name}_{ts}.html"

    standalone_dir = _wc.REPORTS_DIR / "standalone"
    standalone_dir.mkdir(parents=True, exist_ok=True)
    out_path = (standalone_dir / file_name).resolve()
    try:
        out_path.relative_to(standalone_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid filename")

    try:
        from markdown_it import MarkdownIt
        _md_parser = MarkdownIt("commonmark", {"html": True}).enable("table")
        html_body = _md_parser.render(markdown_text)
    except ImportError:
        html_body = markdown_text.replace("\n", "<br>")

    # 标题：取 markdown 第一个 # 标题，否则用文件名
    import re as _re
    m = _re.search(r"^#\s+(.+)$", markdown_text, _re.MULTILINE)
    title = (m.group(1).strip() if m else file_name.replace(".html", ""))[:120]

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{_REPORT_VIEW_CSS}</style>
</head>
<body>
<div class="report-shell">
<div class="report-toolbar">
  <span>📄 {title}</span>
  <span style="flex:1"></span>
  <button onclick="window.print()">🖨 打印</button>
  <a href="/app">← 返回</a>
</div>
{html_body}
<div class="ai-disclaimer">该数据由AI在数据的基础上进行分析，仅供参考</div>
</div>
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")
    return {"url": f"/report-view/{file_name}", "filename": file_name}


@router.get("/report-view/{filename}")
async def api_report_view_page(filename: str):
    """Serve a standalone report HTML file (new-tab view)."""
    safe_name = _sanitize_filename(filename)
    if not safe_name:
        raise HTTPException(status_code=404, detail="Report not found")
    standalone_dir = _wc.REPORTS_DIR / "standalone"
    report_path = (standalone_dir / safe_name).resolve()
    try:
        report_path.relative_to(standalone_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    content = report_path.read_text(encoding="utf-8")
    return Response(content=content, media_type="text/html; charset=utf-8")


@router.get("/api/stats/costs")
async def api_cost_stats(days: int = Query(default=30, ge=1, le=365), model: str = Query(default=None, max_length=300)):
    """LLM cost/token aggregation for the cost dashboard.

    Counts only terminal tasks that actually ran (completed / failed /
    cancelled); running and interrupted tasks are excluded so partial
    or zero billing does not skew the averages. `by_day` covers the
    last `days` calendar days (including today) with zero-filled gaps.
    `model`（可选）按模型过滤；`by_model` 始终返回模型维度分组。
    """
    repo = get_repository()
    return await repo.aget_cost_stats(days, model)


# ── P4: PDF export ──────────────────────────────────────────────

_PDF_CSS = """
@page { size: A4; margin: 2cm 2.2cm; @bottom-center { content: counter(page); font-size: 0.7rem; color: #999; } }
body { font-family: "Noto Serif SC", "Source Han Serif SC", "SimSun", serif; font-size: 11pt; line-height: 1.8; color: #333; }
h1 { font-size: 1.5rem; border-bottom: 1px solid #ccc; padding-bottom: 8px; }
h2 { font-size: 1.2rem; color: #6b8e6b; margin-top: 24px; }
h3 { font-size: 1rem; color: #c4a77d; }
table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 9pt; }
th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; }
th { background: #f5f5f5; }
pre { background: #f8f8f8; padding: 10px; border-radius: 4px; font-size: 9pt; overflow-x: auto; }
code { background: #f0f0f0; padding: 1px 4px; font-size: 0.9em; }
blockquote { border-left: 3px solid #6b8e6b; padding-left: 12px; color: #666; margin: 12px 0; }
.ai-disclaimer { border-top: 1px solid #ddd; margin-top: 24px; padding-top: 8px; font-size: 9pt; color: #999; text-align: center; }
"""


@router.post("/api/export/pdf")
async def api_export_pdf(request: Request):
    """P4: Convert markdown report to PDF with Chinese font support."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    markdown_text = body.get("markdown", "")
    if not markdown_text or len(markdown_text) < 50:
        raise HTTPException(status_code=400, detail="Markdown content too short")

    try:
        from markdown_it import MarkdownIt
        _md_parser = MarkdownIt("commonmark", {"html": True}).enable("table")
        html_body = _md_parser.render(markdown_text)
    except ImportError:
        # Fallback: basic conversion
        html_body = markdown_text.replace("\n", "<br>")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><style>{_PDF_CSS}</style></head>
<body>{html_body}</body>
</html>"""

    try:
        from weasyprint import HTML
        pdf_bytes = HTML(string=html).write_pdf()
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": "inline; filename=report.pdf"},
        )
    except Exception as e:
        logger.error(f"PDF generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

@router.post("/api/tools/{tool_name}")
async def api_run_tool(tool_name: str, params: dict = None):
    """Run a financial tool (sync tools only via this endpoint)."""
    from ..tools import benford, calc, cross_validate, three_scenario, verify_market_cap, verify_valuation
    tool_map = {
        "verify-market-cap": verify_market_cap,
        "verify-valuation": verify_valuation,
        "cross-validate": cross_validate,
        "three-scenario": three_scenario,
        "calc": calc,
        "benford": benford,
    }
    if tool_name not in tool_map:
        return {"error": f"Unknown tool: {tool_name}", "available": list(tool_map.keys())}
    try:
        result = tool_map[tool_name](**(params or {}))
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}
