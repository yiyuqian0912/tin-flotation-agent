#!/usr/bin/env python3
"""
query_index.py — Query FAISS index with ore parameters.
Applies synonym normalization to query. Ranks by combined score:

  final_score = similarity_weight × similarity + score_weight × normalized_recipe_score

Usage:
    python3 query_index.py --query '{"ore_type":"氧化锡矿","gangue":"石英","d80_um":"45"}'
    python3 query_index.py --query '...' --top-k 5 --score-weight 0.3
    python3 query_index.py --query '...' --similarity-weight 0.6 --score-weight 0.4

Requires: pip install faiss-cpu sentence-transformers
"""

import argparse
import json
import os
import pickle
import re
import sys

EMBED_MODEL = "all-MiniLM-L6-v2"
DEFAULT_INDEX_DIR = os.path.expanduser("~/.openclaw/workspace/database/faiss_index")
DEFAULT_SYNONYMS = os.path.expanduser("~/.openclaw/workspace/database/synonyms.json")

# Default ranking weights
DEFAULT_SIM_WEIGHT = 0.7
DEFAULT_SCORE_WEIGHT = 0.3


def setup_hf_mirror(mirror_url=""):
    if mirror_url:
        os.environ["HF_ENDPOINT"] = mirror_url


def load_synonyms(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    flat = {}
    for cat, mapping in raw.items():
        if cat.startswith("_"): continue
        if isinstance(mapping, dict):
            for var, std in mapping.items():
                flat[var.lower().strip()] = std.lower().strip()
    return flat


def normalize_text(text, syn_map):
    if not text or not syn_map:
        return text
    result = text.lower()
    for var, std in sorted(syn_map.items(), key=lambda x: len(x[0]), reverse=True):
        if var in result:
            result = result.replace(var, std)
    return result


def load_index(index_dir):
    try:
        import faiss
    except ImportError:
        print(json.dumps({"error": "faiss-cpu not installed. Run: pip3 install faiss-cpu"}))
        sys.exit(1)

    index_path = os.path.join(index_dir, "recipes.index")
    meta_path = os.path.join(index_dir, "metadata.pkl")

    if not os.path.exists(index_path):
        print(json.dumps({"error": f"Index not found: {index_path}. Run build_index.py first."}))
        sys.exit(1)

    index = faiss.read_index(index_path)
    with open(meta_path, "rb") as f:
        metadata = pickle.load(f)
    return index, metadata


def ore_query_to_text(params, syn_map):
    """Convert user ore params to embedding text with synonym normalization."""
    parts = []

    mappings = {
        "ore_type":         "ore type",
        "ore_class":        "ore class",
        "ore_family":       "ore family",
        "gangue":           "gangue",
        "gangue_primary":   "gangue",
        "gangue_main":      "gangue",
        "gangue_secondary": "gangue",
        "associated":       "associated",
        "valuable_mineral": "valuable mineral",
    }

    for key, label in mappings.items():
        val = params.get(key, "")
        if val:
            parts.append(f"{label}: {normalize_text(str(val), syn_map)}")

    # List-form gangue
    gangue_list = params.get("gangue_minerals", [])
    if isinstance(gangue_list, list):
        for g in gangue_list:
            name = g.get("name", g) if isinstance(g, dict) else str(g)
            if name:
                parts.append(f"gangue: {normalize_text(name, syn_map)}")

    # Numeric fields
    for key, (label, unit) in {
        "d80_um": ("D80", " um"),
        "d50_um": ("D50", " um"),
        "sno2_pct": ("head assay sn", "%"),
        "sno2_content_pct": ("head assay sn", "%"),
        "sn_pct": ("head assay sn", "%"),
    }.items():
        val = params.get(key, "")
        if val:
            parts.append(f"{label}: {val}{unit}")

    # Free text
    for key in ["slime_note", "fines_note", "ore_features", "size_fraction"]:
        val = params.get(key, "")
        if val:
            parts.append(normalize_text(val, syn_map))

    # Pain point
    pain = params.get("pain_point", "")
    if pain:
        parts.append(f"target: {normalize_text(pain, syn_map)}")

    return "; ".join(parts) if parts else "cassiterite ore"


def format_recommendation(meta, rank, final_score, similarity, score_contribution):
    recipe = meta.get("recipe_meta", {})

    reagent_by_role = {}
    for r in recipe.get("reagents", []):
        role = r.get("role", "unknown")
        if role not in reagent_by_role:
            reagent_by_role[role] = []
        reagent_by_role[role].append(r)
    for role in reagent_by_role:
        reagent_by_role[role].sort(key=lambda x: x.get("order") or 999)

    return {
        "rank": rank,
        "final_score": round(final_score, 4),
        "similarity": round(float(similarity), 4),
        "recipe_score": recipe.get("recommendation_score"),
        "score_contribution": round(score_contribution, 4),
        "paper_title": meta.get("paper_title", ""),
        "paper_year": meta.get("paper_year", ""),
        "paper_url": meta.get("paper_url", ""),
        "filename": meta.get("filename", ""),
        "experiment_id": meta.get("experiment_id", ""),
        "experiment_name": meta.get("experiment_name", ""),
        "ore_matched_on": meta.get("ore_embed_text", ""),
        "grade_rating": recipe.get("grade_rating", ""),
        "is_promising": recipe.get("is_promising", False),
        "is_reported_best": recipe.get("is_reported_best", False),
        "reagent_scheme": reagent_by_role,
        "products": recipe.get("products", []),
        "stage": recipe.get("stage", ""),
        "concentrate_type": recipe.get("concentrate_type", ""),
        "flotation_conditions": recipe.get("flotation_conditions", {}),
    }


def query(index, metadata, query_text, top_k=5,
          sim_weight=DEFAULT_SIM_WEIGHT, score_weight=DEFAULT_SCORE_WEIGHT):
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBED_MODEL)
    query_vec = model.encode([query_text], normalize_embeddings=True)

    # Fetch more candidates than needed for re-ranking
    fetch_k = min(top_k * 5, index.ntotal)
    distances, indices = index.search(query_vec, fetch_k)

    # Collect all recipe scores for normalization
    raw_scores = []
    for idx in indices[0]:
        if 0 <= idx < len(metadata):
            s = metadata[idx].get("recommendation_score")
            if s is not None:
                raw_scores.append(s)

    # Normalize recipe scores to [0, 1]
    if raw_scores:
        s_min = min(raw_scores)
        s_max = max(raw_scores)
        s_range = s_max - s_min if s_max > s_min else 1.0
    else:
        s_min, s_range = 0, 1.0

    # Compute final_score = sim_weight × similarity + score_weight × normalized_score
    candidates = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(metadata):
            continue

        meta = metadata[idx]
        sim = float(dist)  # cosine similarity [0, 1]

        rec_score = meta.get("recommendation_score")
        if rec_score is not None:
            norm_score = (rec_score - s_min) / s_range  # [0, 1]
            score_contrib = score_weight * norm_score
        else:
            norm_score = 0.5  # neutral if missing
            score_contrib = score_weight * norm_score

        final = sim_weight * sim + score_contrib

        candidates.append({
            "meta": meta,
            "similarity": sim,
            "score_contribution": score_contrib,
            "final_score": final,
        })

    # Sort by final_score descending
    candidates.sort(key=lambda x: x["final_score"], reverse=True)

    results = []
    for i, c in enumerate(candidates[:top_k]):
        results.append(format_recommendation(
            c["meta"], i + 1, c["final_score"], c["similarity"], c["score_contribution"]
        ))

    return results


def main():
    parser = argparse.ArgumentParser(description="Query FAISS ore index → weighted recipe recommendations")
    parser.add_argument("--query", required=True, help="Ore params JSON")
    parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    parser.add_argument("--synonyms", default=DEFAULT_SYNONYMS, help="Synonym map JSON")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--similarity-weight", type=float, default=DEFAULT_SIM_WEIGHT,
                        help=f"Weight for vector similarity (default {DEFAULT_SIM_WEIGHT})")
    parser.add_argument("--score-weight", type=float, default=DEFAULT_SCORE_WEIGHT,
                        help=f"Weight for recipe score (default {DEFAULT_SCORE_WEIGHT})")
    parser.add_argument("--mirror", action="store_true")
    parser.add_argument("--mirror-url", default="")
    args = parser.parse_args()

    if args.mirror_url:
        setup_hf_mirror(args.mirror_url)
    elif args.mirror:
        setup_hf_mirror("https://hf-mirror.com")

    try:
        ore_params = json.loads(args.query)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        sys.exit(1)

    syn_map = load_synonyms(args.synonyms)
    query_text = ore_query_to_text(ore_params, syn_map)
    print(f"[INFO] Query (normalized): {query_text}", file=sys.stderr)

    index, metadata = load_index(args.index_dir)
    print(f"[INFO] Index: {index.ntotal} experiments", file=sys.stderr)

    results = query(index, metadata, query_text, args.top_k,
                    args.similarity_weight, args.score_weight)

    print(json.dumps({
        "query_params": ore_params,
        "query_text_normalized": query_text,
        "ranking_weights": {
            "similarity": args.similarity_weight,
            "recipe_score": args.score_weight,
        },
        "index_size": index.ntotal,
        "results_returned": len(results),
        "recommendations": results,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
