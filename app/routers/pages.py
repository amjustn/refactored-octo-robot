"""Page routes: /, /manual, /app, /health, /favicon.ico (+ MANUAL_HTML)."""
import json
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import web_common as _wc  # late-bound REPORTS_DIR (tests monkeypatch web_common)
from ..core.config import BERKSHIRE_API_TOKEN
from ..core.jwt_utils import create_token
from ..harness import active_count as harness_active_count
from ..skills import list_skills
from ..web_common import data_cache, static_dir, task_store, templates_dir

router = APIRouter()


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

<h2>技能速查（19个技能）</h2>

<h3>深度研究</h3>
<table>
<tr><th>技能名</th><th>显示名</th><th>输入</th><th>说明</th></tr>
<tr><td><code>investment-team</code></td><td>多Agent投研团队<span class="tag">4A</span></td><td>公司名/代码</td><td>★推荐★ 商业模式/财务/护城河/估值 4Agent并行</td></tr>
<tr><td><code>investment-research</code></td><td>四大师综合分析</td><td>公司名/代码</td><td>巴菲特+芒格+段永平+李录 四大视角</td></tr>
<tr><td><code>dyp-ask</code></td><td>段永平问答</td><td>投资问题</td><td>以段永平思维框架回答投资问题</td></tr>
<tr><td><code>investment-checklist</code></td><td>巴菲特Checklist</td><td>多公司</td><td>六关快速筛选，支持多公司对比</td></tr>
<tr><td><code>management-deep-dive</code></td><td>管理层深研</td><td>管理层名 公司</td><td>履历/能力圈/诚信/资本配置 全景评估（输入公司名即可）</td></tr>
<tr><td><code>private-company-research</code></td><td>未上市公司研究</td><td>公司名</td><td>侦探式公开信息挖掘</td></tr>
<tr><td><code>deep-company-series</code></td><td>深度系列长文</td><td>公司名</td><td>8篇系列专题（~12万字），逐篇生成</td></tr>
<tr><td><code>wechat-article</code></td><td>公众号文章<span class="tag">3A</span></td><td>主题</td><td>3Agent并行生成公众号风格长文</td></tr>
</table>

<h3>财报分析</h3>
<table>
<tr><td><code>earnings-review</code></td><td>财报精读</td><td>公司名 季度</td><td>单Agent深度解读，营收/利润/现金流拆解</td></tr>
<tr><td><code>earnings-team</code></td><td>财报精读团队<span class="tag">6A</span></td><td>公司名 季度</td><td>6Agent并行 + 首席审稿，全面覆盖</td></tr>
<tr><td><code>financial-data</code></td><td>财务数据规范</td><td>股票代码</td><td>交叉验证的财务数据提取</td></tr>
</table>

<h3>行业筛选</h3>
<table>
<tr><td><code>industry-research</code></td><td>产业链全景扫描</td><td>行业名</td><td>上中下游格局、竞争态势、政策环境</td></tr>
<tr><td><code>industry-funnel</code></td><td>行业漏斗筛选</td><td>行业/方向</td><td>30-60家公司 → 精筛 → 终选3家</td></tr>
<tr><td><code>quality-screen</code></td><td>去劣筛选</td><td>个股/行业</td><td>7项硬指标快速排除劣质公司</td></tr>
<tr><td><code>bottleneck-hunter</code></td><td>瓶颈猎手</td><td>趋势名</td><td>产业链物理瓶颈识别</td></tr>
</table>

<h3>持仓管理</h3>
<table>
<tr><td><code>portfolio-review</code></td><td>组合审视</td><td>持仓清单</td><td>仓位体检 + 调仓建议</td></tr>
<tr><td><code>thesis-tracker</code></td><td>持仓评估</td><td>公司名</td><td>持仓健康度评估：假设检查+红线触发+买卖信号</td></tr>
<tr><td><code>news-pulse</code></td><td>股价异动归因<span class="tag">4A</span></td><td>公司名 [涨跌]</td><td>4Agent并行归因，信息不实时立即报错</td></tr>
</table>

<h3>日报</h3>
<table>
<tr><td><code>daily-briefing</code></td><td>金融日报</td><td>（可选）日期</td><td>★NEW★ 每日A股盘后综述：指数/板块/资金/要闻</td></tr>
</table>

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
    """使用手册"""
    # F5: the skill count in the static HTML goes stale as skills are added —
    # substitute the live count at serve time.
    html = MANUAL_HTML.replace("技能速查（19个技能）", f"技能速查（{len(list_skills())}个技能）")
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
