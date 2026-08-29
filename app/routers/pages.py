"""Page routes: /, /manual, /app, /health, /favicon.ico (+ MANUAL_HTML)."""
import json
from datetime import datetime
from html import escape as _html_escape

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import web_common as _wc  # late-bound REPORTS_DIR (tests monkeypatch web_common)
from ..core.config import BERKSHIRE_API_TOKEN
from ..core.jwt_utils import create_token
from ..harness import active_count as harness_active_count
from ..skills import list_skills
from ..web_common import data_cache, static_dir, task_store, templates_dir

router = APIRouter()


# ==================== 使用手册 · 技能表（服务端动态生成，永不过期） ====================
# 输入列短提示（手工维护一次性映射；新技能未收录时回退到 input_hint 截断）
_SHORT_HINTS = {
    "investment-research": "公司名/代码",
    "investment-team": "公司名/代码",
    "dyp-ask": "投资问题",
    "investment-checklist": "多公司",
    "management-deep-dive": "管理层+公司名",
    "private-company-research": "公司名",
    "broker-reports": "A股代码/公司名",
    "deep-company-series": "公司名",
    "global-macro-analysis": "宏观问题",
    "china-macro-analysis": "宏观问题",
    "wechat-article": "主题",
    "earnings-review": "公司名+季度",
    "earnings-team": "公司名+季度",
    "financial-data": "股票代码",
    "industry-research": "行业名",
    "industry-funnel": "行业/方向",
    "quality-screen": "个股/行业",
    "bottleneck-hunter": "趋势名",
    "portfolio-review": "持仓清单",
    "thesis-tracker": "公司名",
    "news-pulse": "公司名 [涨跌]",
    "daily-briefing": "留空 / 关注方向",
}


def _short_hint(name: str, hint: str) -> str:
    """输入列短提示：优先手工映射，否则从 input_hint 提取。"""
    if name in _SHORT_HINTS:
        return _SHORT_HINTS[name]
    if not hint:
        return "分析目标"
    h = hint.split("如：")[0].strip().split("，")[0].strip()
    for pre in (
        "描述你的分析目标", "描述筛选目标", "描述研究目标", "描述想研究的",
        "描述一个超级趋势", "描述你的持仓", "描述分析目标", "描述你想分析的",
        "描述异动情况", "描述筛选对象", "描述需要的数据", "描述文章主题",
    ):
        h = h.replace(pre, "")
    h = h.strip("，,、；;。 ")
    if not h:
        return "分析目标"
    if len(h) > 14:
        h = h[:13] + "…"
    return h


def _render_skills_table() -> str:
    """按 category 分组渲染技能速查表（数据源 registry.SKILLS，随技能增减自动更新）。"""
    groups = {}
    for s in list_skills():
        groups.setdefault(s.get("category", "其他"), []).append(s)
    parts = []
    for cat, skills in groups.items():
        parts.append(f"<h3>{_html_escape(cat)}</h3>")
        parts.append('<table><tr><th>技能名</th><th>显示名</th><th>输入</th><th>说明</th></tr>')
        for s in skills:
            name = s["name"]
            disp = _html_escape(s.get("display_name", name))
            if s.get("is_multi_agent") and s.get("agent_count"):
                disp += f'<span class="tag">{int(s["agent_count"])}A</span>'
            hint = _html_escape(_short_hint(name, s.get("input_hint", "")))
            desc = _html_escape(s.get("description", ""))
            parts.append(
                f'<tr><td><code>{name}</code></td><td>{disp}</td>'
                f"<td>{hint}</td><td>{desc}</td></tr>"
            )
        parts.append("</table>")
    return "\n".join(parts)


# ==================== 使用手册 ====================
MANUAL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI Berkshire 使用手册</title>
<style>
  body{font-family:'Noto Serif SC','Source Han Serif SC','STSong',Georgia,'Times New Roman',serif;max-width:760px;margin:0 auto;padding:32px 24px 60px;background:#f7f3e9;color:#3d3d3d;line-height:1.85;letter-spacing:0.03em}
  h1{color:#3d3d3d;font-weight:400;font-size:1.4rem;border-bottom:1px solid #d4c5a9;padding-bottom:14px;margin-bottom:28px;letter-spacing:0.06em}
  h2{color:#6b8e6b;font-weight:400;margin-top:36px;font-size:1.1rem;letter-spacing:0.05em}
  h3{color:#c4a77d;font-weight:400;font-size:0.95rem;margin-top:24px}
  table{width:100%;border-collapse:collapse;margin:14px 0;font-size:0.82rem}
  th,td{border-bottom:1px solid #e5dac8;padding:8px 12px;text-align:left}
  th{background:#f0ebe0;color:#8c8273;font-weight:400;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.04em}
  td{background:#fdfaf5}
  code{background:#f0ebe0;padding:2px 6px;border-radius:2px;color:#6b8e6b;font-size:0.88em}
  pre{background:#f0ebe0;padding:14px;border-radius:4px;overflow-x:auto;border:1px solid #e5dac8}
  a{color:#6b8e6b;text-decoration:none;border-bottom:1px solid #d4c5a9}
  a:hover{color:#3d3d3d}
  .note{background:#fdfaf5;border-left:2px solid #c4a77d;padding:10px 16px;margin:18px 0;font-size:0.82rem;color:#8c8273}
  .tag{display:inline-block;background:#f0ebe0;color:#8c8273;font-size:0.65rem;padding:1px 6px;border-radius:2px;margin-left:4px;vertical-align:middle}
  ul,ol{padding-left:22px}
  li{margin:5px 0}
</style>
</head>
<body>
<h1>AI Berkshire Web — 价值投资研究工作站 使用指南</h1>
<p>一个人 + AI = 一个投研团队。地址：<code>http://localhost:8001/</code></p>

<div style="text-align:center;margin:24px 0">
<a href="/app" style="display:inline-block;background:#6b8e6b;color:#fff;padding:12px 32px;border-radius:4px;text-decoration:none;font-size:1rem;letter-spacing:0.06em;border:none">→ 进入研究工作站</a>
</div>

<h2>技能速查（@@SKILL_COUNT@@个技能）</h2>

@@SKILLS_TABLE@@

<h2>典型使用场景</h2>
<ol>
<li><b>快速评估</b>：选「多Agent投研团队」→ 输入「美团」→ 开始研究</li>
<li><b>对比多家</b>：选「巴菲特Checklist」→ 输入「茅台,五粮液,泸州老窖」</li>
<li><b>行业选股</b>：选「行业漏斗筛选」→ 输入「机器人」</li>
<li><b>持仓体检</b>：选「组合审视」→ 输入「腾讯30%,美团20%,茅台20%,现金30%」</li>
<li><b>新闻归因</b>：选「新闻脉搏」→ 输入「腾讯 -5%」</li>
<li><b>每日复盘</b>：选「金融日报」→ 直接点开始（无需输入）</li>
</ol>

<h2>功能一览</h2>
<table>
<tr><th>功能</th><th>说明</th><th>位置</th></tr>
<tr><td>流式输出</td><td>实时看到 AI 生成内容，支持中途取消</td><td>默认开启</td></tr>
<tr><td>进度显示</td><td>多Agent模式下每个Agent独立显示进度条</td><td>研究时自动显示</td></tr>
<tr><td>复制报告</td><td>一键复制 Markdown 全文到剪贴板</td><td>结果区工具栏</td></tr>
<tr><td>下载 MD</td><td>下载原始 Markdown 文件</td><td>结果区工具栏</td></tr>
<tr><td>导出 HTML</td><td>导出带和风样式的独立 HTML 页面</td><td>结果区工具栏</td></tr>
<tr><td>打印 / PDF</td><td>浏览器打印，可另存为 PDF</td><td>结果区工具栏</td></tr>
<tr><td>目录</td><td>自动提取标题生成侧边目录导航</td><td>结果区工具栏</td></tr>
<tr><td>历史报告</td><td>查看、搜索、删除历史报告，支持两份对比</td><td>左下角按钮</td></tr>
<tr><td>技能收藏</td><td>点击技能旁的 ★ 收藏，置顶显示</td><td>左侧技能列表</td></tr>
<tr><td>技能搜索</td><td>关键词筛选技能，支持名称/描述搜索</td><td>左侧搜索框</td></tr>
<tr><td>输入历史</td><td>自动记录分析目标，下次可快速选择</td><td>输入框下拉</td></tr>
<tr><td>快捷键</td><td>Enter 直接开始研究</td><td>全局</td></tr>
<tr><td>移动端适配</td><td>点击左上角 ☰ 展开/收起侧边栏</td><td>左上角按钮</td></tr>
<tr><td>实时市场数据</td><td>研究时自动注入最新行情/财务/新闻</td><td>后台自动</td></tr>
<tr><td>换肤系统</td><td>🎨 12套皮肤（6常规+6渐变）/ 强调色 / 背景照片 / 表面透明度，自动保存</td><td>右下角 🎨 按钮</td></tr>
<tr><td>登录认证</td><td>本地 BasicAuth 登录 + JWT 自动注入，/manual 免登录</td><td>浏览器弹窗</td></tr>
<tr><td>数据源健康</td><td>各行情/新闻数据源实时状态与自动熔断摘除</td><td>健康检查</td></tr>
<tr><td>知识库自动更新</td><td>启动时自动更新市场知识与技能知识</td><td>后台自动</td></tr>
<tr><td>LLM 模型选择</td><td>切换默认大模型，一键测试连通性</td><td>设置面板</td></tr>
<tr><td>成本统计</td><td>查看 API 调用量/Token/费用，按模型与技能分布</td><td>设置面板</td></tr>
<tr><td>任务恢复</td><td>中断/失败任务可一键恢复继续生成</td><td>历史报告</td></tr>
<tr><td>报告追问</td><td>报告底部直接追问，深入某个细节</td><td>结果区</td></tr>
<tr><td>报告对比</td><td>两份历史报告左右分栏对比</td><td>历史报告面板</td></tr>
<tr><td>券商研报直查</td><td>按 A 股代码查询机构研报评级与一致预期</td><td>技能输入</td></tr>
</table>

<h2>换肤（渐变皮肤）</h2>
<div class="note">右下角 <b>🎨</b> 悬浮按钮打开换肤面板。所有设置自动保存，并在本机站点间同步。</div>
<table>
<tr><th>项目</th><th>说明</th></tr>
<tr><td>常规皮肤（6套）</td><td>和風·禅 / 抹茶·清 / 桜·粉 / 紺青·夜 / 墨·黑 / 夜·紫</td></tr>
<tr><td>渐变皮肤（6套）</td><td>雾蓝·樱粉 / 湖蓝·淡粉 / 晨雾·青 / 奶油·蜜桃 / 暮山·紫 / 深海·蓝 —— 柔和克制的莫兰迪色系，渐变铺满整页背景</td></tr>
<tr><td>强调色</td><td>自定义强调色，或一键回到跟随皮肤</td></tr>
<tr><td>背景照片</td><td>上传自己的照片作为背景，透明度/模糊可调；照片与渐变皮肤<b>叠加共存</b>，互不覆盖</td></tr>
<tr><td>表面透明度</td><td>侧边栏/输入区半透明，透出背景（卡片保持实心保证可读性）</td></tr>
<tr><td>一键浅色</td><td>照片场景下快速切换 透明度25% + 模糊8px + 侧边栏透出</td></tr>
</table>

<h2>AI 免责声明</h2>
<div class="note">
每条报告末尾均标注：<b>「该数据由AI在数据的基础上进行分析，仅供参考」</b>。
所有分析内容由大语言模型基于公开数据生成，不构成任何投资建议。
投资决策请以个人独立判断为准，市场有风险，投资需谨慎。
</div>

<div class="note"><b>耗时参考</b>：单Agent 1-3分钟，多Agent 3-8分钟，深度系列15-30分钟，日报 2-5分钟。</div>
</body>
</html>"""

@router.get("/manual", response_class=HTMLResponse)
async def manual():
    """使用手册 — 技能表由服务端按 registry 动态生成（数量/内容永不过期）"""
    html = MANUAL_HTML.replace("@@SKILL_COUNT@@", str(len(list_skills())))
    html = html.replace("@@SKILLS_TABLE@@", _render_skills_table())
    return HTMLResponse(html)


@router.get("/", response_class=HTMLResponse)
async def index():
    """Go directly to the research workstation."""
    return RedirectResponse(url="/app", status_code=302)


@router.get("/app", response_class=HTMLResponse)
async def app_index():
    """Main application — auto-injects JWT for authenticated users."""
    index_path = templates_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>AI Berkshire Web</h1><p>Loading...</p>")

    html = index_path.read_text(encoding="utf-8")

    # Inject JWT into the page so the frontend picks it up automatically.
    # BasicAuthMiddleware already verified the user — no need to prompt again.
    # F6: acceptable for the single-user / local deployment this app targets.
    # Never share an /app page URL — whoever holds the URL holds the token.
    if BERKSHIRE_API_TOKEN:
        jwt_token = create_token(BERKSHIRE_API_TOKEN)
        script_tag = (
            f'<script>window.__JWT__={json.dumps(jwt_token)};'
            f'localStorage.setItem("ai_berkshire_jwt",{json.dumps(jwt_token)});</script>'
        )
        html = html.replace("</head>", script_tag + "</head>")

    return HTMLResponse(html)


@router.get("/health")
async def health():
    from ..core.config import LLM_API_KEY, LLM_MODEL
    from ..tools.knowledge_updater import get_knowledge_status
    from ..tools.skill_knowledge import get_all_skill_knowledge_status
    return {
        "status": "ok",
        "version": "3.0.0",
        "llm_configured": bool(LLM_API_KEY),
        "llm_model": LLM_MODEL,
        "skills_count": len(list_skills()),
        "reports_count": len(list(_wc.REPORTS_DIR.glob("*.md"))) if _wc.REPORTS_DIR.exists() else 0,
        "active_tasks": harness_active_count(),
        "db_size": task_store.db_path.stat().st_size if task_store.db_path.exists() else 0,
        "data_cache": data_cache.get_stats(),
        "knowledge_status": get_knowledge_status(),
        "skill_knowledge_status": get_all_skill_knowledge_status(),
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/favicon.ico")
async def favicon():
    favicon_path = static_dir / "favicon.svg"
    if favicon_path.exists():
        return FileResponse(str(favicon_path), media_type="image/svg+xml")
    return HTMLResponse("")
