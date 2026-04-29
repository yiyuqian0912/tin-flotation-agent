#!/usr/bin/env python3
"""
build_index.py — Build FAISS index from recipe JSONs.
Embeds ore characteristics (with synonym normalization).
Stores recipe score alongside for weighted ranking.

Usage:
    python3 build_index.py --recipes-dir ~/.openclaw/workspace/database/recipes --rebuild
    python3 build_index.py --recipes-dir ... --mirror --rebuild
    python3 build_index.py --recipes-dir ... --synonyms path/to/synonyms.json

Requires: pip install faiss-cpu sentence-transformers
"""

import argparse
import json
import os
import pickle
import re
import sys

EMBED_MODEL = "all-MiniLM-L6-v2"
INDEX_DIR_NAME = "faiss_index"
DEFAULT_SYNONYMS = os.path.expanduser("~/.openclaw/workspace/database/synonyms.json")


def setup_hf_mirror(mirror_url=""):
    if mirror_url:
        os.environ["HF_ENDPOINT"] = mirror_url
        print(f"[INFO] HF mirror: {mirror_url}", file=sys.stderr)
    elif os.environ.get("HF_ENDPOINT"):
        print(f"[INFO] HF mirror from env: {os.environ['HF_ENDPOINT']}", file=sys.stderr)


# ═══════════════════════════════════════════════════
# V2 value extraction
# ═══════════════════════════════════════════════════

def _v(obj, default=None):
    if isinstance(obj, dict) and "value" in obj:
        val = obj["value"]
        return val if val is not None else default
    return obj if obj is not None else default

def _s(obj, default=""):
    val = _v(obj)
    return str(val).strip() if val is not None else default

def _n(obj, default=None):
    val = _v(obj)
    if val is None: return default
    if isinstance(val, (int, float)): return float(val)
    m = re.search(r'(\d+\.?\d*)', str(val))
    return float(m.group(1)) if m else default


# ═══════════════════════════════════════════════════
# Synonym mapping
# ═══════════════════════════════════════════════════

def load_synonyms(path):
    """Load synonym map. Returns flat dict: variant → standard."""
    if not os.path.exists(path):
        print(f"[WARN] Synonyms file not found: {path}", file=sys.stderr)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    flat = {}
    for category, mapping in raw.items():
        if category.startswith("_"):
            continue
        if isinstance(mapping, dict):
            for variant, standard in mapping.items():
                flat[variant.lower().strip()] = standard.lower().strip()
    return flat


def normalize_text(text, syn_map):
    """Apply synonym mapping to text. Replaces all known variants with standard form."""
    if not text or not syn_map:
        return text

    result = text.lower()

    # Sort by length descending so longer matches take priority
    for variant, standard in sorted(syn_map.items(), key=lambda x: len(x[0]), reverse=True):
        if variant in result:
            result = result.replace(variant, standard)

    return result


# ═══════════════════════════════════════════════════
# Ore → embedding text
# ═══════════════════════════════════════════════════

def ore_to_embed_text(ore_profile, experiment=None, syn_map=None):
    """Convert ore characteristics to text, with synonym normalization."""
    syn = syn_map or {}
    parts = []

    def _add(label, val):
        if val:
            parts.append(f"{label}: {normalize_text(str(val), syn)}")

    _add("ore type", _s(ore_profile.get("ore_type", "")))
    _add("ore class", _s(ore_profile.get("ore_type_standard", "")))
    _add("deposit", _s(ore_profile.get("deposit_name", "")))
    _add("region", _s(ore_profile.get("deposit_region", "")))

    for vm in ore_profile.get("valuable_minerals", []):
        name = _s(vm.get("name", ""))
        pct = _n(vm.get("composition_pct"))
        if name:
            norm = normalize_text(name, syn)
            s = f"valuable mineral: {norm}"
            if pct is not None: s += f" {pct}%"
            parts.append(s)

    for gm in ore_profile.get("gangue_minerals", []):
        name = _s(gm.get("name", ""))
        pct = _n(gm.get("composition_pct"))
        role = _s(gm.get("role", ""))
        if name:
            norm = normalize_text(name, syn)
            s = f"gangue: {norm}"
            if pct is not None: s += f" {pct}%"
            if role: s += f" ({role})"
            parts.append(s)

    for am in ore_profile.get("associated_minerals", []):
        name = _s(am.get("name", ""))
        if name:
            parts.append(f"associated: {normalize_text(name, syn)}")

    for ha in ore_profile.get("head_assays", []):
        analyte = _s(ha.get("analyte", ""))
        value = _n(ha.get("value_pct"))
        unit = _s(ha.get("unit", "%"))
        if analyte and value is not None:
            parts.append(f"head assay {normalize_text(analyte, syn)}: {value}{unit}")

    ps = ore_profile.get("particle_size", {})
    d80 = _n(ps.get("d80_um"))
    d50 = _n(ps.get("d50_um"))
    frac = _s(ps.get("size_fraction_text", ""))
    if d80 is not None: parts.append(f"D80: {d80} um")
    if d50 is not None: parts.append(f"D50: {d50} um")
    if frac: parts.append(f"size fraction: {frac}")

    lib = _n(ore_profile.get("liberation_degree_pct"))
    if lib is not None: parts.append(f"liberation: {lib}%")

    slime = _s(ore_profile.get("slime_or_fines_note", ""))
    if slime: parts.append(f"fines: {normalize_text(slime, syn)}")

    feat = _s(ore_profile.get("ore_features_text", ""))
    if feat: parts.append(normalize_text(feat, syn))

    if experiment:
        ctx = experiment.get("ore_context_override",
              experiment.get("sample_override", {}))
        for field in ["sample_type", "pretreatment", "size_fraction_used"]:
            val = _s(ctx.get(field, ""))
            if val: parts.append(normalize_text(val, syn))

    return "; ".join(parts) if parts else "cassiterite ore"


# ═══════════════════════════════════════════════════
# Extract recipe + score for metadata
# ═══════════════════════════════════════════════════

def extract_metadata(paper, ore_profile, exp):
    """Extract everything needed for recommendation display."""
    scheme = exp.get("reagent_scheme", [])
    results = exp.get("results", {})
    signals = exp.get("recommendation_signals", {})
    cond = exp.get("flotation_conditions", {})

    reagents = []
    for r in scheme:
        reagents.append({
            "name": _s(r.get("name_standard", "")) or _s(r.get("name", "")),
            "role": _s(r.get("role", "")),
            "dosage_gpt": _n(r.get("dosage_gpt")),
            "dosage_original": _s(r.get("dosage_original_text", "")),
            "target_ph": _n(r.get("target_ph")),
            "conditioning_min": _n(r.get("conditioning_min")),
            "order": _n(r.get("order")),
            "target_mineral": _s(r.get("target_mineral", "")),
            "environmental_warning": _v(r.get("environmental_warning"), False),
        })

    products = []
    for p in results.get("products", []):
        products.append({
            "analyte": _s(p.get("analyte", "")),
            "basis": _s(p.get("basis", "")),
            "recovery_pct": _n(p.get("recovery_pct")),
            "grade_value": _n(p.get("grade_value")),
            "grade_unit": _s(p.get("grade_unit", "%")),
            "enrichment_ratio": _n(p.get("enrichment_ratio")),
        })

    # Score extraction
    rec_score = _n(signals.get("recommendation_score"))
    grade_rating = _s(signals.get("grade_rating", ""))
    is_promising = _v(signals.get("is_promising"), False)
    is_reported_best = _v(results.get("is_reported_best"), False)

    return {
        "reagents": reagents,
        "products": products,
        "stage": _s(results.get("stage", "")),
        "concentrate_type": _s(results.get("concentrate_type", "")),
        "flotation_conditions": {
            "cell_type": _s(cond.get("cell_type", "")),
            "pulp_density_pct": _n(cond.get("pulp_density_pct")),
            "temperature_c": _n(cond.get("temperature_c")),
            "flotation_time_min": _n(cond.get("flotation_time_min")),
        },
        "recommendation_score": rec_score,
        "grade_rating": grade_rating,
        "is_promising": is_promising,
        "is_reported_best": is_reported_best,
        "recommendation_signals": {
            k: _v(v) for k, v in signals.items()
            if k not in ("grade_breakdown", "grade_deductions")  # skip large nested data
        },
    }


# ═══════════════════════════════════════════════════
# Load + Build
# ═══════════════════════════════════════════════════

def load_recipes(recipes_dir, syn_map):
    records = []
    for filename in sorted(os.listdir(recipes_dir)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(recipes_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] Skip {filename}: {e}", file=sys.stderr)
            continue

        paper_info = data.get("paper", {})
        ore_profile = data.get("ore_profile", {})

        # v3 flat: file IS the experiment (has reagent_scheme at root)
        # v2 nested: file has experiments[] array
        if "reagent_scheme" in data:
            experiments = [data]
        elif "experiments" in data:
            experiments = data.get("experiments", [])
        else:
            continue

        for exp in experiments:
            ore_text = ore_to_embed_text(ore_profile, exp, syn_map)
            meta = extract_metadata(paper_info, ore_profile, exp)

            records.append({
                "ore_embed_text": ore_text,
                "recipe_meta": meta,
                "filename": filename,
                "paper_title": _s(paper_info.get("title", "")),
                "paper_year": _s(paper_info.get("year", "")),
                "paper_url": _s(paper_info.get("url", "")),
                "experiment_id": _s(exp.get("experiment_id", "")),
                "experiment_name": _s(exp.get("experiment_name", "")),
                "recommendation_score": meta["recommendation_score"],
            })
    return records


def build_index(records, index_dir):
    try:
        import faiss
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        print(json.dumps({"error": f"Missing: {e}. Run: pip3 install faiss-cpu sentence-transformers"}))
        sys.exit(1)

    print(f"[INFO] Loading model: {EMBED_MODEL}", file=sys.stderr)
    model = SentenceTransformer(EMBED_MODEL)

    texts = [r["ore_embed_text"] for r in records]
    print(f"[INFO] Embedding {len(texts)} ore profiles...", file=sys.stderr)
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)

    os.makedirs(index_dir, exist_ok=True)
    faiss.write_index(index, os.path.join(index_dir, "recipes.index"))

    metadata = []
    for r in records:
        metadata.append({k: v for k, v in r.items() if k != "ore_embed_text"})
        metadata[-1]["ore_embed_text"] = r["ore_embed_text"]

    with open(os.path.join(index_dir, "metadata.pkl"), "wb") as f:
        pickle.dump(metadata, f)

    scored_count = sum(1 for r in records if r["recommendation_score"] is not None)

    print(json.dumps({
        "status": "success",
        "index_dir": index_dir,
        "total_experiments": len(records),
        "experiments_with_score": scored_count,
        "embedding_dim": dim,
        "embedding_content": "ore characteristics only (synonym-normalized)",
        "model": EMBED_MODEL,
        "files_processed": len(set(r["filename"] for r in records)),
        "sample_ore_texts": texts[:3],
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Build FAISS index (ore-only, synonym-normalized)")
    parser.add_argument("--recipes-dir", required=True)
    parser.add_argument("--index-dir", default="")
    parser.add_argument("--synonyms", default=DEFAULT_SYNONYMS, help="Synonym map JSON")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--mirror", action="store_true")
    parser.add_argument("--mirror-url", default="")
    args = parser.parse_args()

    if args.mirror_url:
        setup_hf_mirror(args.mirror_url)
    elif args.mirror:
        setup_hf_mirror("https://hf-mirror.com")

    if not os.path.isdir(args.recipes_dir):
        print(json.dumps({"error": f"Not found: {args.recipes_dir}"}))
        sys.exit(1)

    index_dir = args.index_dir or os.path.join(os.path.dirname(args.recipes_dir), INDEX_DIR_NAME)

    if os.path.exists(os.path.join(index_dir, "recipes.index")) and not args.rebuild:
        print(json.dumps({"status": "index_exists", "index_dir": index_dir}))
        sys.exit(0)

    syn_map = load_synonyms(args.synonyms)
    print(f"[INFO] Loaded {len(syn_map)} synonym mappings", file=sys.stderr)

    records = load_recipes(args.recipes_dir, syn_map)
    if not records:
        print(json.dumps({"error": "No recipes found"}))
        sys.exit(1)

    build_index(records, index_dir)


if __name__ == "__main__":
    main()
