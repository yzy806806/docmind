# DocMind

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
    SQLite + FTS5 全文索引
          │
    摘要生成 (LLM)
          │
   ┌─────────┼─────────┐
   ▼         ▼         ▼
Web UI    Hermes Tool   CLI
(搜索/问答)  (聊天检索)   (命令行)
```

## 功能

- [ ] **多源接入：** WebDAV（群晖等 NAS）、本地目录、PostgreSQL 数据库
- [ ] **全格式提取：** PDF、DOCX、HTML、Markdown、TXT、图片元信息
- [ ] **FTS5 全文索引：** 轻量高效，支持 SQLite 内建全文搜索
- [ ] **LLM 多轮筛选：** 关键词初筛 → LLM 摘要匹配 → 原文返回
- [ ] **引用溯源：** 每段回答标注来源文档 + 位置
- [ ] **Web 管理界面：** 上传、搜索、对话、索引导航
- [ ] **Hermes Tool 接入：** `kb_search`、`kb_list`、`kb_read`、`kb_ingest`
- [ ] **增量处理：** 文件 hash 检测变更，只处理新文件
- [ ] **TPM 限速：** 控制 LLM 调用频率，不炸 API

## 项目结构

```
docmind/
├── src/
│   ├── core/          # 核心引擎
│   │   ├── storage.py     # WebDAV / 目录 / DB 接入
│   │   ├── extractor.py   # 文本提取（PDF/DOCX/HTML...）
│   │   ├── indexer.py     # SQLite + FTS5 管理
│   │   ├── summarizer.py  # LLM 摘要管道
│   │   └── search.py      # 多轮搜索引擎
│   ├── web/           # Web 前端 (FastAPI + Jinja2 / Vue)
│   │   ├── server.py
│   │   └── templates/
│   ├── cli/           # 命令行工具
│   │   └── main.py
│   └── hermes_plugin.py  # Hermes Tool 注册
├── tests/
├── config/
│   └── config.example.yaml
├── data/              # SQLite 数据库存储
└── docs/
```

## 开始使用

```bash
# 安装
git clone https://github.com/yzy806806/docmind.git
cd docmind
uv sync

# 配置
cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml：填入 WebDAV 地址、LLM API key

# 启动 Web 服务
uv run python -m src.web.server

# 或 Hermes tool 模式
hermes plugins install src/hermes_plugin.py
```

访问 `http://localhost:8080` 进入管理界面。

## 技术栈

- **后端：** Python 3.11+, FastAPI, SQLite + FTS5
- **前端：** Jinja2 / htmx（轻量），后续可选 Vue/React
- **文档提取：** pdfplumber, python-docx, beautifulsoup4
- **LLM：** OpenAI 兼容 API（支持任何 OpenAI 格式接口）
- **打包：** uv / pip

## License

MIT
