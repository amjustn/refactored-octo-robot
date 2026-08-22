"""Skin settings persistence — 换肤设置服务端存储（跨浏览器/设备同步）

GET  /api/skin  → {"code":0,"data": settings|null}
POST /api/skin  → 保存（字段白名单 + 原子写入 + 4MB 上限）
壁纸以 dataURL 随设置保存（前端已压缩 ≤2MB）。
"""
import json
import os
import tempfile

from fastapi import APIRouter

router = APIRouter()

_DATA_DIR = os.getenv("SKIN_DATA_DIR", os.path.expanduser("~/.ai_berkshire_skin"))
_SKIN_FILE = os.path.join(_DATA_DIR, "skin_settings.json")
_MAX_BYTES = 4 * 1024 * 1024
_ALLOWED = {"skin", "accent", "wallpaper", "wpOpacity", "wpBlur", "sidebarAlpha", "gradient"}


@router.get("/api/skin")
async def get_skin():
    if not os.path.exists(_SKIN_FILE):
        return {"code": 0, "data": None}
    try:
        with open(_SKIN_FILE, "r", encoding="utf-8") as f:
            return {"code": 0, "data": json.load(f)}
    except Exception:
        return {"code": 0, "data": None}


@router.post("/api/skin")
async def save_skin(body: dict):
    payload = {k: v for k, v in body.items() if k in _ALLOWED}
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode("utf-8")) > _MAX_BYTES:
        return {"code": 1, "message": "设置数据过大（>4MB），请换更小的壁纸图片"}
    os.makedirs(_DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, _SKIN_FILE)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return {"code": 1, "message": f"保存失败: {e}"}
    return {"code": 0, "message": "已保存"}
