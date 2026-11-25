# 锡矿浮选配方专家系统 — 项目总结

## 项目概览

**项目名称:** 锡矿浮选药剂配方智能推荐系统（浮选助手）

**开发者:** Yiyu Qian

**核心目标:** 从学术文献中自动提取锡矿浮选实验数据，建立配方数据库，基于矿石相似度推荐最佳药剂配方，并导出 ML 训练数据集。

**技术栈:**

| 组件 | 技术 |
|------|------|
| LLM | DeepSeek API (`deepseek-chat`, OpenAI 兼容格式) |
| 后端 | Python 3.14, 标准库 HTTP 服务器 + 线程 |
| 前端 | 单文件 HTML (React CDN + marked.js + highlight.js) |
| 向量搜索 | FAISS + sentence-transformers (`all-MiniLM-L6-v2`) |
| PDF 解析 | PyMuPDF (`fitz`) |
| 通信 | REST API + SSE 流式进度推送 |
| 环境 | macOS, HuggingFace 镜像 (`hf-mirror.com`), SOCKS 代理环境 |

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                     浏览器 (index.html)                      │
│  React UI · Markdown 渲染 · SSE 进度条 · 矿石参数表单       │
└──────────────────────┬──────────────────────────────────────┘
                       │ POST /api/chat (SSE stream)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                    server.py (HTTP 服务器)                    │
│  ThreadingHTTPServer · SSE 进度推送 · 会话管理                │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                    agent.py (核心 Agent)                      │
│  DeepSeek API Function Calling · 按需加载 SKILL.md            │
│  Tool 路由 · 自动历史压缩 · 错误恢复                          │
└──────┬────────┬────────┬────────┬────────┬────────┬────────┘
       │        │        │        │        │        │
       ▼        ▼        ▼        ▼        ▼        ▼
  search_   fetch_    save_    grade_   build_   query_
  papers    paper     recipe   recipe   index    index
   .py       .py     (3合1)    .py      .py      .py
                    grade+syn
```

---

## 文件结构

```
~/tin-agent/                          ← 主项目目录
├── agent.py                          ← 核心 Agent (32KB, 840行)
│                                       DeepSeek API + 9个 Tool + 按需 SKILL 加载
├── server.py                         ← HTTP 服务器 (7KB)
│                                       SSE 流式进度 + 会话管理
├── index.html                        ← 前端界面 (23KB)
│                                       React + Markdown + 进度条
│
└── skills/                           ← 技能模块 (从 ~/.openclaw/workspace/skills/ 引用)
    │
    ├── tin-consultant/               ← Skill 1: 矿石咨询 + 论文搜索
    │   ├── SKILL.md                    行为规则文档 (8.6KB)
    │   └── scripts/
    │       └── search_papers.py        搜索 5 个学术源 (arXiv/Semantic Scholar/
    │                                   CrossRef/OpenAlex/CORE)
    │
    ├── recipe-extractor/             ← Skill 2: 配方提取
    │   ├── SKILL.md                    详细提取规则 (24KB, 最大的 SKILL)
    │   ├── scripts/
    │   │   └── fetch_paper.py          PDF 文本提取 + 智能截断
    │   └── references/
    │       └── recipe_template.json    v3 模板 (一实验一文件)
    │
    ├── recipe-grader/                ← Skill 3: 配方评分
    │   ├── SKILL.md                    评分规则 (7.4KB)
    │   └── scripts/
    │       └── grade_recipe.py         100分制 4维评分 + --merge 写回
    │
    ├── recipe-to-csv/                ← Skill 4: CSV 导出
    │   ├── SKILL.md                    100列 CSV 规范 (5.7KB)
    │   └── scripts/
    │       └── recipes_to_csv.py       ML 训练数据集生成
    │
    └── recipe-db/                    ← Skill 5: 向量数据库
        ├── SKILL.md                    索引/查询/近义词规则 (8.2KB)
        └── scripts/
            ├── build_index.py          FAISS 索引构建
            ├── query_index.py          矿石相似度推荐
            └── update_synonyms.py      近义词词典自动增长

~/.openclaw/workspace/database/       ← 数据存储
├── recipes/                          ← 配方 JSON (v3 格式, 一实验一文件)
│   ├── jin_2021_exp1.json
│   ├── jin_2021_exp2.json
│   └── ...
├── datasets/
│   └── recipe_recommendation_train.csv  ← ML 训练集 (100列)
├── synonyms.json                     ← 近义词词典 (自动增长)
└── faiss_index/
    ├── recipes.index                 ← FAISS 向量索引
    └── metadata.pkl                  ← 配方元数据
```

---

## 5 个 Skill 详解

### Skill 1: tin-consultant (矿石咨询 + 论文搜索)

**脚本:** `search_papers.py`

**功能:** 接收矿石关键词，并行搜索 5 个学术数据库，返回论文列表（标题、作者、年份、DOI、PDF 链接）。按引用数排序，DOI/标题去重。

**搜索源:** arXiv, Semantic Scholar, CrossRef, OpenAlex, CORE

### Skill 2: recipe-extractor (配方提取)

**脚本:** `fetch_paper.py`  
**模板:** `recipe_template.json` (v3)

**功能:** 从 PDF 提取全文和表格，供 DeepSeek 分析并提取浮选实验数据。

**fetch_paper.py 关键特性:**
- 默认 60,000 字符上限（≈15K tokens）
- 自动过滤无用 section（References、Acknowledgments 等）
- 智能截断：保留头 60%（摘要+方法）+ 尾 40%（结果+结论）+ 全部表格
- 移除了 `tables_structured`（和 tables_text 重复，之前双倍占 token）

**v3 模板格式（一实验一文件）:**
```json
{
  "paper": {"title": "...", "year": 2024, ...},
  "experiment_id": "zhang_2024_exp1",
  "ore_profile": { ... },
  "reagent_scheme": [ ... ],
  "results": { ... },
  "recommendation_signals": { ... }
}
```

**文件名规范:** `{first_author}_{year}_exp{N}.json`

### Skill 3: recipe-grader (配方评分)

**脚本:** `grade_recipe.py`

**评分体系:** 100 分制，4 个维度：

| 维度 | 满分 | 评估内容 |
|------|------|---------|
| 完整性 | 25 | 字段是否齐全 |
| 化学逻辑 | 30 | pH-捕收剂匹配、药剂拮抗、用量合理性 |
| 结果质量 | 25 | 回收率和品位 |
| 安全性 | 20 | 是否含铅/汞/铬 |

**评级:** A(75+) B(60-74) C(45-59) D(0-44)

**`--merge` 模式:** 将评分结果直接写回 JSON 文件的 `recommendation_signals` 字段。

**格式兼容:** 自动检测 v3 flat（一实验一文件）和 v2 nested（experiments[] 数组）格式。

### Skill 4: recipe-to-csv (ML 数据集导出)

**脚本:** `recipes_to_csv.py`

**输出:** `recipe_recommendation_train.csv`，100 列：

| 列组 | 列数 | 内容 |
|------|------|------|
| ID | 4 | case_id, paper_id, experiment_id, source_file |
| 矿石特征 | 29 | ore_type, minerals, assays, particle_size, flags |
| 药剂配方 | 30 | collector/depressant/activator/frother/pH modifier |
| 浮选条件 | 18 | cell_type, density, temperature, times |
| 结果 | 10 | recovery, grade, selectivity |
| 标签 | 5 | is_promising, is_best, recipe_score, rank |
| 质量 | 4 | extraction_confidence, value_source |

**格式兼容:** 自动检测 v3/v2 格式。

### Skill 5: recipe-db (向量数据库)

**脚本:** `build_index.py`, `query_index.py`, `update_synonyms.py`

**向量化策略:** 只对矿石特征（ore_profile）做 embedding，不含药剂信息。

**推荐排序:** `final_score = 0.7 × cosine_similarity + 0.3 × normalized_recipe_score`

**近义词词典 (`synonyms.json`):**
- 每次 `save_recipe` 后自动扫描 JSON，提取新术语
- 已知中文术语自动映射英文（内置 50+ 种子映射）
- 未知术语以 identity 形式加入，用户可后续编辑
- 幂等：重复运行不产生重复

---

## Agent 核心设计 (agent.py)

### 按需加载 SKILL.md

System prompt 只有 ~220 tokens（路由表），不预加载 54KB 的全部 SKILL 文档。

```
Tool 首次调用时：
  TOOL_TO_SKILL["fetch_paper_text"] → "recipe-extractor"
  → 读取 recipe-extractor/SKILL.md (24KB)
  → 注入到 tool result 前面（不创建独立消息）
  → 同一会话不重复加载
```

避免了 DeepSeek 的两个限制：
- 不允许多个 `system` 消息
- `tool_calls` 后面必须紧跟 `tool` 消息（不能插其他角色）

### save_recipe 三合一

每次保存自动执行：
1. 写入 JSON 文件
2. 运行 `grade_recipe.py --merge`（评分写回）
3. 运行 `update_synonyms.py`（更新近义词）

从 3 轮 API 调用缩减为 1 轮。

### 自动历史压缩

每轮开始前检查 history 大小：
- 超过 60KB → 自动压缩旧 tool 结果（只保留最近 2 条完整）
- API 400 错误 → 激进裁剪所有 tool 结果到 500 字符并重试

### 论文概述流程

提取配方前，DeepSeek 先输出论文概述：
```
📄 论文概述
- 标题: ...
- 研究矿石: oxide tin, quartz gangue
- 实验数量: 共识别到 10 个独立实验
⏳ 开始提取 10 个配方...
```

概述作为中间消息实时推送到前端，不等最终回复。

### 9 个 Tool

| Tool | 对应脚本 | 自动加载 SKILL |
|------|---------|---------------|
| `search_papers` | search_papers.py | tin-consultant |
| `fetch_paper_text` | fetch_paper.py | recipe-extractor |
| `save_recipe` | (内置, 含 grade+synonyms) | recipe-extractor |
| `grade_recipes` | grade_recipe.py | recipe-grader |
| `update_synonyms` | update_synonyms.py | recipe-db |
| `build_faiss_index` | build_index.py | recipe-db |
| `recommend_recipes` | query_index.py | recipe-db |
| `export_csv` | recipes_to_csv.py | recipe-to-csv |
| `list_database_status` | (内置) | — |

---

## 前端界面 (index.html)

### 功能

- **左侧栏:** 7 个快捷操作按钮 + 数据库状态面板
- **主聊天区:** Markdown 渲染（marked.js + highlight.js 语法高亮）
- **矿石参数表单:** "搜索论文"和"推荐配方"弹出 6 字段表单
- **SSE 实时进度:** 每个 tool 调用实时推送状态
- **进度计数器:** 保存配方时显示 (3/10) 进度

### 进度事件

| 事件类型 | 显示效果 |
|---------|---------|
| `thinking` | 正在分析请求...（轮次 N） |
| `skill` | 📚 加载技能文档: recipe-extractor |
| `tool` | 💾 正在保存配方（3/10）... |
| `tool_done` | ✅ 已保存 zhang_exp1.json, 评分: 83/100 |
| `intermediate` | 论文概述（渲染为聊天气泡） |
| `complete` | 最终回复 |

### 通信

```
POST /api/chat  → SSE stream (progress + done)
POST /api/reset → 重置会话
POST /api/reload → 重新加载所有 SKILL.md
GET  /api/status → 数据库状态
```

---

## 关键设计决策

| 决策 | 原因 |
|------|------|
| 一实验一文件 (v3 flat) | 防止输出截断，每个 JSON 只有 2-5KB |
| 近义词自动增长 | 中文术语→英文标准名，无需手动维护 |
| 矿石向量化不含药剂 | 推荐基于"矿石相似"而非"药剂相似" |
| SKILL.md 按需加载 | 避免 system prompt 爆 context (54KB→220 tokens) |
| save_recipe 三合一 | 减少 tool call 轮次 (3 轮→1 轮/实验) |
| SSE 而非 WebSocket | 单向推送够用，实现更简单 |
| 抛弃 OpenClaw | WebSocket challenge 认证太复杂，直连 DeepSeek API 更可控 |
| fetch_paper 60K 截断 | 表格全保留，文本裁两头，留 context 给 JSON 输出 |

---

## 部署与运行

### 依赖安装

```bash
pip3 install pymupdf faiss-cpu sentence-transformers --break-system-packages
```

### 环境变量

```bash
export DEEPSEEK_API_KEY="sk-xxxx"
export HF_ENDPOINT="https://hf-mirror.com"    # HuggingFace 镜像
```

### 启动

```bash
cd ~/tin-agent
python3 server.py            # 浏览器自动打开 http://localhost:8080
python3 server.py --port 9000 --no-browser
python3 agent.py              # 纯命令行模式
```

### macOS 注意事项

- SOCKS 代理需要 `pip3 install python-socks --break-system-packages`
- SSL 问题运行 `/Applications/Python\ 3.x/Install\ Certificates.command`
- 端口被占用: `lsof -ti:8080 | xargs kill -9`

---

## 完整工作流示例

```
用户: ~/Downloads/article5.pdf

→ [agent] 调用 fetch_paper_text
  → [skill] 首次加载 recipe-extractor/SKILL.md
  → [fetch_paper.py] 提取 60K 字符 + 所有表格

→ [agent] DeepSeek 输出论文概述
  → 前端实时显示概述气泡

→ [agent] DeepSeek 逐个调用 save_recipe
  → [save_recipe] 保存 JSON + grade_recipe.py --merge + update_synonyms.py
  → [前端] 进度条: 💾 正在保存配方（1/10）...
  → [自动压缩] history > 60K 时压缩旧 tool 结果
  → 重复直到所有实验保存完毕

→ [agent] DeepSeek 输出最终汇总
  → 前端渲染 Markdown 表格
  → 侧边栏数据库状态自动刷新
```

---

## 项目演化历程

| 阶段 | 时间 | 内容 |
|------|------|------|
| Phase 1 | 2026-03-16 | OpenClaw 5 个 Skill 初始构建 |
| Phase 2 | 2026-03-18 | 模板 v1→v2：添加 `{value, source}` 格式 |
| Phase 3 | 2026-03-18 | 模板 v2→v3：一实验一文件，plain values |
| Phase 4 | 2026-03-18 | CSV 设计：100 列 ML 训练表 |
| Phase 5 | 2026-03-18 | FAISS 向量搜索 + 近义词自动增长 |
| Phase 6 | 2026-03-18 | 放弃 OpenClaw Gateway (WebSocket 太复杂) |
| Phase 7 | 2026-03-18 | 自建 agent.py + server.py + index.html |
| Phase 8 | 2026-03-18 | SKILL.md 按需加载 (54KB→220 tokens system prompt) |
| Phase 9 | 2026-03-18 | SSE 进度推送 + save_recipe 三合一 |
| Phase 10 | 2026-03-18 | 论文概述 + 进度计数器 + Markdown 渲染 |

---

*最后更新: 2026-03-18*
