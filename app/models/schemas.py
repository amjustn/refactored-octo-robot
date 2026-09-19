"""Pydantic models for AI Berkshire Web API"""
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class SkillCategory(str, Enum):
    RESEARCH = "深度研究"
    EARNINGS = "财报分析"
    INDUSTRY = "行业筛选"
    PORTFOLIO = "持仓管理"
    THINKING = "思维工具"
    DAILY = "日报"


class SkillInfo(BaseModel):
    name: str
    display_name: str
    description: str
    category: SkillCategory
    is_multi_agent: bool = False
    agent_count: int = 1
    series_mode: bool = False
    series_topics: List[str] = []


class ResearchRequest(BaseModel):
    skill_name: str = Field(..., description="Skill name (e.g. investment-team)")
    arguments: str = Field(..., max_length=10000, description="Target company/topic/parameters or a free-form analysis goal")
    attachments: Optional[str] = Field(default=None, max_length=30000, description="Extracted text from user-uploaded reference files (travels separately from arguments)")
    stream: bool = Field(default=False, description="Stream the response")
    llm_config: Optional[dict] = Field(default=None, description="Per-request LLM override: {api_key, base_url, model, synthesis_model}（synthesis_model 为 Team Lead 综合环节可选的独立模型）")
    debate_confirm: bool = Field(default=False, description="HITL：multi 策略辩论完成后暂停等待人工确认（默认关闭）")
    debate_tools: Optional[bool] = Field(default=None, description="P5：交叉辩论开放受限核对工具（复算/原始数据白名单）；None=沿用 skill 元数据，默认关闭")
    debate_rebuttal: Optional[bool] = Field(default=None, description="P5：被评审者获得一轮答辩权，筛掉截断造成的假分歧；None=沿用 skill 元数据，默认关闭")
    debate_stance: Optional[bool] = Field(default=None, description="P5：立场卡（防漂移）+ 法官分离；None=沿用 skill 元数据，默认关闭")
    debate_tools: Optional[bool] = Field(default=None, description="P5：交叉辩论开放受限核对工具（复算/原始数据白名单）；None=沿用 skill 元数据，默认关闭")
    debate_rebuttal: Optional[bool] = Field(default=None, description="P5：被评审者获得一轮答辩权，筛掉截断造成的假分歧；None=沿用 skill 元数据，默认关闭")
    debate_stance: Optional[bool] = Field(default=None, description="P5：立场卡（防漂移）+ 法官分离；None=沿用 skill 元数据，默认关闭")


class AgentProgress(BaseModel):
    agent_name: str
    status: str  # pending, running, completed, failed
    message: str = ""
    progress: float = 0.0


class ResearchStatus(BaseModel):
    task_id: str
    skill_name: str
    status: str  # queued, running, completed, failed, interrupted
    agents: List[AgentProgress] = []
    report: str = ""
    error: str = ""


class ToolRequest(BaseModel):
    tool_name: str
    params: dict = {}


class ToolResult(BaseModel):
    success: bool
    output: str = ""
    error: str = ""
