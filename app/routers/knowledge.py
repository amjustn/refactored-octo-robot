"""Knowledge routes: /api/knowledge*, /api/skill-knowledge*."""
from fastapi import APIRouter, HTTPException, Request

from .. import web_common as _wc

router = APIRouter()


# ==================== Knowledge Base API ====================

@router.get("/api/knowledge/status")
async def api_knowledge_status():
    """Get knowledge base status for all investment masters."""
    from ..tools.knowledge_updater import get_knowledge_status
    return {"masters": get_knowledge_status()}


@router.get("/api/knowledge/{master_key}")
async def api_get_knowledge(master_key: str):
    """Get knowledge for a specific investment master."""
    from ..tools.knowledge_updater import get_master_knowledge
    return get_master_knowledge(master_key)


@router.get("/api/knowledge")
async def api_get_all_knowledge():
    """Get all knowledge base entries."""
    from ..tools.knowledge_updater import get_all_knowledge
    return {"knowledge": get_all_knowledge()}


@router.post("/api/knowledge/update")
async def api_update_knowledge(request: Request, master_key: str = None):
    """Trigger knowledge base update. If master_key is specified, only update that master."""
    # F4: this triggers outbound fetches + LLM work — rate-limit like the others.
    if not _wc._check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    from ..tools.knowledge_updater import update_all_masters, update_master_knowledge
    if master_key:
        result = await update_master_knowledge(master_key)
    else:
        result = await update_all_masters()
    return {"result": result}


# ==================== Skill Knowledge API ====================

@router.get("/api/skill-knowledge/status")
async def api_skill_knowledge_status():
    """Get self-update status for ALL 21 skills' domain knowledge."""
    from ..tools.skill_knowledge import get_all_skill_knowledge_status
    return {"skills": get_all_skill_knowledge_status()}


@router.get("/api/skill-knowledge/{skill_name}")
async def api_get_skill_knowledge(skill_name: str):
    """Get domain knowledge for a specific skill."""
    from ..tools.skill_knowledge import get_skill_knowledge
    data = get_skill_knowledge(skill_name)
    return data


@router.post("/api/skill-knowledge/update")
async def api_update_skill_knowledge(skill_name: str = None):
    """Trigger skill knowledge update. If skill_name is specified, only update that skill."""
    from ..tools.skill_knowledge import update_all_skill_knowledge, update_skill_knowledge
    if skill_name:
        result = await update_skill_knowledge(skill_name)
    else:
        result = await update_all_skill_knowledge()
    return {"result": result}


# ==================== Sector Knowledge API（板块知识库，2026-09-22） ====================

@router.get("/api/sector-knowledge/status")
async def api_sector_knowledge_status():
    """Get status for all 31 sectors' knowledge bases."""
    from ..tools.sector_knowledge import get_all_sector_knowledge_status
    return {"sectors": get_all_sector_knowledge_status()}


@router.get("/api/sector-knowledge/list")
async def api_sector_knowledge_list():
    """List registered sectors (申万一级 31 个) with routing aliases."""
    from ..tools.sector_knowledge import list_sectors
    return {"sectors": list_sectors()}


@router.get("/api/sector-knowledge/{sector_key}")
async def api_get_sector_knowledge(sector_key: str):
    """Get one sector's 4-perspective knowledge."""
    from ..tools.sector_knowledge import get_sector_knowledge
    return get_sector_knowledge(sector_key)


@router.post("/api/sector-knowledge/update")
async def api_update_sector_knowledge(request: Request, sector_key: str = None):
    """Trigger sector knowledge update. If sector_key is given, only that sector."""
    if not _wc._check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    from ..tools.sector_knowledge import update_all_sector_knowledge, update_sector_knowledge
    if sector_key:
        result = await update_sector_knowledge(sector_key)
    else:
        result = await update_all_sector_knowledge()
    return {"result": result}
