# ⛏️ 锡矿浮选配方专家系统

AI-powered system for extracting, grading, and recommending tin ore flotation reagent recipes from academic literature.

## Features

- 🔎 **Paper Search** — Search 5 academic databases (arXiv, Semantic Scholar, CrossRef, OpenAlex, CORE)
- 📄 **Recipe Extraction** — Extract flotation experiments from PDFs using LLM
- ⭐ **Auto Grading** — 100-point scoring across 4 dimensions (completeness, chemistry, results, safety)
- 🗄️ **Vector Database** — FAISS-powered ore similarity search for recipe recommendations
- 📊 **ML Export** — 100-column CSV training dataset
- 📖 **Auto Synonyms** — Chinese↔English mineral/reagent terminology dictionary

## Architecture

```
Browser (index.html)
  → POST /api/chat (SSE stream)
    → server.py (HTTP + SSE progress)
      → agent.py (DeepSeek API + Function Calling)
        → skill scripts (search, extract, grade, index, recommend, export)
```

## Quick Start

### 1. Clone

```bash
git clone https://github.com/YOUR_USERNAME/tin-flotation-agent.git
cd tin-flotation-agent
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env and add your DeepSeek API key
```

Or export directly:

```bash
export DEEPSEEK_API_KEY="sk-your-key-here"
```

### 4. Run

```bash
python server.py
```

Opens browser at `http://localhost:8080`.

**CLI mode:**

```bash
python agent.py                           # Interactive
python agent.py --once "搜索锡矿浮选论文"   # Single query
```

## Project Structure

```
tin-flotation-agent/
├── agent.py              ← Core agent (DeepSeek API + 9 tools)
├── server.py             ← HTTP server (SSE streaming)
├── index.html            ← Web UI (React + Markdown)
├── requirements.txt
├── .env.example
│
├── skills/               ← Skill modules (SKILL.md + scripts)
│   ├── tin-consultant/         Paper search
│   ├── recipe-extractor/       PDF extraction + v3 template
│   ├── recipe-grader/          100-point scoring
│   ├── recipe-to-csv/          ML dataset export
│   └── recipe-db/              FAISS index + synonyms
│
└── database/             ← Data storage (auto-created)
    ├── recipes/                JSON files (one per experiment)
    ├── datasets/               CSV training data
    ├── synonyms.json           Terminology dictionary
    └── faiss_index/            Vector index
```

## Skills

| Skill | Purpose | Key Script |
|-------|---------|------------|
| `tin-consultant` | Ore consultation + paper search | `search_papers.py` |
| `recipe-extractor` | PDF text extraction + recipe template | `fetch_paper.py` |
| `recipe-grader` | 4-dimension grading (100 pts) | `grade_recipe.py` |
| `recipe-to-csv` | ML training dataset (100 columns) | `recipes_to_csv.py` |
| `recipe-db` | FAISS index + synonym dictionary | `build_index.py`, `query_index.py` |

## Key Design Decisions

- **One experiment per file** (v3 flat JSON) — prevents output truncation, each file 2-5KB
- **On-demand SKILL loading** — system prompt only ~220 tokens; full SKILL.md loaded when tool is first called
- **save_recipe 3-in-1** — auto-runs grading + synonym update, reducing API rounds from 3→1 per experiment
- **SSE streaming** — real-time progress (tool status, save counter, paper overview)
- **Ore-only embeddings** — recommendations based on ore similarity, not reagent similarity
- **Auto-growing synonyms** — Chinese terms auto-mapped to English (50+ seed mappings)

## Grading System

| Dimension | Points | Evaluates |
|-----------|--------|-----------|
| Completeness | 25 | Field coverage |
| Chemistry | 30 | pH-collector match, antagonism, dosage |
| Results | 25 | Recovery and grade values |
| Safety | 20 | Lead/mercury/chromium flagging |

Rating: A(75+) B(60-74) C(45-59) D(0-44)

## Recommendation Formula

```
final_score = 0.7 × cosine_similarity + 0.3 × normalized_recipe_score
```

## Environment Notes

- **HuggingFace Mirror** (China): `export HF_ENDPOINT=https://hf-mirror.com`
- **SOCKS Proxy**: `pip install python-socks` if needed
- **macOS SSL**: Run `/Applications/Python\ 3.x/Install\ Certificates.command`

## License

MIT
