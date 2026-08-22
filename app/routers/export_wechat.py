"""Export routes: /api/export/* — wechat push for finished reports."""
from fastapi import APIRouter, HTTPException, Request

from ..web_common import logger

router = APIRouter()


@router.post("/api/export/wechat")
async def api_export_wechat(request: Request):
    """Push the current report to WeChat (Server酱 webhook).

    Body: {"markdown": "...", "title": "可选"}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    markdown_text = (body.get("markdown") or "").strip()
    if not markdown_text:
        raise HTTPException(status_code=400, detail="缺少 markdown 内容")

    from ..tools.report_export import push_report_wechat

    result = push_report_wechat(markdown_text, title=body.get("title"))
    if not result.get("ok"):
        # 未配置 webhook 或推送失败 → 4xx/5xx，前端提示
        status = 500 if result.get("status") else 400
        raise HTTPException(status_code=status, detail=result.get("error", "推送失败"))
    return {"success": True, "result": result}
