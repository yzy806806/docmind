# DocMind

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](https://www.docker.com/)
[![Tests](https://img.shields.io/badge/tests-pytest-0a9edc.svg)](https://docs.pytest.org/)

**AI 驱动的企业级文档知识库** — 把文档、标书、报表、数据库全扔进来，AI 帮你归类整理、提炼摘要、写索引目录。需要的时候用自然语言搜索，AI 替你读文档、找依据、写报告。

## 核心定位

- **主力：** Web 端（上传、管理、搜索、对话）
- **副线：** Hermes Tool 接入（聊天中检索知识库）
- **面向：** 企业文档管理（标书、合同、财报、ERP 数据）和行政办公（发言稿、工作报告、地方规划）

## 架构概览

```
WebDAV / 本地目录 / 数据库
          │
    文件发现 & 文本提取
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
    摘要生成 (LLM)
          │
   ┌──────┼──────┐
   ▼      ▼      ▼
 Web UI  Hermes   CLI
(搜索/问答) Tool  (命令行)
```

## 功能

### 数据接入与处理

- [x] **多源接入：** WebDAV（群晖等 NAS）、本地目录、PostgreSQL 数据库
- [x] **全格式提取：** PDF、DOCX、HTML、Markdown、TXT、图片元信息
- [x] **增量处理：** 文件 hash 检测变更，只处理新文件
- [x] **文档分块 (Document Chunking)：** 按语义切分文档，提升搜索粒度与 RAG 检索精度，减少 LLM token 消耗

### 搜索与检索

- [x] **FTS5 全文索引：** 轻量高效，支持 SQLite 内建全文搜索
- [x] **向量语义搜索：** 基于 sentence-transformers 嵌入，支持本地 / Ollama / OpenAI 多种 embedding 后端
- [x] **混合搜索 (Hybrid Search)：** FTS5 关键词 + 向量语义双路融合排序，可调节权重，无嵌入时自动回退到纯 FTS5
- [x] **LLM 多轮筛选：** 关键词初筛 → LLM 摘要匹配 → 原文返回
- [x] **引用溯源：** 每段回答标注来源文档 + 位置

### Web 管理界面

- [x] **Web 管理界面：** 上传、搜索、对话、索引导航
- [x] **分析仪表盘 (Analytics Dashboard)：** 使用统计与可视化图表，支持日期范围筛选、文档增长趋势、搜索热度、标签分布
- [x] **文档查看器 (Document Viewer)：** 格式化内容渲染、分页浏览、目录侧边栏、文档内搜索、阅读模式
- [x] **Jinja2 模板系统：** 18 个模板，HTML/CSS/JS 与业务逻辑分离，server.py 从 3542 行精简至 1408 行
- [x] **文档标签 (Document Tags)：** 标签云、按标签筛选、文档分类管理
- [x] **聊天历史 (Chat History)：** 持久化多轮对话记录，会话回放，历史查询
- [x] **设置页面：** Web 端配置 LLM 参数、数据源管理

### 导出与摘要

- [x] **答案导出 (Export)：** 聊天记录导出（Markdown / JSON / TXT）、搜索结果导出（CSV / JSON）、文档摘要导出（Markdown / TXT）
- [x] **自动摘要 (Auto-Summarization)：** LLM 驱动的 map-reduce 分块摘要，长文档自动切分、逐段摘要再合并，带重试与抽取式回退

### 安全与集成

- [x] **API Key 认证：** 可选的会话 + API Key 双模式认证，HMAC-SHA256 签名 Cookie，支持 `X-API-Key` 头部
- [x] **Hermes Tool 接入：** `kb_search`、`kb_list`、`kb_read`、`kb_ingest`
- [x] **TPM 限速：** 控制 LLM 调用频率，不炸 API

## 项目结构

```
docmind/
├── src/
│   ├── core/              # 核心引擎
│   │   ├── storage.py         # WebDAV / 目录 / DB 接入
│   │   ├── extractor.py       # 文本提取（PDF/DOCX/HTML...）
│   │   ├── indexer.py         # SQLite + FTS5 管理
│   │   ├── chunking.py        # 文档分块
│   │   ├── embeddings.py      # 向量嵌入（本地/Ollama/OpenAI）
│   │   ├── search.py          # 多轮搜索引擎 + 混合搜索
│   │   ├── search_backend.py  # 搜索后端抽象
│   │   ├── summarizer.py      # LLM 摘要管道（map-reduce）
│   │   ├── llm_client.py      # OpenAI 兼容 LLM 客户端
│   │   ├── job_queue.py       # 异步任务队列
│   │   └── config.py          # 配置管理
│   ├── web/               # Web 前端 (FastAPI + Jinja2)
│   │   ├── server.py          # FastAPI 应用与路由
│   │   ├── auth.py            # API Key 认证
│   │   ├── chat.py            # WebSocket 聊天
│   │   ├── document_viewer.py # 文档查看器
│   │   ├── rendering.py       # Jinja2 模板渲染
│   │   ├── services.py        # 业务服务（导出、摘要）
│   │   └── templates/         # 18 个 Jinja2 模板
│   ├── cli/               # 命令行工具
│   │   └── main.py
│   └── hermes_plugin.py   # Hermes Tool 注册
├── tests/                 # 测试套件（pytest）
├── config/
│   └── config.example.yaml
├── data/                  # SQLite 数据库存储
├── Dockerfile             # 多阶段 Docker 构建
├── docker-compose.yml     # 一键容器编排
└── docs/
```

## 快速开始

### 方式一：Docker（推荐）

```bash
# 克隆仓库
git clone https://github.com/yzy806806/docmind.git
cd docmind

# 复制环境变量模板并填入 LLM API Key
cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml：填入 LLM API key、数据源

# 创建 .env 文件（可选，用于覆盖 docker-compose 中的环境变量）
cat > .env <<'EOF'
DOCMIND_LLM_API_KEY=sk-your-key-here
DOCMIND_LLM_MODEL=gpt-4o-mini
DOCMIND_AUTH_ENABLED=false
EOF

# 启动服务
docker compose up -d --build

# 查看日志
docker compose logs -f docmind
```

访问 `http://localhost:8000` 进入管理界面。

容器默认暴露 **8000** 端口（可通过 `DOCMIND_PORT` 环境变量修改）。数据持久化通过 volume 挂载 `./data` 和 `./config` 目录。

### 方式二：本地开发

```bash
# 安装
git clone https://github.com/yzy806806/docmind.git
cd docmind
uv sync

# 可选：安装向量搜索依赖
uv sync --extra embeddings

# 配置
cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml：填入 WebDAV 地址、LLM API key

# 启动 Web 服务
uv run python -m src.web.server

# 或 Hermes tool 模式
hermes plugins install src/hermes_plugin.py
```

访问 `http://localhost:8080` 进入管理界面（本地开发默认端口）。

## 技术栈

- **后端：** Python 3.11+, FastAPI, SQLite + FTS5, Jinja2
- **前端：** Jinja2 + htmx（轻量），后续可选 Vue/React
- **文档提取：** pdfplumber, python-docx, beautifulsoup4
- **向量搜索：** sentence-transformers, numpy（可选依赖）
- **LLM：** OpenAI 兼容 API（支持任何 OpenAI 格式接口）
- **认证：** HMAC-SHA256 签名 Cookie + API Key
- **容器化：** Docker（多阶段构建）, docker-compose
- **打包：** uv / pip

## 开发

```bash
# 安装开发依赖
uv sync --extra dev --extra embeddings

# 运行测试
uv run pytest

# 运行测试并生成覆盖率报告
uv run pytest --cov=src --cov-report=html
```

## License

MIT
