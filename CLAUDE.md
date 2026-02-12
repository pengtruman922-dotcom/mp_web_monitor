# 政策情报助手 (Web Monitor)

## 项目概述

自动化 AI 政策情报监控系统，爬取中国政府网站的政策更新、新闻和文件，生成摘要报告并邮件推送。

## 技术栈

- **后端**: FastAPI 0.115 + Uvicorn，Python 3.10+
- **数据库**: SQLAlchemy 2.0 (async) + SQLite (aiosqlite)
- **LLM**: qwen3-max via DashScope API (OpenAI-compatible)
- **浏览器**: Playwright (Chromium) 自动化爬虫
- **前端**: Jinja2 服务端渲染 + 原生 CSS/JS
- **邮件**: aiosmtplib
- **调度**: APScheduler
- **平台**: Windows 11, 端口 8090 (APP_PORT in .env)

## 目录结构

```
app/
├── main.py                  # FastAPI 入口，lifespan 管理启动/关闭
├── config.py                # 环境变量 + 运行时常量
├── agent/                   # AI Agent 管线（核心）
│   ├── orchestrator.py      # 并行 agent 执行器，4 阶段管线
│   ├── runtime.py           # Agent 运行时（LLM 循环 + 工具执行）
│   ├── prompts.py           # LLM Prompt 模板
│   ├── domain_filter.py     # 跨域 URL 过滤
│   └── tools/
│       ├── browser.py       # Playwright 页面访问 + 日期提取
│       ├── document.py      # PDF/DOCX/XLSX 解析
│       └── downloader.py    # 文件下载
├── api/                     # RESTful API 路由
│   ├── sources.py           # 监控源 CRUD
│   ├── tasks.py             # 任务触发 (POST /api/tasks/trigger)
│   ├── reports.py           # 报告管理
│   ├── results.py           # 爬取结果
│   ├── push_rules.py        # 推送规则
│   └── settings.py          # 系统设置（LLM/SMTP）
├── models/                  # SQLAlchemy ORM 模型
│   ├── source.py            # MonitorSource
│   ├── task.py              # CrawlTask + TaskStatus/TriggerType
│   ├── result.py            # CrawlResult
│   ├── report.py            # Report
│   ├── push_rule.py         # PushRule
│   └── settings.py          # LLMConfig
├── database/
│   ├── connection.py        # async engine + session factory
│   └── migrations.py        # 初始化迁移 + seed data
├── llm/
│   ├── client.py            # OpenAI-compatible 异步客户端
│   └── schemas.py           # 工具定义 (CRAWLER_TOOLS)
├── notification/
│   ├── engine.py            # 推送引擎
│   ├── email_sender.py      # SMTP 发送
│   └── templates/           # 邮件 HTML 模板
├── scheduler/
│   └── scheduler.py         # APScheduler 定时任务
└── web/
    ├── routes.py            # 页面路由
    ├── static/              # CSS/JS 静态资源
    └── templates/           # Jinja2 模板 (8 个页面)
data/                        # 运行时数据
├── db.sqlite                # SQLite 数据库
└── downloads/               # 下载的附件
```

## 核心管线 (orchestrator.py)

4 阶段管线，每个源独立并行执行：
1. **Phase 1a** - 首页爬取：访问源首页，提取栏目链接
2. **Phase 1b** - 栏目爬取：逐栏目深入，收集文章列表
3. **Phase 2** - 摘要生成：LLM 对每篇文章生成摘要 + 标签 + content_type
4. **Phase 3** - 排序 + 报告：LLM 按重要性排序，生成总览报告

关键参数：
- `AGENT_MAX_CONCURRENCY = 5` (最多 5 个源并行)
- `LLM_MAX_CONCURRENCY = 3` (最多 3 个 LLM 并发请求)
- `MAX_SECTIONS = 5` (每源最多 5 个栏目)
- `AGENT_MAX_TURNS = 50` (每 agent 最多 50 轮 LLM 调用)
- `AGENT_PAGE_DELAY = 2.0` (页面访问间隔秒数)

## 监控源 (8 个)

| ID | 名称 | 特殊配置 |
|----|------|----------|
| 1  | 国家能源局 | 标准 |
| 2  | 国资委 | 标准 |
| 3  | 共产党员网 | 标准 |
| 4  | 求是网 | 标准 |
| 5  | 经济日报 | 标准 |
| 6  | 人民日报 | 特殊 URL 模式（报纸版） |
| 7  | 党建研究 | 标准 |
| 11 | 中大继教 | 允许跨域, time_range=90d |

## 开发规范

### 启动服务
```bash
python -m app.main
# 或使用 start.bat (Windows)
```

### 数据库
- 使用 `async_session()` 上下文管理器操作数据库
- 模型变更后在 `migrations.py` 中添加迁移逻辑
- 不要直接操作 db.sqlite 文件

### API 规范
- 路由在 `app/api/` 下按资源组织
- 使用 Pydantic model 做请求/响应校验
- 异步处理所有 I/O 操作

### Agent 管线
- `orchestrator.py` 是最复杂的文件，修改时需特别谨慎
- 修改 prompt 在 `prompts.py` 中进行
- 新增工具在 `tools/` 下创建并在 `schemas.py` 注册
- 浏览器操作通过 `browse_page()` 函数，自带反爬延迟

### 前端
- Jinja2 模板在 `app/web/templates/`
- 静态资源在 `app/web/static/`
- 基础布局在 `base.html`，其他页面继承

### Windows 注意事项
- 使用 `sleep` 而不是 `timeout /t`
- 进程管理用 `taskkill /F /IM python.exe`
- Git 的 LF→CRLF 警告是正常的

## 测试

```bash
# 触发单源爬取测试
curl -X POST http://localhost:8090/api/tasks/trigger -H "Content-Type: application/json" -d "{\"source_ids\": [1]}"

# 查看任务状态
curl http://localhost:8090/api/tasks

# 查看最新报告
curl http://localhost:8090/api/reports
```

## 环境变量 (.env)

参考 `.env.example`，关键变量：
- `LLM_API_URL` / `LLM_API_KEY` / `LLM_MODEL_NAME` - LLM 配置
- `SMTP_*` - 邮件配置
- `APP_PORT` - 服务端口 (生产环境用 8090)
- `SECRET_KEY` - 应用密钥
