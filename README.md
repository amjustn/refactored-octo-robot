# AI Berkshire Web

一个人可以依靠 AI 进行价值投资的分析——它支持国内外多种大模型的调用、多 Agent 高效协作、财务和管理层深度分析，并且重构了 harness 架构，保障了报告输出质量和降本增效。

一个人 + AI = 一个投研团队。

AI Berkshire 是一个基于大模型的自主投资研究 Web 应用：输入一家公司、一个行业或你的持仓组合，它会像一支投研团队一样分工协作——搜索数据、交叉验证、撰写深度报告。

## 核心特性

- **多智能体投研流水线**：投资团队（策略师/分析师/风险官）、新闻脉搏、财报解读等多个角色分工，自动规划 → 研究 → 交叉验证 → 成稿
- **15+ 投研技能**：基本面分析、公司深度研究、行业漏斗筛选、持仓评估、财报解读、管理层深研、估值审查、投资清单等
- **实时市场数据**：A 股/港股/美股行情、财务数据、公司新闻、行业研报（多数据源自动降级：akshare → 腾讯/新浪/东财 HTTP → 缓存）
- **宏观与行业研究**：中国宏观分析、全球宏观五维评估、产业图谱检索
- **文档理解**：PDF / Office 文档上传解析（MarkItDown）
- **浏览器报告界面**：WebSocket 实时流式输出、深色主题、报告导出

## 技术栈

- **后端**：FastAPI + Uvicorn（Python 3.10+）
- **LLM**：OpenAI 兼容接口（DeepSeek 等，通过 `LLM_BASE_URL` / `LLM_API_KEY` 配置）
- **数据**：akshare / yfinance / 腾讯 / 新浪 / 东方财富 HTTP 接口
- **前端**：原生 HTML/JS + ECharts

## 快速开始

```bash
# 1. 安装依赖
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env，填入你的 LLM_API_KEY（DeepSeek 或任意 OpenAI 兼容服务）
# 可选：LLM_BASE_URL 指定自定义 endpoint，LLM_MODEL 指定模型名

# 3. 启动
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001

# 4. 打开
http://localhost:8001
```

如需外网访问，在 `.env` 中设置：

```
BERKSHIRE_API_TOKEN=你的访问令牌   # 启用后所有 /api/* 接口需带 token
BERKSHIRE_BASIC_AUTH=用户名:密码   # 可选的基础认证
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_API_KEY` | - | LLM API 密钥（必填） |
| `LLM_BASE_URL` | DeepSeek 官方 | OpenAI 兼容 endpoint |
| `LLM_MODEL` | deepseek-chat | 模型名 |
| `BERKSHIRE_API_TOKEN` | 空 | 访问令牌，设置后保护所有 API |
| `BERKSHIRE_BASIC_AUTH` | 空 | 基础认证 用户名:密码 |
| `SKIN_DATA_DIR` | `~/.ai_berkshire_skin` | 换肤设置存储目录 |
| `MARKET_SYNC_WORKERS` | 6 | 行情同步线程池大小 |

## 测试

```bash
python -m pytest tests/ -v
```

## 目录结构

```
app/
  core/      配置、LLM 客户端、JWT、任务存储
  harness/   多智能体编排（规划、工具网关、上下文、持久化）
  tools/     数据与工具：行情、财务、新闻、研报、因子筛选、网页搜索
  skills/    15+ 投研技能定义（提示词 + 工具绑定）
  routers/   附加 API 路由（换肤等）
  models/    数据模型
static/      前端资源
templates/   HTML 模板
tests/       pytest 测试
```

## License

MIT
>>>>>>> 2fa2bb1 (feat: AI Berkshire Web - 自主投研多智能体系统)
