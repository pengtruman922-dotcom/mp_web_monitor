# 情报助手 (Policy Intelligence Monitor)

AI 驱动的政策情报自动监控系统。自动爬取网站的政策更新、新闻和文件，利用大语言模型生成摘要与分析报告，并支持邮件/企业微信定时推送。

## 功能特性

- **智能爬虫**：基于 Playwright + LLM 的 Agent 管线，自动识别栏目、筛选内容、提取日期
- **多源并行**：支持同时监控多个网站，5 源并行采集
- **AI 摘要**：LLM 自动生成文章摘要、标签分类、重要性排序
- **综合报告**：按重要性排序生成每日/每周政策情报报告
- **推送通知**：支持邮件和企业微信 Webhook 推送，可定时或采集后自动推送
- **多用户隔离**：支持管理员和普通用户角色，数据按账号隔离
- **管理后台**：Web 界面管理监控源、查看报告、配置推送规则

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 数据库 | SQLAlchemy 2.0 (async) + SQLite |
| LLM | qwen3-max via DashScope API (OpenAI 兼容接口) |
| 浏览器自动化 | Playwright (Chromium) |
| 前端 | Jinja2 模板 + TailwindCSS CDN |
| 任务调度 | APScheduler |
| 邮件 | aiosmtplib |
| 认证 | Cookie Session (itsdangerous) |

## 快速开始

### 环境要求

- Python 3.10+
- Windows / Linux / macOS

### 安装

```bash
# 克隆仓库
git clone https://github.com/pengtruman922-dotcom/mp_web_monitor.git
cd mp_web_monitor

# 创建虚拟环境（推荐）
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
python -m playwright install chromium
```

### 配置

复制环境变量模板并填写：

```bash
cp .env.example .env
```

编辑 `.env` 文件，配置以下关键项：

```env
# LLM 配置（必填 - 推荐使用 DashScope qwen3-max）
LLM_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
LLM_API_KEY=sk-your-api-key
LLM_MODEL_NAME=qwen3-max

# 邮件推送配置（可选 - 需要时填写）
SMTP_HOST=smtp.example.com
SMTP_PORT=465
SMTP_USE_TLS=true
SMTP_USERNAME=your-email@example.com
SMTP_PASSWORD=your-password
SENDER_EMAIL=your-email@example.com

# 应用配置
APP_PORT=8090
SECRET_KEY=your-random-secret-key
```

> LLM 也可以在启动后通过 Web 界面"设置"页配置。

### 启动

```bash
# 方式一：命令行启动
python -m app.main

# 方式二：Windows 双击启动
start.bat
```

启动后访问 `http://localhost:8090`。

### 默认账号

首次启动会自动创建管理员账号：

- 用户名：`admin`
- 密码：`admin123`

> 首次登录后会提示修改密码。

## 项目结构

```
app/
├── main.py                  # FastAPI 应用入口，lifespan 管理
├── config.py                # 环境变量与常量配置
├── auth.py                  # 认证模块（密码哈希、Session、权限）
├── agent/                   # AI Agent 采集管线
│   ├── orchestrator.py      # 4 阶段并行管线调度器
│   ├── runtime.py           # Agent 运行时（LLM 循环 + 工具执行）
│   ├── prompts.py           # LLM Prompt 模板
│   ├── domain_filter.py     # 跨域 URL 过滤
│   └── tools/               # Agent 工具集
│       ├── browser.py       # Playwright 页面访问 + 日期提取
│       ├── document.py      # PDF/DOCX/XLSX 解析
│       └── downloader.py    # 文件下载
├── api/                     # RESTful API 路由
│   ├── auth.py              # 登录/登出/改密
│   ├── users.py             # 用户管理（管理员）
│   ├── sources.py           # 监控源 CRUD
│   ├── tasks.py             # 采集任务触发与管理
│   ├── results.py           # 采集结果查询
│   ├── reports.py           # 报告管理
│   ├── push_rules.py        # 推送规则 CRUD
│   └── settings.py          # 系统设置（LLM/SMTP）
├── models/                  # SQLAlchemy ORM 模型
├── database/                # 数据库连接与迁移
├── llm/                     # LLM 客户端与工具定义
├── notification/            # 推送引擎（邮件/企微）
├── scheduler/               # APScheduler 定时任务
└── web/
    ├── routes.py            # 页面路由
    ├── static/              # 静态资源
    └── templates/           # Jinja2 页面模板（10 个）
```

## 采集管线

系统采用 4 阶段 AI Agent 管线，每个监控源独立并行执行：

```
Phase 1a: 首页探索 → 识别栏目链接
Phase 1b: 栏目深入 → 收集文章列表（最多 5 个栏目）
Phase 2:  AI 摘要 → 生成摘要 + 标签 + 内容分类
Phase 3:  排序报告 → 按重要性排序，生成综合报告
```

关键参数：
- 最多 5 个监控源并行采集
- 每源最多 5 个栏目
- 每 Agent 最多 50 轮 LLM 调用
- 页面访问间隔 2 秒（反爬）
- 支持 7 种日期格式提取
- 智能去重（标题 + URL）

## 用户角色

| 功能 | 管理员 | 普通用户 |
|------|--------|----------|
| 查看自己的数据 | ✅ | ✅ |
| 添加监控源 | ✅ | ✅ |
| 触发采集 | ✅ | ✅ |
| 查看报告 | ✅ | ✅ |
| 配置推送规则 | ✅ | ✅ |
| 查看其他用户数据 | ✅ | ❌ |
| 查看全部数据 | ✅ | ❌ |
| 账号管理 | ✅ | ❌ |
| 系统设置 | ✅ | ❌ |

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 当前用户信息 |
| PUT | `/api/auth/change-password` | 修改密码 |
| GET/POST | `/api/sources` | 监控源列表/创建 |
| GET/PUT/DELETE | `/api/sources/{id}` | 监控源详情/编辑/删除 |
| POST | `/api/tasks/trigger` | 触发采集任务 |
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/results` | 采集结果 |
| GET | `/api/reports` | 报告列表 |
| GET | `/api/reports/{id}` | 报告详情 |
| GET/POST | `/api/push-rules` | 推送规则列表/创建 |
| POST | `/api/push-rules/{id}/push` | 立即推送 |
| GET/PUT | `/api/settings/llm` | LLM 配置 |
| GET/PUT | `/api/settings/smtp` | 邮件配置 |
| GET/POST | `/api/users` | 用户列表/创建（管理员） |

## 许可证

MIT License
