# DocMind

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.ruff.sh/)
[![Tests](https://img.shields.io/badge/tests-pytest-0a9edc.svg)](https://docs.pytest.org/)

**AI 驱动的企业级文档知识库** — 把文档、标书、报表、数据库、邮件全扔进来，AI 帮你归类整理、提炼摘要、写索引目录。需要的时候用自然语言搜索，AI 替你读文档、找依据、写报告。

## 核心定位

- **主力：** Web 端（上传、管理、搜索、对话）
- **副线：** Hermes Tool 接入（聊天中检索知识库）
- **面向：** 企业文档管理（标书、合同、财报、ERP 数据）和行政办公（发言稿、工作报告、地方规划）

## 架构概览

```
IMAP 邮箱 / WebDAV / 本地目录 / PostgreSQL 数据库
          │
    文件发现 & 文本提取 (含 OCR)
          │
    文档分块 (Chunking)
          │
    ┌─────┴─────┐
    ▼           ▼
  FTS5 全文   向量语义
   索引       嵌入索引
    │           │
    └─────┬─────┘
          │
    混合搜索 (Hybrid Search)
          │
    摘要生成 (LLM)    ← ─ ─ 查询结果缓存层
          │
   ┌──────┼──────┐
   ▼      ▼      ▼
 Web UI  Hermes   CLI
(搜索/问答) Tool  (命令行)
```

## 功能

### 数据接入与处理

- [x] **多源接入：** IMAP 邮箱（Gmail、Outlook、自建服务器）、WebDAV（群晖等 NAS）、本地目录、PostgreSQL 数据库、Web 拖拽上传
- [x] **邮件自动抓取：** 定时轮询 IMAP 收件箱，自动将邮件正文和附件转为可搜索文档，支持多账户、去重、附件白名单/黑名单过滤
- [x] **全格式提取：** PDF、DOCX、HTML、Markdown、TXT、图片元信息
- [x] **OCR 扫描件识别：** Tesseract OCR 自动识别扫描版 PDF 和图片中的文字
- [x] **文档类型自动检测：** LLM 驱动的文档分类（合同、报告、财报等），无 LLM 时自动回退到关键词启发式
- [x] **增量处理：** 文件 hash (SHA256) 检测变更，`upsert_document` + `ON CONFLICT` 只处理新文件
- [x] **文档分块 (Document Chunking)：** 按语义切分文档，提升搜索粒度与 RAG 检索精度，减少 LLM token 消耗
- [x] **多文件拖拽上传 (Multi-File Drag-and-Drop Upload)：** 拖拽式批量上传界面

### 搜索与检索

- [x] **FTS5 全文搜索：** 轻量高效，支持 SQLite 内建全文搜索
- [x] **向量语义搜索 (Vector/Semantic Search)：** 基于 sentence-transformers 嵌入，支持本地 / Ollama / OpenAI 多种 embedding 后端
- [x] **混合搜索 (Hybrid Search)：** FTS5 关键词 + 向量语义双路融合排序，可调节权重，无嵌入时自动回退到纯 FTS5
- [x] **搜索权重调节 (Search Relevance Tuning)：** 滑块控制 FTS5 与向量语义的权重比例（`vector_weight`），实时调节搜索结果偏向
- [x] **分类筛选 (Faceted Search)：** 按文件类型和数据来源侧边栏筛选，实时统计各分类下的文档数量
- [x] **LLM 多轮筛选：** FTS5 + 向量混合搜索初筛 → LLM 摘要匹配 → 原文返回
- [x] **引用溯源 (Citation/Source Tracking)：** 每段回答标注来源文档 + 位置

### Web 管理界面

- [x] **Web 管理界面：** 上传、搜索、对话、文档管理、分析仪表盘、设置、任务状态
- [x] **分析仪表盘 (Analytics Dashboard)：** 使用统计与可视化图表，支持日期范围筛选、文档增长趋势、搜索热度、标签分布
- [x] **文档查看器 (Document Viewer)：** 格式化内容渲染、分页浏览 (Pagination)、目录侧边栏、文档内搜索、阅读模式
- [x] **文档标签与元数据 (Document Tags & Metadata)：** 标签云、按标签筛选、文档分类管理
- [x] **文档集合 (Document Collections)：** 层级树形结构，面包屑导航，支持增删改查和文档批量移入/移出
- [x] **邮件账户管理：** Web UI 管理 IMAP 账户，支持连接测试、手动触发同步、查看抓取日志
- [x] **聊天历史 (Chat History)：** 持久化多轮对话记录，会话管理 (Session Management)，会话回放，历史查询
- [x] **任务状态页 (Job Processing Status Page)：** 异步任务队列可视化，处理进度跟踪
- [x] **深色模式 (Dark Mode Toggle)：** 一键切换明暗主题，自动记忆用户偏好，全页面适配
- [x] **批量操作 (Bulk Operations)：** 多选复选框，支持批量删除、批量标签、批量移动、批量导出
- [x] **响应式设计 (Responsive Design)：** 5 个 CSS 断点 (1024/768/640/480px)，适配桌面到手机

### 导出与摘要

- [x] **答案导出 (Answer Export)：** 聊天记录导出（Markdown / JSON / TXT）、搜索结果导出（CSV / JSON）、文档摘要导出（Markdown / TXT）
- [x] **自动摘要 (Auto-Summarization)：** LLM 驱动的 map-reduce 分块摘要，长文档自动切分、逐段摘要再合并，带重试与抽取式回退

### 安全与集成

- [x] **API Key 认证 (API Key Authentication)：** 可选的会话 + API Key 双模式认证，HMAC-SHA256 签名 Cookie，支持 `X-API-Key` 头部
- [x] **Hermes Tool 接入：** `kb_search`、`kb_list`、`kb_read`、`kb_ingest` 四个工具，可在 Hermes 聊天中直接检索知识库。搜索后端已整合 HybridSearchEngine，支持 FTS5 + 向量混合搜索与评分融合
- [x] **速率限制 (Rate Limiting)：** LLM 调用频率控制（TPM，默认 5 TPM）+ 基于 IP 的滑动窗口 API 限流（可配置每分钟请求上限，超限返回 429 + `Retry-After` 头部）
- [x] **凭证加密 (Credential Encryption)：** IMAP 邮箱密码使用 Fernet 对称加密存储，密钥通过环境变量注入

### 性能与部署

- [x] **多 LLM 支持 (Multi-LLM Support)：** OpenAI 兼容 API + Ollama 双后端，支持 OpenAI / vLLM / LM Studio 等任意 OpenAI 格式接口
- [x] **Docker 支持：** 多阶段 Dockerfile + docker-compose 编排
- [x] **查询结果缓存 (Query Result Caching)：** 读方法自动缓存，写路径精准失效，支持内存（默认）和 Redis 两种后端，可一键关闭

## 对比同类项目

| 功能 | DocMind | Paperless-ngx | Teedy | Docspell | Mayan EDMS |
|------|---------|:---:|:---:|:---:|:---:|
| 多源接入（邮件/WebDAV/本地/数据库） | ✅ | ✅ | ✅ | ✅ | ✅ |
| OCR 扫描件 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 混合搜索（全文+向量） | ✅ | ❌ | ❌ | ❌ | ❌ |
| 搜索权重调节 | ✅ | ❌ | ❌ | ❌ | ❌ |
| LLM 问答 + 引用溯源 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 自动摘要 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 邮件抓取 | ✅ | ✅ | ⚠️ | ✅ | ⚠️ |
| 文档类型自动分类 | ✅ | ⚠️ | ❌ | ❌ | ⚠️ |
| 层级集合 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 批量操作 | ✅ | ✅ | ✅ | ✅ | ✅ |
| API 速率限制 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 查询缓存 | ✅ | ✅ | ⚠️ | ✅ | ✅ |
| 深色模式 | ✅ | ✅ | ⚠️ | ❌ | ❌ |
| 响应式设计 | ✅ | ✅ | ✅ | ⚠️ | ❌ |
| 工作流自动化 | ❌ | ✅ | ⚠️ | ❌ | ✅ |
| 零外部依赖（开箱即用） | ✅ | ❌ | ❌ | ❌ | ❌ |

> DocMind 的核心优势：零外部依赖（SQLite 单文件数据库，无需 PostgreSQL/Redis/Elasticsearch），AI 原生（混合搜索 + LLM 问答），轻量部署。

## 项目结构

```
docmind/
├── src/
│   ├── core/                  # 核心引擎
│   │   ├── storage.py             # WebDAV / 目录 / PostgreSQL 接入
│   │   ├── extractor.py           # 文本提取（PDF/DOCX/HTML...）
│   │   ├── indexer.py             # SQLite + FTS5 管理（hash 检测 + upsert）
│   │   ├── chunking.py            # 文档分块
│   │   ├── embeddings.py          # 向量嵌入（本地/Ollama/OpenAI）
│   │   ├── search.py              # 多轮搜索引擎 + 混合搜索
│   │   ├── search_backend.py      # 搜索后端抽象
│   │   ├── summarizer.py          # LLM 摘要管道（map-reduce + TPM 限速）
│   │   ├── llm_client.py          # OpenAI + Ollama LLM 客户端
│   │   ├── job_queue.py           # 异步任务队列
│   │   ├── email_ingestor.py      # IMAP 邮件抓取（轮询、解析、去重、附件提取）
│   │   ├── crypto.py              # 凭证加密（Fernet 对称加密）
│   │   ├── cache.py               # 查询结果缓存（内存/Redis，cache-aside）
│   │   ├── db.py / db_sqlite.py   # 数据库适配层
│   │   ├── config.py              # 配置管理
│   │   ├── models.py              # 数据模型
│   │   ├── parser_sandbox.py      # 解析器沙箱
│   │   └── sanitizer.py           # 数据清洗
│   ├── web/                   # Web 前端 (FastAPI + Jinja2)
│   │   ├── server.py              # FastAPI 应用与路由
│   │   ├── auth.py                # API Key 认证（HMAC-SHA256）
│   │   ├── chat.py                # WebSocket 聊天
│   │   ├── document_viewer.py     # 文档查看器
│   │   ├── rate_limit.py          # API 速率限制中间件
│   │   ├── rendering.py           # Jinja2 模板渲染
│   │   ├── services.py            # 业务服务（导出、摘要）
│   │   └── templates/             # Jinja2 模板
│   │       ├── _partials/             # 分页等可复用组件
│   │       └── documents/             # 文档列表与详情页
│   ├── cli/                   # 命令行工具
│   │   ├── main.py
│   │   ├── services.py
│   │   └── formatters.py
│   └── hermes_plugin.py       # Hermes Tool 注册（kb_search/list/read/ingest）
├── tests/                     # 测试套件（pytest，2138+ 测试）
├── config/
│   └── config.example.yaml
├── data/                      # SQLite 数据库存储
├── docs/
│   ├── gap-analysis.md            # 竞品对比与差距分析
│   ├── architecture/
│   │   ├── caching.md             # 缓存层架构设计
│   │   ├── rate-limiting.md       # API 速率限制配置指南
│   │   └── email-ingestion.md     # 邮件抓取配置与安全指南
│   └── openapi.yaml           # OpenAPI 规范
├── pyproject.toml             # 依赖与项目元数据
├── ARCHITECTURE.md            # 架构决策记录 (ADR)
├── CHANGELOG.md               # 版本变更日志
└── AGENTS.md
```

## 快速开始

```bash
# 安装
git clone https://github.com/yzy806806/docmind.git
cd docmind
uv sync

# 配置
cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml：填入 LLM API key、数据源

# 启动 Web 服务
uv run python -m src.web.server

# 或 Hermes tool 模式
hermes plugins install src/hermes_plugin.py
```

访问 `http://localhost:8080` 进入管理界面。

### Docker 一键启动

```bash
docker-compose up -d
```

访问 `http://localhost:8000` 进入管理界面。

> **提示：** Docker 模式默认使用端口 8000，本地原生启动默认使用端口 8080。

### 邮件抓取配置

DocMind 可定时轮询 IMAP 邮箱（Gmail、Outlook、自建服务器），自动将邮件和附件转为可搜索文档：

```bash
# 开启邮件抓取
export DOCMIND_EMAIL_ENABLED=true

# 配置邮箱（Gmail 需使用应用专用密码）
export DOCMIND_EMAIL_ACCOUNT_0_NAME="Work Gmail"
export DOCMIND_EMAIL_ACCOUNT_0_HOST="imap.gmail.com"
export DOCMIND_EMAIL_ACCOUNT_0_PORT="993"
export DOCMIND_EMAIL_ACCOUNT_0_USERNAME="you@gmail.com"
export DOCMIND_EMAIL_ACCOUNT_0_PASSWORD="abcd efgh ijkl mnop"

# 可调整轮询间隔（默认 600 秒 = 10 分钟）
export DOCMIND_EMAIL_POLL_INTERVAL="300"

# 设置加密密钥保护邮箱密码
export DOCMIND_EMAIL_ENCRYPTION_KEY="your-generated-key"
```

> **安全提示：** 请使用应用专用密码而非主密码。Gmail 用户需开启两步验证后在 https://myaccount.google.com/apppasswords 生成。详见 `docs/architecture/email-ingestion.md`。

### 缓存配置

DocMind 默认启用内存缓存，无需额外配置即可获得查询加速。对于多进程部署或需要跨实例共享缓存的场景，可切换至 Redis：

```bash
# 使用 Redis 缓存（需要 pip install redis）
export DOCMIND_CACHE_BACKEND=redis
export DOCMIND_CACHE_REDIS_URL=redis://localhost:6379/0

# 关闭缓存（所有查询直接走数据库）
export DOCMIND_CACHE_ENABLED=false
```

> **自托管说明：** 内存缓存（默认）是进程内缓存，每个 worker 进程独立维护。若使用 `DOCMIND_WORKERS > 1` 的多进程模式，各进程的缓存不互通 —— 写入操作会通过 SQLite 的 WAL 模式确保数据一致性，但缓存命中率会因进程隔离而降低。如需跨进程共享缓存，请启用 Redis 后端。

### 速率限制配置

DocMind 内置基于 IP 的滑动窗口速率限制器，无需外部依赖：

```bash
# 开启速率限制（默认关闭，适合自托管单用户场景）
export DOCMIND_RATE_LIMIT_ENABLED=true

# 每 IP 每分钟最大请求数（默认 60）
export DOCMIND_RATE_LIMIT_REQUESTS_PER_MINUTE=120
```

超限时返回 `HTTP 429 Too Many Requests`，响应体包含 `Retry-After` 头部和 JSON 错误详情。健康检查、API 文档和静态资源路径不受限制。详见 `docs/architecture/rate-limiting.md`。

> **反向代理注意事项：** 若 DocMind 位于 nginx/Caddy/Traefik 等反向代理之后，请确保代理转发客户端真实 IP（`X-Forwarded-For` 头部），否则所有请求将共享同一个速率限制桶。

## 技术栈

- **后端：** Python 3.11+, FastAPI, SQLite + FTS5
- **前端：** Jinja2 模板 + htmx（轻量）
- **文档提取：** pdfplumber, python-docx, beautifulsoup4, pytesseract (OCR)
- **向量嵌入：** sentence-transformers（本地），兼容 Ollama / OpenAI
- **LLM：** OpenAI 兼容 API + Ollama（支持任意 OpenAI 格式接口）
- **打包：** uv / pip

## 同类项目对比

DocMind 在以下方面区别于 Paperless-ngx、Teedy、Docspell 和 Mayan EDMS：

- **零外部依赖：** 使用 SQLite 单文件数据库，无需部署 PostgreSQL、Redis、Elasticsearch 等外部服务。Docker 一键启动，本地 `uv run` 即可运行。
- **AI 原生搜索：** 混合搜索（关键词 + 向量语义）融合排序，支持滑块实时调节权重。LLM 驱动的多轮筛选与问答，答案附带原文出处引用。
- **轻量全栈：** Jinja2 SSR + 零依赖 JavaScript（无 npm 构建步骤），所有功能在一个 Python 进程中完成。

## License

MIT
