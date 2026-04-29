#!/usr/bin/env python3
"""
agent.py — Flotation expert agent powered by DeepSeek API.
Routes user requests to skill scripts via function calling.

Can be used standalone (CLI) or imported by server.py.

Usage:
    python3 agent.py                    # interactive CLI
    python3 agent.py --once "搜索锡矿浮选论文"  # single query
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
import ssl

# ═══════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# Paths — relative to this script's directory
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(PROJECT_DIR, "skills")
DATABASE_DIR = os.path.join(PROJECT_DIR, "database")
RECIPES_DIR = os.path.join(DATABASE_DIR, "recipes")
DATASETS_DIR = os.path.join(DATABASE_DIR, "datasets")
SYNONYMS_PATH = os.path.join(DATABASE_DIR, "synonyms.json")
FAISS_DIR = os.path.join(DATABASE_DIR, "faiss_index")


# ═══════════════════════════════════════════════════
# System prompt — slim routing table only
# ═══════════════════════════════════════════════════

SYSTEM_PROMPT = """你是锡矿浮选药剂配方专家系统（浮选助手）。用中文回复，技术术语中英文标注。

# 核心规则

- 所有数据操作必须通过 tool 调用完成，绝不能编造数据
- 含铅药剂（Pb、lead、铅）必须标注 ⚠️ 环保风险
- 不展示脚本运行细节，只展示最终格式化结果

# 数据路径

- 配方 JSON: {recipes_dir}
- CSV 数据集: {datasets_dir}
- 近义词词典: {synonyms_path}
- FAISS 索引: {faiss_dir}

# 配方提取流程（严格遵循此顺序）

当用户提供 PDF 或 URL 时，你必须按以下步骤执行：

## 步骤 1: 获取全文
调用 fetch_paper_text 获取论文全文。

## 步骤 2: 输出论文概述（在 save_recipe 之前必须先输出文字回复）
阅读全文后，你必须先回复一段文字概述，格式如下：

```
📄 **论文概述**

- **标题**: {title}
- **作者**: {authors}
- **年份**: {year}
- **研究矿石**: {ore type, valuable minerals, gangue minerals}
- **研究目的**: {一句话概括}
- **实验数量**: 共识别到 **{N}** 个独立实验
- **关键发现**: {最佳回收率/品位}

---

⏳ 开始提取 {N} 个配方，每个实验保存为独立 JSON 文件...
```

这段概述必须在任何 save_recipe 调用之前输出。

## 步骤 3: 逐个保存实验
对每个实验调用 save_recipe。save_recipe 会自动评分和更新近义词。
不要再单独调用 grade_recipes 或 update_synonyms。

## 步骤 4: 输出最终汇总

所有实验保存完毕后，输出汇总：

```
✅ **提取完成**

- 论文: {title} ({year})
- 总文件数: {N}
- 评分汇总:
  - {filename1} → [{rating}] {score}/100
  - {filename2} → [{rating}] {score}/100
  ...
- 平均分: {avg}/100
```

# Pipeline

搜索论文 (search_papers)
  → 提取PDF全文 (fetch_paper_text)
    → 先输出论文概述（文字回复）
    → 逐个保存实验 (save_recipe，自动含评分+近义词)
    → 输出最终汇总

重要：save_recipe 已包含评分和近义词更新，不要再额外调用 grade_recipes 和 update_synonyms。
只有需要"批量重新评分"时才使用 grade_recipes。

# 工具选择

| 用户意图 | 调用工具 |
|---------|---------|
| 搜索论文、咨询矿石问题 | search_papers |
| 给了PDF路径或URL | fetch_paper_text → 概述 → save_recipe |
| 推荐配方 | recommend_recipes |
| 建立索引 | build_faiss_index |
| 导出CSV | export_csv |
| 评分/重新评分 | grade_recipes |
| 查看状态 | list_database_status |

当你需要执行某个工具时，系统会自动为你加载该工具对应的完整技能文档。
""".format(
    recipes_dir=RECIPES_DIR,
    datasets_dir=DATASETS_DIR,
    synonyms_path=SYNONYMS_PATH,
    faiss_dir=FAISS_DIR,
)

# ═══════════════════════════════════════════════════
# Skill ↔ Tool mapping (which SKILL.md to load for which tool)
# ═══════════════════════════════════════════════════

TOOL_TO_SKILL = {
    "search_papers":      "tin-consultant",
    "fetch_paper_text":   "recipe-extractor",
    "save_recipe":        "recipe-extractor",
    "grade_recipes":      "recipe-grader",
    "update_synonyms":    "recipe-db",
    "build_faiss_index":  "recipe-db",
    "recommend_recipes":  "recipe-db",
    "export_csv":         "recipe-to-csv",
}

# Cache loaded skills to avoid re-reading within same session
_skill_cache = {}


def load_skill_content(skill_name):
    """Read a SKILL.md from disk. Cached per session."""
    if skill_name in _skill_cache:
        return _skill_cache[skill_name]

    content = ""
    skill_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    if os.path.exists(skill_path):
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

    # Also load template for recipe-extractor
    if skill_name == "recipe-extractor":
        template_path = os.path.join(SKILLS_DIR, skill_name, "references", "recipe_template.json")
        if os.path.exists(template_path):
            with open(template_path, "r", encoding="utf-8") as f:
                content += f"\n\n# RECIPE TEMPLATE (v3)\n```json\n{f.read()}\n```"

    _skill_cache[skill_name] = content
    return content


def clear_skill_cache():
    """Clear cache so skills are re-read from disk."""
    _skill_cache.clear()


# ═══════════════════════════════════════════════════
# Tool definitions (OpenAI function calling format)
# ═══════════════════════════════════════════════════

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_papers",
            "description": "搜索锡矿浮选相关学术论文。输入矿石关键词，返回论文列表（标题、作者、年份、URL、PDF链接）。搜索源包括 arXiv、Semantic Scholar、CrossRef、OpenAlex、CORE。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，如 'cassiterite flotation SHA collector quartz gangue'"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回论文数量，默认10",
                        "default": 10
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_paper_text",
            "description": "从 PDF 提取全文和表格。支持本地文件路径或远程 URL。返回的文本可用于配方提取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "本地 PDF 文件路径（与 url 二选一）"
                    },
                    "url": {
                        "type": "string",
                        "description": "论文 URL（与 file_path 二选一）"
                    },
                    "pdf_url": {
                        "type": "string",
                        "description": "直接 PDF 下载链接（可选，配合 url 使用）"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_recipe",
            "description": "将提取的配方数据保存为 JSON 文件，并自动评分和更新近义词词典。每个实验保存为一个独立文件。保存后自动运行 grade_recipe.py --merge 和 update_synonyms.py，无需再单独调用 grade_recipes 或 update_synonyms。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名，如 zhang_2024_exp1.json"
                    },
                    "content": {
                        "type": "string",
                        "description": "完整的 v3 模板 JSON 内容（字符串格式）"
                    }
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grade_recipes",
            "description": "对配方 JSON 文件进行评分。使用 --merge 将评分写回原文件。可评分单个文件或整个目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "单个 JSON 文件路径（与 directory 二选一）"
                    },
                    "directory": {
                        "type": "string",
                        "description": "JSON 文件目录（与 file 二选一），默认为 recipes 目录"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_synonyms",
            "description": "扫描配方 JSON 文件，提取新术语并更新近义词词典。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "单个 JSON 文件路径（与 directory 二选一）"
                    },
                    "directory": {
                        "type": "string",
                        "description": "JSON 文件目录（与 file 二选一）"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "build_faiss_index",
            "description": "从 recipes 目录构建 FAISS 向量索引。索引只包含矿石特征，用于按矿石相似度推荐配方。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rebuild": {
                        "type": "boolean",
                        "description": "是否强制重建索引",
                        "default": True
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_recipes",
            "description": "根据矿石参数在 FAISS 索引中搜索最相似的历史矿石，返回对应的药剂配方推荐。排序 = 0.7×相似度 + 0.3×评分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ore_params": {
                        "type": "object",
                        "description": "矿石参数 JSON，如 {\"ore_type\":\"oxide tin\",\"gangue\":\"quartz\",\"d80_um\":\"45\"}",
                        "properties": {
                            "ore_type": {"type": "string"},
                            "gangue": {"type": "string"},
                            "gangue_secondary": {"type": "string"},
                            "d80_um": {"type": "string"},
                            "sno2_pct": {"type": "string"},
                            "pain_point": {"type": "string"}
                        }
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回推荐数量，默认5",
                        "default": 5
                    }
                },
                "required": ["ore_params"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "export_csv",
            "description": "将配方数据导出为 ML 训练 CSV 数据集（recipe_recommendation_train.csv）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "append": {
                        "type": "boolean",
                        "description": "是否追加到已有 CSV",
                        "default": False
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_database_status",
            "description": "查看数据库当前状态：配方文件数、索引状态、CSV 行数、近义词词典大小。",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
]


# ═══════════════════════════════════════════════════
# Tool execution
# ═══════════════════════════════════════════════════

def _run_script(script_path, args_list, timeout=300):
    """Run a Python script and return its stdout."""
    cmd = [sys.executable, script_path] + args_list
    env = os.environ.copy()
    env["HF_ENDPOINT"] = env.get("HF_ENDPOINT", "https://hf-mirror.com")
    # Clear proxy for local operations
    for k in ["ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"]:
        env.pop(k, None)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
        output = result.stdout.strip()
        if result.returncode != 0 and not output:
            output = result.stderr.strip() or f"Script exited with code {result.returncode}"
        return output
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Script timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _skill_path(skill_name, script_name):
    return os.path.join(SKILLS_DIR, skill_name, "scripts", script_name)


def execute_tool(name, arguments):
    """Execute a tool and return result string."""

    if name == "search_papers":
        script = _skill_path("tin-consultant", "search_papers.py")
        query = arguments.get("query", "")
        limit = arguments.get("limit", 10)
        return _run_script(script, ["--query", query, "--limit", str(limit)])

    elif name == "fetch_paper_text":
        script = _skill_path("recipe-extractor", "fetch_paper.py")
        args = []
        if arguments.get("file_path"):
            path = os.path.expanduser(arguments["file_path"])
            if not os.path.exists(path):
                return json.dumps({"error": f"File not found: {path}"})
            args += ["--file", path]
        elif arguments.get("url"):
            args += ["--url", arguments["url"]]
            if arguments.get("pdf_url"):
                args += ["--pdf-url", arguments["pdf_url"]]
        else:
            return json.dumps({"error": "Provide file_path or url"})
        return _run_script(script, args)

    elif name == "save_recipe":
        filename = arguments.get("filename", "")
        content = arguments.get("content", "")
        os.makedirs(RECIPES_DIR, exist_ok=True)
        filepath = os.path.join(RECIPES_DIR, filename)
        try:
            data = json.loads(content)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON: {e}"})

        # Auto-run grade + synonyms (saves 2 extra tool call rounds)
        grade_result = ""
        grade_script = _skill_path("recipe-grader", "grade_recipe.py")
        if os.path.exists(grade_script):
            grade_result = _run_script(grade_script, ["--file", filepath, "--merge"])

        syn_result = ""
        syn_script = _skill_path("recipe-db", "update_synonyms.py")
        if os.path.exists(syn_script):
            syn_result = _run_script(syn_script, ["--file", filepath, "--dict", SYNONYMS_PATH])

        # Parse grade info
        score_info = ""
        try:
            gj = json.loads(grade_result)
            scores = gj.get("per_file", [{}])[0].get("scores", [])
            if scores:
                score_info = f", 评分: {scores[0]}/100"
        except Exception:
            pass

        return json.dumps({
            "status": "saved_and_graded",
            "path": filepath,
            "filename": filename,
            "grade": grade_result[:500] if grade_result else "grading script not found",
            "synonyms": syn_result[:200] if syn_result else "synonyms script not found",
            "summary": f"已保存 {filename}{score_info}，近义词已更新"
        }, ensure_ascii=False)

    elif name == "grade_recipes":
        script = _skill_path("recipe-grader", "grade_recipe.py")
        args = ["--merge"]
        if arguments.get("file"):
            args += ["--file", arguments["file"]]
        else:
            args += ["--dir", arguments.get("directory", RECIPES_DIR)]
        return _run_script(script, args)

    elif name == "update_synonyms":
        script = _skill_path("recipe-db", "update_synonyms.py")
        args = ["--dict", SYNONYMS_PATH]
        if arguments.get("file"):
            args += ["--file", arguments["file"]]
        else:
            args += ["--dir", arguments.get("directory", RECIPES_DIR)]
        return _run_script(script, args)

    elif name == "build_faiss_index":
        script = _skill_path("recipe-db", "build_index.py")
        args = ["--recipes-dir", RECIPES_DIR, "--mirror"]
        if arguments.get("rebuild", True):
            args.append("--rebuild")
        return _run_script(script, args, timeout=600)

    elif name == "recommend_recipes":
        script = _skill_path("recipe-db", "query_index.py")
        ore_params = arguments.get("ore_params", {})
        top_k = arguments.get("top_k", 5)
        args = ["--query", json.dumps(ore_params, ensure_ascii=False),
                "--top-k", str(top_k), "--mirror"]
        return _run_script(script, args, timeout=120)

    elif name == "export_csv":
        script = _skill_path("recipe-to-csv", "recipes_to_csv.py")
        os.makedirs(DATASETS_DIR, exist_ok=True)
        args = ["--dir", RECIPES_DIR, "--output-dir", DATASETS_DIR]
        if arguments.get("append"):
            args.append("--append")
        return _run_script(script, args)

    elif name == "list_database_status":
        status = {}
        # Recipe count
        if os.path.isdir(RECIPES_DIR):
            status["recipe_files"] = len([f for f in os.listdir(RECIPES_DIR) if f.endswith(".json")])
        else:
            status["recipe_files"] = 0

        # Synonym count
        if os.path.exists(SYNONYMS_PATH):
            try:
                with open(SYNONYMS_PATH) as f:
                    syn = json.load(f)
                status["synonym_entries"] = sum(len(v) for k, v in syn.items() if isinstance(v, dict) and not k.startswith("_"))
            except:
                status["synonym_entries"] = 0
        else:
            status["synonym_entries"] = 0

        # Index status
        status["faiss_index_exists"] = os.path.exists(os.path.join(FAISS_DIR, "recipes.index"))

        # CSV row count
        csv_path = os.path.join(DATASETS_DIR, "recipe_recommendation_train.csv")
        if os.path.exists(csv_path):
            with open(csv_path) as f:
                status["csv_rows"] = sum(1 for _ in f) - 1  # minus header
        else:
            status["csv_rows"] = 0

        return json.dumps(status, ensure_ascii=False, indent=2)

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ═══════════════════════════════════════════════════
# DeepSeek API calls
# ═══════════════════════════════════════════════════

def _make_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def call_deepseek(messages, tools=None):
    """Call DeepSeek API. Returns (message_dict, finish_reason) or raises RuntimeError."""
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"

    # Sanitize messages — DeepSeek is strict about format
    clean_msgs = []
    for msg in messages:
        m = {"role": msg["role"], "content": msg.get("content", "")}
        if msg.get("tool_calls"):
            m["tool_calls"] = msg["tool_calls"]
            # DeepSeek requires content to be null or absent for tool_call messages
            if not m["content"]:
                m["content"] = None
        if msg.get("tool_call_id"):
            m["tool_call_id"] = msg["tool_call_id"]
        clean_msgs.append(m)

    body = {
        "model": MODEL,
        "messages": clean_msgs,
        "max_tokens": 8192,
        "temperature": 0.3,
    }
    if tools:
        body["tools"] = tools

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    print(f"  [API] Request: {len(data):,} bytes, {len(clean_msgs)} messages", file=sys.stderr, flush=True)
    ctx = _make_ssl_context()

    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    })

    try:
        with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        choice = result["choices"][0]
        return choice["message"], choice.get("finish_reason", "stop")
    except Exception as e:
        err_detail = ""
        if hasattr(e, 'read'):
            try:
                err_detail = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
        if hasattr(e, 'code'):
            err_detail = f"HTTP {e.code}: {err_detail}"
        if not err_detail:
            err_detail = str(e)

        print(f"  [API ERROR] {err_detail[:300]}", file=sys.stderr, flush=True)
        raise RuntimeError(err_detail[:300])


# ═══════════════════════════════════════════════════
# Agent loop
# ═══════════════════════════════════════════════════

TOOL_LABELS = {
    "search_papers":     "🔎 正在搜索论文...",
    "fetch_paper_text":  "📄 正在提取PDF全文...",
    "save_recipe":       "💾 正在保存配方...",
    "grade_recipes":     "⭐ 正在评分...",
    "update_synonyms":   "📖 正在更新近义词词典...",
    "build_faiss_index": "🗄️ 正在构建向量索引...",
    "recommend_recipes": "🔍 正在搜索推荐...",
    "export_csv":        "📊 正在导出CSV...",
    "list_database_status": "📋 正在查询状态...",
}


def _trim_history(history):
    """Aggressive trim for error recovery. Cut all tool results to 500 chars."""
    return [
        {**m, "content": (m.get("content", "") or "")[:500] + "..." }
        if m.get("role") == "tool" and len(m.get("content", "") or "") > 500
        else m
        for m in history
    ]


def _compress_history(history):
    """Compress old tool results to summaries. Keeps last 2 tool results in full."""
    # Find all tool message indices
    tool_indices = [i for i, m in enumerate(history) if m.get("role") == "tool"]

    # Keep last 2 tool results full, compress older ones
    compress_before = tool_indices[-2] if len(tool_indices) >= 2 else -1

    compressed = []
    for i, msg in enumerate(history):
        if msg.get("role") == "tool" and i < compress_before:
            content = msg.get("content", "")
            # Extract just the summary/status from tool result
            summary = content[:300]
            if len(content) > 300:
                summary += "... [compressed]"
            compressed.append({**msg, "content": summary})
        else:
            compressed.append(msg)

    return compressed


def agent_chat(user_message, history=None, _loaded_skills=None, on_progress=None):
    """
    Process one user message through the agent loop.
    Auto-injects relevant SKILL.md content when a tool is first called.
    Compresses history between rounds to prevent context overflow.
    Returns (assistant_reply_text, updated_history).
    """
    def emit(event, data):
        if on_progress:
            on_progress(event, data)
        print(f"  [{event}] {data}", file=sys.stderr, flush=True)

    if history is None:
        history = [{"role": "system", "content": SYSTEM_PROMPT}]

    if _loaded_skills is None:
        _loaded_skills = set()

    history.append({"role": "user", "content": user_message})

    max_rounds = 20  # higher limit since save_recipe now does 3-in-1
    for round_num in range(max_rounds):
        # Compress history if getting large (>60K bytes)
        hist_size = sum(len(m.get("content", "") or "") for m in history)
        if hist_size > 60000:
            emit("thinking", "⚠️ 压缩对话历史...")
            history = _compress_history(history)

        emit("thinking", f"正在分析请求...（轮次 {round_num+1}）")

        try:
            message, finish_reason = call_deepseek(history, tools=TOOLS)
        except Exception as e:
            emit("tool_error", f"⚠️ API 错误，尝试恢复...")
            history = _trim_history(history)
            try:
                message, finish_reason = call_deepseek(history, tools=TOOLS)
            except Exception as e2:
                return f"❌ DeepSeek API 错误：{e2}", history

        tool_calls = message.get("tool_calls", [])

        # If message has both text content AND tool_calls, emit text as intermediate
        msg_content = message.get("content", "") or ""
        if tool_calls and msg_content.strip():
            emit("intermediate", msg_content)

        if tool_calls:
            history.append(message)

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    func_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    func_args = {}

                # Load skill doc (will be prepended to tool result)
                skill_prefix = ""
                skill_name = TOOL_TO_SKILL.get(func_name)
                if skill_name and skill_name not in _loaded_skills:
                    skill_content = load_skill_content(skill_name)
                    if skill_content:
                        _loaded_skills.add(skill_name)
                        emit("skill", f"📚 加载技能文档: {skill_name}")
                        skill_prefix = f"[技能文档 {skill_name} 已加载，请严格遵循以下规则]\n\n{skill_content}\n\n[以下是工具执行结果]\n\n"

                # Execute tool
                label = TOOL_LABELS.get(func_name, f"⚙️ 正在执行 {func_name}...")
                emit("tool", label)
                result = execute_tool(func_name, func_args)

                # Brief result summary for progress
                try:
                    rj = json.loads(result)
                    if "error" in rj:
                        emit("tool_error", f"❌ {func_name}: {rj['error'][:80]}")
                    elif func_name == "save_recipe":
                        summary = rj.get("summary", "")
                        emit("tool_done", f"✅ {summary}" if summary else f"✅ 已保存+评分+更新近义词")
                    elif func_name == "grade_recipes":
                        avg = rj.get("overall_average", rj.get("per_file", [{}])[0].get("average", ""))
                        emit("tool_done", f"✅ 评分完成 (平均: {avg}/100)")
                    else:
                        emit("tool_done", f"✅ {func_name} 完成")
                except (json.JSONDecodeError, IndexError, KeyError):
                    emit("tool_done", f"✅ {func_name} 完成")

                if len(result) > 30000:
                    result = result[:30000] + "\n\n[... truncated ...]"

                # Prepend skill doc to tool result so it stays in the right position
                tool_content = skill_prefix + result

                history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_content,
                })

            continue

        # No tool calls — text response
        reply = message.get("content", "") or ""
        history.append({"role": "assistant", "content": reply})

        # Check if this is an intermediate response (paper overview, not final)
        # If the text contains "开始提取" or "⏳", DeepSeek plans to continue with saves
        is_intermediate = any(marker in reply for marker in ["开始提取", "⏳", "开始保存", "逐个保存"])

        if is_intermediate and round_num < max_rounds - 2:
            # Emit overview to frontend immediately, then continue the loop
            emit("intermediate", reply)
            # Nudge DeepSeek to continue saving
            history.append({"role": "user", "content": "请继续，逐个保存所有实验配方。"})
            continue

        # Final response
        emit("complete", "生成回复完成")
        return reply, history

    return "（达到最大工具调用轮数，请简化请求）", history


# ═══════════════════════════════════════════════════
# CLI mode
# ═══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Flotation Expert Agent (DeepSeek)")
    parser.add_argument("--once", default="", help="Single query, then exit")
    args = parser.parse_args()

    # Ensure directories exist
    for d in [RECIPES_DIR, DATASETS_DIR, FAISS_DIR]:
        os.makedirs(d, exist_ok=True)

    if not DEEPSEEK_API_KEY:
        print("❌ DEEPSEEK_API_KEY 未设置。请运行:")
        print("   export DEEPSEEK_API_KEY='sk-your-key-here'")
        sys.exit(1)

    if args.once:
        reply, _ = agent_chat(args.once)
        print(reply)
        return

    # Interactive mode
    print("⛏️  浮选助手 (DeepSeek Agent)")
    print("  输入矿石信息、论文链接或操作指令")
    print("  输入 quit 退出\n")

    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    loaded_skills = set()

    while True:
        try:
            user_input = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("再见！")
            break

        try:
            reply, history = agent_chat(user_input, history, _loaded_skills=loaded_skills)
            print(f"\n⛏️> {reply}\n")
        except Exception as e:
            print(f"\n❌ 错误：{e}\n")


if __name__ == "__main__":
    main()
