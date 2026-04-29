"""
MinerU 预处理器: 语义评分 (Bi-Encoder + Cross-Encoder)
=======================================================
Block 级别评分，过滤无关内容，降低 LLM 输入 token。

三层评分:
  Layer 1 — 规则过滤: page_header / page_number / references → 直接跳过
  Layer 2 — Bi-Encoder: 每个 block 与 29 个 column query 算语义相似度 (快)
  Layer 3 — Cross-Encoder: 对 top-N block 做精排 (可选，更准)

环境:
  pip install sentence-transformers --break-system-packages
  export HF_ENDPOINT=https://hf-mirror.com  # 中国用户

用法:
  python preprocess.py score article.json              # 查看统计
  python preprocess.py detail article.json             # 逐 block 详情
  python preprocess.py dump article.json -o cleaned.md # 输出过滤文本
  python preprocess.py dump article.json --mode keyword # 不用模型，纯关键词
"""

import json
import re
import os
import numpy as np
from pathlib import Path
from typing import Optional

# ===========================================================================
# Column Queries — 每列对应一段描述性查询，用于语义匹配
# ===========================================================================

COLUMN_QUERIES = {
    # --- Material ---
    "alloy":
        "aluminum alloy designation such as 6061, 2024, 2A14, A356, 7075 used as matrix material",
    "temper":
        "heat treatment temper condition of the alloy such as T6, T4, O, as-cast, as-extruded, solution treated and aged",
    "reinf_type":
        "type of reinforcement particles in the composite such as SiC particles, SiCp, B4C, Al2O3 ceramic reinforcement",
    "reinf_vol_pct":
        "volume fraction or weight percentage of reinforcement particles, such as 15 vol% SiC or 20 wt% reinforcement content",
    "reinf_size_min_um":
        "particle size or diameter of reinforcement in micrometers, such as 7 μm average particle size or 3-10 μm size range",
    "thickness_mm":
        "plate thickness or specimen thickness in millimeters for the welded material",
    "has_interlayer":
        "use of interlayer, intermediate layer, filler foil, or insert material placed between the workpieces before welding",
    "interlayer_type":
        "specific interlayer material type such as titanium Ti foil, copper Cu interlayer, nickel Ni coating, Al-Si filler",
    # --- Process ---
    "method":
        "welding or joining method such as laser welding, friction stir welding FSW, TIG, MIG, electron beam, brazing, diffusion bonding",
    "joint_type":
        "joint configuration type such as butt joint, lap joint, T-joint, fillet weld",
    "laser_power_kW":
        "laser power in kilowatts kW used during laser welding process",
    "welding_speed_mm_s":
        "welding speed, travel speed, or traverse speed in mm/s or mm/min during the joining process",
    "rotation_speed_rpm":
        "tool rotation speed in RPM for friction stir welding FSW process",
    "heat_input_kJ_mm":
        "heat input or line energy in kJ/mm or J/mm calculated from welding parameters",
    "shielding_gas":
        "shielding gas or protective gas type such as argon Ar, helium He, nitrogen N2 used during welding",
    "gas_flow_L_min":
        "shielding gas flow rate in liters per minute L/min",
    # --- Results ---
    "tensile_MPa":
        "tensile strength or ultimate tensile strength UTS measurement result in MPa of the welded joint",
    "yield_MPa":
        "yield strength or 0.2% proof stress measurement in MPa of the welded joint",
    "elongation_pct":
        "elongation at fracture or ductility percentage of the welded joint specimen",
    "efficiency_pct":
        "joint efficiency percentage calculated as joint strength divided by base material strength",
    "hardness_HV":
        "hardness measurement such as Vickers microhardness HV, HRC, Knoop across the weld profile",
    # --- Microstructure / Defect (binary) ---
    "has_Al4C3":
        "formation of aluminum carbide Al4C3 or Al₄C₃ brittle needle-like interfacial reaction product at SiC/Al interface",
    "has_porosity":
        "porosity, voids, gas pores, or gas bubbles observed in the weld zone",
    "has_cracks":
        "cracks, hot tearing, solidification cracking, or fissures in the weld or heat affected zone",
    "fracture_mode":
        "fracture mode and fracture surface morphology such as ductile dimple fracture, brittle cleavage, mixed mode, intergranular",
}

# --- 规则跳过 ---
SKIP_TYPES = {"page_header", "page_footer", "page_number"}

SECTION_PATTERNS = {
    "introduction": [r'introduction', r'background'],
    "experimental": [r'experiment', r'material.*method', r'method', r'procedure'],
    "results":      [r'result', r'finding', r'observation'],
    "discussion":   [r'discussion', r'analysis', r'interpretation', r'mechanism'],
    "conclusions":  [r'conclusion', r'summary', r'concluding'],
    "references":   [r'reference', r'bibliography'],
    "meta":         [r'credit', r'author', r'acknowledg', r'declaration', r'data\s*availab'],
}

# --- 关键词后备 (无模型时用) ---
COLUMN_KEYWORDS = {
    "alloy": [r'\b\d{4}\b', r'2a14', r'6061', r'2024', r'7075', r'a356', r'aluminum\s*(?:alloy|matrix)'],
    "temper": [r'T6', r'T4', r'as-cast', r'as-extruded', r'heat\s*treat', r'temper', r'aging'],
    "reinf_type": [r'sic[pw]?(?:/|\b)', r'b4c', r'al2o3', r'reinforc', r'particle', r'composite'],
    "reinf_vol_pct": [r'\d+\s*vol\.?\s*%', r'\d+\s*wt\.?\s*%', r'volume\s*fraction'],
    "reinf_size_min_um": [r'\d+\s*[μµ]m', r'particle\s*size', r'diameter'],
    "thickness_mm": [r'\d+\s*mm\s*(?:thick|plate)', r'thickness', r'medium.thick'],
    "has_interlayer": [r'interlayer', r'intermediate\s*layer', r'foil', r'insert'],
    "interlayer_type": [r'Ti\b.*(?:foil|layer)', r'Cu\b.*layer', r'Ni\b.*coat', r'er\d{4}'],
    "method": [r'laser\s*weld', r'friction\s*stir', r'\bfsw\b', r'\btig\b', r'\bmig\b', r'electron\s*beam', r'brazing', r'diffusion\s*bond'],
    "joint_type": [r'butt\s*(?:joint|weld)', r'lap\s*(?:joint|weld)', r'T-joint', r'fillet'],
    "laser_power_kW": [r'\d+\.?\d*\s*kw', r'laser\s*power'],
    "welding_speed_mm_s": [r'\d+\.?\d*\s*mm/s', r'\d+\.?\d*\s*mm/min', r'welding\s*(?:speed|velocity)'],
    "rotation_speed_rpm": [r'\d+\s*rpm', r'rotation\s*speed'],
    "heat_input_kJ_mm": [r'\d+\.?\d*\s*(?:kj/mm|j/mm)', r'heat\s*input'],
    "shielding_gas": [r'shielding\s*gas', r'\bargon\b', r'99\s*%\s*ar', r'gas\s*flow'],
    "gas_flow_L_min": [r'\d+\.?\d*\s*(?:l/min|L/min)', r'gas\s*flow\s*rate'],
    "tensile_MPa": [r'\d+\.?\d*\s*mpa', r'tensile\s*strength', r'UTS'],
    "yield_MPa": [r'yield\s*strength', r'proof\s*stress'],
    "elongation_pct": [r'elongation', r'ductility'],
    "efficiency_pct": [r'joint\s*efficiency', r'weld\s*efficiency'],
    "hardness_HV": [r'hardness', r'\bHV\b', r'vickers', r'microhardness'],
    "has_Al4C3": [r'al4c3', r'al₄c₃', r'aluminum\s*carbide', r'interfacial\s*reaction'],
    "has_porosity": [r'poros', r'void', r'pore'],
    "has_cracks": [r'crack', r'fissure', r'hot\s*tear'],
    "fracture_mode": [r'fracture\s*(?:mode|surface|morphology)', r'ductile\s*fracture', r'brittle\s*fracture', r'dimple', r'cleavage'],
}


# ===========================================================================
# MinerU 解析 (与之前相同)
# ===========================================================================

def load_mineru(filepath: str) -> list[dict]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    blocks = []
    idx = 0
    for page_idx, page in enumerate(data):
        if not isinstance(page, list): continue
        for block in page:
            if not isinstance(block, dict): continue
            b = {"_idx": idx, "_page": page_idx, "_type": block.get("type","unknown"),
                 "_text": _extract_text(block), "_bbox": block.get("bbox"), "_raw": block}
            if b["_type"] == "title":
                b["_level"] = block.get("content",{}).get("level")
                b["_section"] = _detect_section(b["_text"])
            if b["_type"] == "table":
                c = block.get("content",{})
                b["_html"] = c.get("html","")
                b["_caption"] = " ".join(item.get("content","") for item in c.get("table_caption",[]) if isinstance(item,dict))
            if b["_type"] in ("image","chart"):
                c = block.get("content",{})
                for key in ["image_caption","chart_caption"]:
                    arr = c.get(key,[])
                    if arr:
                        b["_caption"] = " ".join(item.get("content","") for item in arr if isinstance(item,dict))
                        break
            blocks.append(b); idx += 1
    return blocks


def _extract_text(block):
    c = block.get("content",{})
    if not isinstance(c, dict): return ""
    for key in ["paragraph_content","title_content","page_header_content","page_footnote_content","page_number_content"]:
        arr = c.get(key,[])
        if arr: return " ".join(item.get("content","") for item in arr if isinstance(item,dict)).strip()
    if "html" in c:
        text = re.sub(r'<[^>]+>',' ',c["html"])
        cap = " ".join(item.get("content","") for item in c.get("table_caption",[]) if isinstance(item,dict))
        return (cap + " " + re.sub(r'\s+',' ',text)).strip()
    for key in ["image_caption","chart_caption"]:
        arr = c.get(key,[])
        if arr: return " ".join(item.get("content","") for item in arr if isinstance(item,dict)).strip()
    if "math_content" in c: return c["math_content"][:200]
    return ""


def _detect_section(title_text):
    t = title_text.lower().strip()
    for sec, pats in SECTION_PATTERNS.items():
        for pat in pats:
            if re.search(pat, t): return sec
    return "other"


# ===========================================================================
# Layer 1: 规则过滤 + Section 传播
# ===========================================================================

def rule_filter(blocks: list[dict]) -> list[dict]:
    """标记规则级跳过，传播 section 标签"""
    current_section = "unknown"
    for b in blocks:
        # section propagation
        if b["_type"] == "title" and b.get("_section") and b["_section"] != "other":
            current_section = b["_section"]
        if "_section" not in b or b["_section"] == "other":
            b["_section"] = current_section

        # rule skip
        if b["_type"] in SKIP_TYPES:
            b["_skip"] = "type"; continue
        if b.get("_section") in ("references", "meta"):
            b["_skip"] = "section"; continue
        if not b["_text"] or len(b["_text"].strip()) < 10:
            b["_skip"] = "empty"; continue
        b["_skip"] = None
    return blocks


# ===========================================================================
# Layer 2: Bi-Encoder 语义评分
# ===========================================================================

_bi_model = None
_query_embeddings = None

def _load_bi_encoder(model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
    global _bi_model, _query_embeddings
    if _bi_model is not None:
        return _bi_model, _query_embeddings

    from sentence_transformers import SentenceTransformer
    print(f"  加载 Bi-Encoder: {model_name}...")
    _bi_model = SentenceTransformer(model_name)

    # 预计算 column query embeddings (只做一次)
    queries = list(COLUMN_QUERIES.values())
    _query_embeddings = _bi_model.encode(queries, normalize_embeddings=True, show_progress_bar=False)
    print(f"  ✅ {len(queries)} column queries encoded (dim={_query_embeddings.shape[1]})")
    return _bi_model, _query_embeddings


def bi_encoder_score(blocks: list[dict], model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> list[dict]:
    """
    Bi-Encoder 评分:
      1. 编码所有 block 文本 (batch)
      2. 与 29 个 column query embeddings 算余弦相似度
      3. 每个 block 得到:
         _score: 最高 column 相似度 (0~1)
         _top_columns: top-5 最相关的列名 + 分数
         _mean_sim: 平均相似度 (用于粗排)
    """
    model, q_embs = _load_bi_encoder(model_name)
    col_names = list(COLUMN_QUERIES.keys())

    # 收集需要编码的 block
    texts = []
    indices = []
    for i, b in enumerate(blocks):
        if b.get("_skip"):
            b["_score"] = 0.0; b["_top_columns"] = []; b["_mean_sim"] = 0.0
            continue
        text = b["_text"]
        # 对 table 加上 caption
        if b["_type"] == "table" and b.get("_caption"):
            text = b["_caption"] + " " + text
        texts.append(text[:512])  # 截断到 512 tokens 左右
        indices.append(i)

    if not texts:
        return blocks

    # Batch encode
    print(f"  编码 {len(texts)} blocks...")
    block_embs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False, batch_size=32)

    # 相似度矩阵: (n_blocks, n_columns)
    sim_matrix = block_embs @ q_embs.T  # cosine (both normalized)

    for j, idx in enumerate(indices):
        sims = sim_matrix[j]  # shape: (n_columns,)
        b = blocks[idx]

        # top columns
        top_k = 5
        top_indices = np.argsort(sims)[::-1][:top_k]
        b["_top_columns"] = [
            {"col": col_names[ci], "sim": round(float(sims[ci]), 3)}
            for ci in top_indices if sims[ci] > 0.15
        ]

        # score = max similarity
        b["_score"] = round(float(np.max(sims)), 3)
        b["_mean_sim"] = round(float(np.mean(sims)), 3)

        # table bonus
        if b["_type"] == "table":
            b["_score"] = min(1.0, b["_score"] + 0.15)

    return blocks


# ===========================================================================
# Layer 3: Cross-Encoder 精排 (可选)
# ===========================================================================

_cross_model = None

def _load_cross_encoder(model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
    global _cross_model
    if _cross_model is not None:
        return _cross_model
    from sentence_transformers import CrossEncoder
    print(f"  加载 Cross-Encoder: {model_name}...")
    _cross_model = CrossEncoder(model_name)
    print(f"  ✅ Cross-Encoder loaded")
    return _cross_model


def cross_encoder_rerank(blocks: list[dict], top_n: int = 50,
                          model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> list[dict]:
    """
    Cross-Encoder 精排:
      取 bi-encoder 打分最高的 top_n 个 block，
      用 cross-encoder 对每个 block × 其 top column query 做精细评分。

    更新 _score_ce 字段。
    """
    model = _load_cross_encoder(model_name)
    col_names = list(COLUMN_QUERIES.keys())
    col_queries = list(COLUMN_QUERIES.values())

    # 选 top_n blocks (按 bi-encoder _score)
    candidates = [(i, b) for i, b in enumerate(blocks) if b.get("_score", 0) > 0]
    candidates.sort(key=lambda x: -x[1]["_score"])
    candidates = candidates[:top_n]

    if not candidates:
        return blocks

    # 构建 pairs: (block_text, best_column_query)
    pairs = []
    pair_info = []  # (block_idx, col_name)
    for idx, b in candidates:
        text = b["_text"][:512]
        # 对每个 block，用 top-3 columns 做 cross-encoder
        top_cols = b.get("_top_columns", [])[:3]
        if not top_cols:
            continue
        for tc in top_cols:
            col = tc["col"]
            query = COLUMN_QUERIES[col]
            pairs.append((query, text))
            pair_info.append((idx, col))

    if not pairs:
        return blocks

    print(f"  Cross-Encoder: {len(pairs)} pairs from {len(candidates)} blocks...")
    scores = model.predict(pairs, show_progress_bar=False)

    # 聚合: 每个 block 取最高 cross-encoder score
    block_ce_scores = {}
    block_ce_cols = {}
    for k, (idx, col) in enumerate(pair_info):
        s = float(scores[k])
        if idx not in block_ce_scores or s > block_ce_scores[idx]:
            block_ce_scores[idx] = s
            block_ce_cols[idx] = col

    for idx, s in block_ce_scores.items():
        blocks[idx]["_score_ce"] = round(s, 3)
        blocks[idx]["_best_col_ce"] = block_ce_cols[idx]

    return blocks


# ===========================================================================
# 关键词后备 (无模型环境)
# ===========================================================================

def keyword_score(blocks: list[dict]) -> list[dict]:
    """纯关键词评分 (不需要模型)"""
    compiled = {col: [re.compile(p, re.IGNORECASE) for p in pats]
                for col, pats in COLUMN_KEYWORDS.items()}

    for b in blocks:
        if b.get("_skip"):
            b["_score"] = 0.0; b["_top_columns"] = []; continue
        text = b["_text"]
        if not text: b["_score"] = 0.0; b["_top_columns"] = []; continue

        matched = []
        for col, patterns in compiled.items():
            for pat in patterns:
                if pat.search(text):
                    matched.append({"col": col, "sim": 1.0})
                    break

        b["_score"] = len(matched) / len(COLUMN_KEYWORDS)  # normalize to 0~1
        b["_top_columns"] = matched
        if b["_type"] == "table":
            b["_score"] = min(1.0, b["_score"] + 0.15)
    return blocks


# ===========================================================================
# 统一入口
# ===========================================================================

def score_blocks(blocks: list[dict], mode: str = "bi-encoder",
                 bi_model: str = "sentence-transformers/all-MiniLM-L6-v2",
                 cross_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
                 use_cross_encoder: bool = False, top_n: int = 50) -> list[dict]:
    """
    完整评分流程:
      mode = "bi-encoder"  → 规则过滤 + bi-encoder 语义评分
      mode = "keyword"     → 规则过滤 + 关键词评分 (不需要模型)
    """
    blocks = rule_filter(blocks)

    if mode == "keyword":
        blocks = keyword_score(blocks)
    else:
        blocks = bi_encoder_score(blocks, model_name=bi_model)
        if use_cross_encoder:
            blocks = cross_encoder_rerank(blocks, top_n=top_n, model_name=cross_model)

    return blocks


def filter_blocks(blocks: list[dict], min_score: float = 0.25) -> list[dict]:
    """过滤低分 block。bi-encoder 模式用 0.25，keyword 模式用 0.04"""
    return [b for b in blocks if b.get("_score", 0) >= min_score]


# ===========================================================================
# 输出
# ===========================================================================

def blocks_to_sections(blocks: list[dict], min_score: float = 0.25) -> dict:
    sections = {}
    for b in blocks:
        sec = b.get("_section", "unknown")
        if sec in ("references", "meta", "unknown"): continue
        if b.get("_score", 0) < min_score: continue
        if not b["_text"]: continue
        sections.setdefault(sec, []).append(b["_text"])
    return {sec: "\n\n".join(texts) for sec, texts in sections.items()}


def blocks_to_text(blocks: list[dict], min_score: float = 0.25) -> str:
    current_section = ""
    parts = []
    for b in blocks:
        if b.get("_score", 0) < min_score: continue
        sec = b.get("_section", "")
        if sec != current_section and b["_type"] == "title":
            current_section = sec
            parts.append(f"\n## {b['_text']}\n")
        elif b["_text"]:
            parts.append(b["_text"])
    return "\n\n".join(parts)


# ===========================================================================
# 统计报告
# ===========================================================================

def report(blocks: list[dict]) -> dict:
    total = len(blocks)
    skipped = [b for b in blocks if b.get("_skip")]
    scored = [b for b in blocks if not b.get("_skip")]
    relevant = [b for b in scored if b.get("_score", 0) >= 0.25]
    high = [b for b in scored if b.get("_score", 0) >= 0.5]

    sec_stats = {}
    for b in blocks:
        sec = b.get("_section", "?")
        sec_stats.setdefault(sec, {"blocks": 0, "relevant": 0, "chars": 0, "avg_score": []})
        sec_stats[sec]["blocks"] += 1
        if b.get("_score", 0) >= 0.25:
            sec_stats[sec]["relevant"] += 1
            sec_stats[sec]["chars"] += len(b.get("_text", ""))
        if not b.get("_skip"):
            sec_stats[sec]["avg_score"].append(b.get("_score", 0))

    for s in sec_stats.values():
        scores = s.pop("avg_score")
        s["avg_score"] = round(sum(scores) / len(scores), 3) if scores else 0

    # column hit count
    col_hits = {}
    for b in relevant:
        for tc in b.get("_top_columns", []):
            col = tc["col"] if isinstance(tc, dict) else tc
            col_hits[col] = col_hits.get(col, 0) + 1
    col_hits = dict(sorted(col_hits.items(), key=lambda x: -x[1]))

    skip_reasons = {}
    for b in skipped:
        r = b.get("_skip", "?"); skip_reasons[r] = skip_reasons.get(r, 0) + 1

    total_chars = sum(len(b.get("_text", "")) for b in blocks)
    relevant_chars = sum(len(b.get("_text", "")) for b in relevant)

    # score distribution
    all_scores = [b.get("_score", 0) for b in scored]
    dist = {}
    for threshold in [0.1, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6]:
        dist[f">={threshold}"] = sum(1 for s in all_scores if s >= threshold)

    return {
        "total_blocks": total,
        "skipped": len(skipped), "skip_reasons": skip_reasons,
        "scored": len(scored),
        "relevant_025": len(relevant), "high_050": len(high),
        "total_chars": total_chars, "relevant_chars": relevant_chars,
        "compression_pct": round((1 - relevant_chars / total_chars) * 100, 1) if total_chars else 0,
        "score_distribution": dist,
        "section_stats": sec_stats,
        "column_hits": col_hits,
    }


# ===========================================================================
# CLI
# ===========================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="MinerU 预处理 + 语义评分")
    sub = parser.add_subparsers(dest="cmd")

    p1 = sub.add_parser("score", help="评分统计")
    p1.add_argument("file")
    p1.add_argument("--mode", default="bi-encoder", choices=["bi-encoder", "keyword"])
    p1.add_argument("--cross-encoder", action="store_true", help="启用 cross-encoder 精排")
    p1.add_argument("--bi-model", default="sentence-transformers/all-MiniLM-L6-v2")

    p2 = sub.add_parser("dump", help="输出过滤后的文本")
    p2.add_argument("file")
    p2.add_argument("--mode", default="bi-encoder", choices=["bi-encoder", "keyword"])
    p2.add_argument("--min-score", type=float, default=None, help="过滤阈值 (bi-encoder:0.25, keyword:0.04)")
    p2.add_argument("-o", "--output", default=None)
    p2.add_argument("--bi-model", default="sentence-transformers/all-MiniLM-L6-v2")

    p3 = sub.add_parser("detail", help="逐 block 详情")
    p3.add_argument("file")
    p3.add_argument("--mode", default="bi-encoder", choices=["bi-encoder", "keyword"])
    p3.add_argument("--bi-model", default="sentence-transformers/all-MiniLM-L6-v2")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help(); return

    blocks = load_mineru(args.file)
    mode = getattr(args, "mode", "bi-encoder")
    bi_model = getattr(args, "bi_model", "sentence-transformers/all-MiniLM-L6-v2")
    use_ce = getattr(args, "cross_encoder", False)
    scored = score_blocks(blocks, mode=mode, bi_model=bi_model, use_cross_encoder=use_ce)

    if args.cmd == "score":
        r = report(scored)
        print(f"\n{'='*55}")
        print(f"  MinerU 预处理 ({mode}): {args.file}")
        print(f"{'='*55}")
        print(f"  总 block: {r['total_blocks']}")
        print(f"  跳过: {r['skipped']} {r['skip_reasons']}")
        print(f"  评分: {r['scored']}")
        print(f"  相关 (≥0.25): {r['relevant_025']}")
        print(f"  高相关 (≥0.50): {r['high_050']}")
        print(f"  压缩: {r['total_chars']} → {r['relevant_chars']} chars ({r['compression_pct']}%)")
        print(f"\n  分数分布: {r['score_distribution']}")
        print(f"\n  Section:")
        for sec, s in r["section_stats"].items():
            print(f"    {sec:15} {s['relevant']:>3}/{s['blocks']:>3} blocks, {s['chars']:>5} chars, avg={s['avg_score']:.3f}")
        print(f"\n  Column hits (top 15):")
        for col, count in list(r["column_hits"].items())[:15]:
            print(f"    {col:25} {count:>3}")

    elif args.cmd == "dump":
        ms = args.min_score or (0.25 if mode == "bi-encoder" else 0.04)
        text = blocks_to_text(scored, ms)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            total_chars = sum(len(b.get("_text","")) for b in scored)
            print(f"💾 {args.output} ({len(text)} chars, {round((1-len(text)/total_chars)*100)}% compressed)")
        else:
            print(text)

    elif args.cmd == "detail":
        for b in scored:
            skip = "⊘" if b.get("_skip") or b.get("_score",0) < 0.25 else " "
            txt = (b["_text"] or "(empty)")[:75].replace('\n',' ')
            score = b.get("_score", 0)
            ce = b.get("_score_ce")
            top = [tc["col"] for tc in b.get("_top_columns",[])[:3]] if isinstance(b.get("_top_columns",[{}])[0] if b.get("_top_columns") else {}, dict) else []
            cols_str = ",".join(top) if top else ""
            ce_str = f" CE={ce:.3f}" if ce is not None else ""
            print(f"  {skip} [{score:.3f}{ce_str}] p{b['_page']:>2} {b['_type']:>12} [{b.get('_section','?'):>13}] {txt}")
            if cols_str:
                print(f"          → {cols_str}")


if __name__ == "__main__":
    main()