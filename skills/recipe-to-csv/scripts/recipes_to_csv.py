#!/usr/bin/env python3
"""
recipes_to_csv.py — Convert v2 template recipe JSONs to ML-ready CSV.
Generates TWO files:
  1. recipe_cases_raw.csv       — full data from literature
  2. recipe_recommendation_train.csv — clean training table for recommendation model

Usage:
    python3 recipes_to_csv.py --dir ~/.openclaw/workspace/database/recipes --output-dir ~/.openclaw/workspace/database/datasets
    python3 recipes_to_csv.py --file zhang_2024.json --output-dir ./datasets
    python3 recipes_to_csv.py --dir recipes/ --output-dir ./datasets --append

Requires: no external dependencies (pure stdlib)
"""

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import defaultdict


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

def _n_str(obj):
    """Numeric as string for CSV, empty if None."""
    val = _n(obj)
    return str(val) if val is not None else ""

def _src(obj):
    if isinstance(obj, dict) and "source" in obj:
        return obj.get("source", "")
    return ""


# ═══════════════════════════════════════════════════
# Reagent helpers
# ═══════════════════════════════════════════════════

def _get_by_role(scheme, role, index=0):
    matches = [r for r in scheme if _s(r.get("role", "")).lower() == role.lower()]
    return matches[index] if index < len(matches) else {}

def _reagent_name(r):
    return _s(r.get("name_standard", "")) or _s(r.get("name", ""))

def _dosage_split(r):
    """Return (value_str, unit_str) from dosage fields."""
    gpt = _n(r.get("dosage_gpt"))
    orig = _s(r.get("dosage_original_text", ""))
    if gpt is not None:
        return str(gpt), "g/t"
    if orig:
        m = re.match(r'([\d.]+)\s*(.+)', orig)
        if m:
            return m.group(1), m.group(2).strip()
        return orig, ""
    return "", ""


# ═══════════════════════════════════════════════════
# Ore flag computation
# ═══════════════════════════════════════════════════

def compute_ore_flags(ore):
    """Derive boolean flags from ore profile."""
    gangue_list = ore.get("gangue_minerals", [])
    assoc_list = ore.get("associated_minerals", [])
    ps = ore.get("particle_size", {})
    d80 = _n(ps.get("d80_um"))
    ore_type = _s(ore.get("ore_type", "")) + " " + _s(ore.get("ore_type_standard", ""))
    slime = _s(ore.get("slime_or_fines_note", ""))
    feat = _s(ore.get("ore_features_text", ""))
    all_text = (ore_type + " " + feat + " " + slime).lower()

    gangue_names = " ".join(_s(g.get("name", "")) for g in gangue_list).lower()
    assoc_names = " ".join(_s(a.get("name", "")) for a in assoc_list).lower()

    carbonate_kw = ["calcite", "dolomite", "siderite", "carbonate", "方解石"]
    silicate_kw = ["quartz", "feldspar", "silicate", "mica", "石英", "长石"]
    sulfide_kw = ["pyrite", "arsenopyrite", "chalcopyrite", "sulfide", "硫化"]

    flags = {
        "fine_grained_flag": "1" if (d80 is not None and d80 <= 38) or "fine" in all_text or "细粒" in all_text else "0",
        "ultrafine_flag": "1" if (d80 is not None and d80 <= 20) or "ultrafine" in all_text or "超细" in all_text else "0",
        "carbonate_gangue_flag": "1" if any(k in gangue_names for k in carbonate_kw) else "0",
        "silicate_gangue_flag": "1" if any(k in gangue_names for k in silicate_kw) else "0",
        "sulfide_associated_flag": "1" if any(k in assoc_names + " " + gangue_names for k in sulfide_kw) else "0",
        "oxide_ore_flag": "1" if "oxide" in all_text or "氧化" in all_text else "0",
    }
    return flags


# ═══════════════════════════════════════════════════
# Result extraction: valuable vs gangue
# ═══════════════════════════════════════════════════

SN_KEYWORDS = ["sn", "sno2", "cassiterite", "tin", "锡"]
GANGUE_ANALYTES = ["sio2", "cao", "fe", "fe2o3", "al2o3", "s", "as", "quartz", "calcite"]

def _find_valuable_product(products):
    """Find the target valuable mineral product."""
    for p in products:
        analyte = _s(p.get("analyte", "")).lower()
        if any(k in analyte for k in SN_KEYWORDS):
            return p
    # Fallback: first product
    return products[0] if products else {}

def _find_gangue_product(products):
    """Find gangue mineral product (if reported)."""
    for p in products:
        analyte = _s(p.get("analyte", "")).lower()
        basis = _s(p.get("basis", "")).lower()
        if basis == "impurity" or any(k in analyte for k in GANGUE_ANALYTES):
            return p
    return {}


# ═══════════════════════════════════════════════════
# Score computation
# ═══════════════════════════════════════════════════

def compute_scores(valuable, gangue):
    """Compute selectivity_score and recipe_score."""
    v_rec = _n(valuable.get("recovery_pct"))
    v_grade = _n(valuable.get("grade_value"))
    g_rec = _n(gangue.get("recovery_pct")) if gangue else None

    selectivity = ""
    if v_rec is not None and g_rec is not None:
        selectivity = str(round(v_rec - g_rec, 2))

    # recipe_score = 0.45 * recovery + 0.35 * grade - 0.20 * gangue_recovery
    recipe_score = ""
    if v_rec is not None:
        score = 0.45 * v_rec
        if v_grade is not None:
            score += 0.35 * v_grade
        if g_rec is not None:
            score -= 0.20 * g_rec
        recipe_score = str(round(score, 2))

    return selectivity, recipe_score


# ═══════════════════════════════════════════════════
# Column definitions
# ═══════════════════════════════════════════════════

TRAIN_COLUMNS = [
    # ── 标识 (4) ──
    "case_id", "paper_id", "experiment_id", "experiment_name",

    # ── 矿石输入特征 (29) ──
    "ore_family", "ore_type_standard", "sample_nature", "deposit_region",
    "valuable_mineral_main", "valuable_mineral_main_pct",
    "valuable_mineral_secondary", "valuable_mineral_secondary_pct",
    "gangue_main", "gangue_main_pct",
    "gangue_secondary", "gangue_secondary_pct",
    "associated_minerals_text",
    "head_assay_target_pct", "head_assay_gangue_pct",
    "target_element", "target_species_basis",
    "d50_um", "d80_um", "size_fraction_min_um", "size_fraction_max_um", "liberation_pct",
    "fine_grained_flag", "ultrafine_flag",
    "carbonate_gangue_flag", "silicate_gangue_flag",
    "sulfide_associated_flag", "oxide_ore_flag",
    "ore_features_text",

    # ── 配方 (30) ──
    "collector_name", "collector_dosage_value", "collector_dosage_unit",
    "collector_2_name", "collector_2_dosage_value", "collector_2_dosage_unit",
    "depressant_name", "depressant_dosage_value", "depressant_dosage_unit", "depressant_target",
    "depressant_2_name", "depressant_2_dosage_value", "depressant_2_dosage_unit", "depressant_2_target",
    "activator_name", "activator_dosage_value", "activator_dosage_unit", "activator_target",
    "dispersant_name", "dispersant_dosage_value", "dispersant_dosage_unit",
    "flocculant_name", "flocculant_dosage_value", "flocculant_dosage_unit",
    "frother_name", "frother_dosage_value", "frother_dosage_unit",
    "sulfidizer_name", "sulfidizer_dosage_value", "sulfidizer_dosage_unit",

    # ── 工艺条件 (18) ──
    "ph_target", "ph_min", "ph_max",
    "temperature_c", "pulp_density_pct", "cell_volume_ml", "impeller_speed_rpm",
    "conditioning_total_min", "collector_conditioning_min",
    "depressant_conditioning_min", "activator_conditioning_min",
    "dispersant_conditioning_min", "frother_conditioning_min",
    "flotation_time_min", "airflow_rate",
    "pretreatment", "stage", "concentrate_type",

    # ── 结果 (10) ──
    "valuable_analyte", "valuable_recovery_pct", "valuable_grade_value", "valuable_grade_unit",
    "gangue_analyte", "gangue_recovery_pct", "gangue_grade_value", "gangue_grade_unit",
    "enrichment_ratio", "mass_pull_pct",

    # ── 推荐标签 (5) ──
    "selectivity_score", "recipe_score",
    "recipe_rank_in_experiment_group", "is_best_in_experiment_group", "is_promising",

    # ── 质量溯源 (4) ──
    "value_source", "extraction_confidence", "reported_as_best", "notes",
]


# ═══════════════════════════════════════════════════
# Build one row
# ═══════════════════════════════════════════════════

def experiment_to_row(paper, ore, exp, case_id):
    scheme = exp.get("reagent_scheme", [])
    results = exp.get("results", {})
    products = results.get("products", [])
    signals = exp.get("recommendation_signals", {})
    context = exp.get("ore_context_override", exp.get("sample_override", {}))
    cond = exp.get("flotation_conditions", {})
    retrieval = exp.get("retrieval", {})
    ps = ore.get("particle_size", {})

    # Reagents
    ph_mod = _get_by_role(scheme, "ph modifier")
    c1 = _get_by_role(scheme, "collector", 0)
    c2 = _get_by_role(scheme, "collector", 1)
    d1 = _get_by_role(scheme, "depressant", 0)
    d2 = _get_by_role(scheme, "depressant", 1)
    a1 = _get_by_role(scheme, "activator", 0)
    disp = _get_by_role(scheme, "dispersant", 0)
    floc = _get_by_role(scheme, "flocculant", 0)
    fr = _get_by_role(scheme, "frother", 0)
    sulf = _get_by_role(scheme, "sulfidizer", 0)

    c1_dv, c1_du = _dosage_split(c1)
    c2_dv, c2_du = _dosage_split(c2)
    d1_dv, d1_du = _dosage_split(d1)
    d2_dv, d2_du = _dosage_split(d2)
    a1_dv, a1_du = _dosage_split(a1)
    disp_dv, disp_du = _dosage_split(disp)
    floc_dv, floc_du = _dosage_split(floc)
    fr_dv, fr_du = _dosage_split(fr)
    sulf_dv, sulf_du = _dosage_split(sulf)

    # Ore arrays
    gangue_list = ore.get("gangue_minerals", [])
    valuable_list = ore.get("valuable_minerals", [])
    assoc_list = ore.get("associated_minerals", [])
    assay_list = ore.get("head_assays", [])

    # Head assays
    target_assay, gangue_assay = "", ""
    for ha in assay_list:
        analyte = _s(ha.get("analyte", "")).lower()
        if any(k in analyte for k in SN_KEYWORDS):
            target_assay = _n_str(ha.get("value_pct"))
        elif any(k in analyte for k in GANGUE_ANALYTES):
            gangue_assay = _n_str(ha.get("value_pct"))

    # Ore family derivation
    ore_type_std = _s(ore.get("ore_type_standard", "")) or _s(ore.get("ore_type", ""))
    ore_family = "tin"  # default for this system
    vm_main = _s(valuable_list[0].get("name", "")) if valuable_list else ""
    if any(k in vm_main.lower() for k in ["scheelite", "wolframite"]):
        ore_family = "tungsten"
    elif any(k in vm_main.lower() for k in ["chalcopyrite", "copper"]):
        ore_family = "copper"

    # Sample nature
    sample = _s(context.get("sample_type", ""))
    sample_map = {
        "pure": "single_mineral", "single": "single_mineral",
        "artificial": "artificial_mixture", "mixture": "artificial_mixture",
        "bulk": "real_ore", "real": "real_ore",
    }
    sample_nature = sample
    for k, v in sample_map.items():
        if k in sample.lower():
            sample_nature = v
            break

    # Target element
    valuable = _find_valuable_product(products)
    gangue_prod = _find_gangue_product(products)
    target_element = _s(valuable.get("analyte", ""))
    target_basis = _s(valuable.get("basis", ""))

    # Size fraction parsing
    frac = _s(ps.get("size_fraction_text", ""))
    size_min, size_max = "", ""
    m = re.search(r'(\d+)\s*[-–]\s*(\d+)\s*[μu]m', frac)
    if m:
        size_min, size_max = m.group(1), m.group(2)

    # Flags
    flags = compute_ore_flags(ore)

    # Associated minerals as text
    assoc_text = ", ".join(_s(a.get("name", "")) for a in assoc_list if _s(a.get("name", "")))

    # Scores
    selectivity, recipe_score = compute_scores(valuable, gangue_prod)

    # Quality/source
    data_loc = _s(exp.get("data_location", ""))
    value_source = "table" if "table" in data_loc.lower() else "text" if data_loc else ""
    confidence = _s(signals.get("extraction_confidence", ""))
    if not confidence:
        # Derive from grade_rating
        rating = _s(signals.get("grade_rating", "")).lower()
        if rating in ("a", "b"):
            confidence = "high"
        elif rating == "c":
            confidence = "medium"
        elif rating:
            confidence = "low"

    reported_best = "1" if _v(results.get("is_reported_best"), False) else "0"

    row = {
        "case_id": case_id,
        "paper_id": _s(paper.get("paper_id", "")),
        "experiment_id": _s(exp.get("experiment_id", "")),
        "experiment_name": _s(exp.get("experiment_name", "")),

        "ore_family": ore_family,
        "ore_type_standard": ore_type_std,
        "sample_nature": sample_nature,
        "deposit_region": _s(ore.get("deposit_region", "")),

        "valuable_mineral_main": _s(valuable_list[0].get("name", "")) if valuable_list else "",
        "valuable_mineral_main_pct": _n_str(valuable_list[0].get("composition_pct")) if valuable_list else "",
        "valuable_mineral_secondary": _s(valuable_list[1].get("name", "")) if len(valuable_list) > 1 else "",
        "valuable_mineral_secondary_pct": _n_str(valuable_list[1].get("composition_pct")) if len(valuable_list) > 1 else "",

        "gangue_main": _s(gangue_list[0].get("name", "")) if gangue_list else "",
        "gangue_main_pct": _n_str(gangue_list[0].get("composition_pct")) if gangue_list else "",
        "gangue_secondary": _s(gangue_list[1].get("name", "")) if len(gangue_list) > 1 else "",
        "gangue_secondary_pct": _n_str(gangue_list[1].get("composition_pct")) if len(gangue_list) > 1 else "",

        "associated_minerals_text": assoc_text,
        "head_assay_target_pct": target_assay,
        "head_assay_gangue_pct": gangue_assay,
        "target_element": target_element,
        "target_species_basis": target_basis,

        "d50_um": _n_str(ps.get("d50_um")),
        "d80_um": _n_str(ps.get("d80_um")),
        "size_fraction_min_um": size_min,
        "size_fraction_max_um": size_max,
        "liberation_pct": _n_str(ore.get("liberation_degree_pct")),
        **flags,
        "ore_features_text": _s(ore.get("ore_features_text", "")),

        "collector_name": _reagent_name(c1), "collector_dosage_value": c1_dv, "collector_dosage_unit": c1_du,
        "collector_2_name": _reagent_name(c2), "collector_2_dosage_value": c2_dv, "collector_2_dosage_unit": c2_du,
        "depressant_name": _reagent_name(d1), "depressant_dosage_value": d1_dv, "depressant_dosage_unit": d1_du,
        "depressant_target": _s(d1.get("target_mineral", "")),
        "depressant_2_name": _reagent_name(d2), "depressant_2_dosage_value": d2_dv, "depressant_2_dosage_unit": d2_du,
        "depressant_2_target": _s(d2.get("target_mineral", "")),
        "activator_name": _reagent_name(a1), "activator_dosage_value": a1_dv, "activator_dosage_unit": a1_du,
        "activator_target": _s(a1.get("target_mineral", "")),
        "dispersant_name": _reagent_name(disp), "dispersant_dosage_value": disp_dv, "dispersant_dosage_unit": disp_du,
        "flocculant_name": _reagent_name(floc), "flocculant_dosage_value": floc_dv, "flocculant_dosage_unit": floc_du,
        "frother_name": _reagent_name(fr), "frother_dosage_value": fr_dv, "frother_dosage_unit": fr_du,
        "sulfidizer_name": _reagent_name(sulf), "sulfidizer_dosage_value": sulf_dv, "sulfidizer_dosage_unit": sulf_du,

        "ph_target": _n_str(ph_mod.get("target_ph")),
        "ph_min": _n_str(ph_mod.get("ph_range_min")),
        "ph_max": _n_str(ph_mod.get("ph_range_max")),
        "temperature_c": _n_str(cond.get("temperature_c")),
        "pulp_density_pct": _n_str(cond.get("pulp_density_pct")),
        "cell_volume_ml": _n_str(cond.get("cell_volume_ml")),
        "impeller_speed_rpm": _n_str(cond.get("impeller_speed_rpm")),
        "conditioning_total_min": _n_str(cond.get("conditioning_total_min")),
        "collector_conditioning_min": _n_str(c1.get("conditioning_min")),
        "depressant_conditioning_min": _n_str(d1.get("conditioning_min")),
        "activator_conditioning_min": _n_str(a1.get("conditioning_min")),
        "dispersant_conditioning_min": _n_str(disp.get("conditioning_min")),
        "frother_conditioning_min": _n_str(fr.get("conditioning_min")),
        "flotation_time_min": _n_str(cond.get("flotation_time_min")),
        "airflow_rate": _s(cond.get("airflow_rate", "")),
        "pretreatment": _s(context.get("pretreatment", "")),
        "stage": _s(results.get("stage", "")),
        "concentrate_type": _s(results.get("concentrate_type", "")),

        "valuable_analyte": _s(valuable.get("analyte", "")),
        "valuable_recovery_pct": _n_str(valuable.get("recovery_pct")),
        "valuable_grade_value": _n_str(valuable.get("grade_value")),
        "valuable_grade_unit": _s(valuable.get("grade_unit", "")),
        "gangue_analyte": _s(gangue_prod.get("analyte", "")),
        "gangue_recovery_pct": _n_str(gangue_prod.get("recovery_pct")),
        "gangue_grade_value": _n_str(gangue_prod.get("grade_value")),
        "gangue_grade_unit": _s(gangue_prod.get("grade_unit", "")),
        "enrichment_ratio": _n_str(valuable.get("enrichment_ratio")),
        "mass_pull_pct": _n_str(results.get("mass_pull_pct")),

        "selectivity_score": selectivity,
        "recipe_score": recipe_score,
        "recipe_rank_in_experiment_group": "",  # computed after all rows
        "is_best_in_experiment_group": "",       # computed after all rows
        "is_promising": "1" if _v(signals.get("is_promising"), False) else "0",

        "value_source": value_source,
        "extraction_confidence": confidence,
        "reported_as_best": reported_best,
        "notes": _s(exp.get("notes", "")),
    }
    return row


# ═══════════════════════════════════════════════════
# Rank within experiment groups
# ═══════════════════════════════════════════════════

def compute_ranks(rows):
    """Compute recipe_rank and is_best within each paper's experiment group."""
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        key = row["paper_id"] or row.get("_source_file", "") or row["case_id"]
        groups[key].append(i)

    for key, indices in groups.items():
        scored = []
        for i in indices:
            rs = rows[i]["recipe_score"]
            scored.append((i, float(rs) if rs else -999))

        scored.sort(key=lambda x: x[1], reverse=True)
        for rank, (idx, score) in enumerate(scored, 1):
            rows[idx]["recipe_rank_in_experiment_group"] = str(rank)
            rows[idx]["is_best_in_experiment_group"] = "1" if rank == 1 and score > -999 else "0"
            # Also mark non-best as not-best
            if rank > 1:
                rows[idx]["is_best_in_experiment_group"] = "0"


# ═══════════════════════════════════════════════════
# File processing
# ═══════════════════════════════════════════════════

def process_file(filepath, case_counter):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    paper = data.get("paper", {})
    ore = data.get("ore_profile", {})

    # v3 flat: file IS the experiment (has reagent_scheme at root)
    # v2 nested: file has experiments[] array
    if "reagent_scheme" in data:
        experiments = [data]
    else:
        experiments = data.get("experiments", [])

    rows = []
    for exp in experiments:
        case_counter[0] += 1
        case_id = f"C{case_counter[0]:05d}"
        row = experiment_to_row(paper, ore, exp, case_id)
        row["_source_file"] = os.path.basename(filepath)
        rows.append(row)

    return rows


def write_csv(rows, filepath, columns, append=False):
    file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0
    mode = "a" if append and file_exists else "w"

    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

    with open(filepath, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if mode == "w" or not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Convert v2 recipe JSON → ML CSV (two tables)")
    parser.add_argument("--file", default="", help="Single recipe JSON")
    parser.add_argument("--dir", default="", help="Directory of recipe JSONs")
    parser.add_argument("--output-dir", required=True, help="Output directory for CSV files")
    parser.add_argument("--append", action="store_true", help="Append to existing CSVs")
    args = parser.parse_args()

    all_rows = []
    case_counter = [0]

    if args.dir:
        for fname in sorted(os.listdir(args.dir)):
            if not fname.endswith(".json"):
                continue
            try:
                rows = process_file(os.path.join(args.dir, fname), case_counter)
                all_rows.extend(rows)
            except Exception as e:
                print(f"[WARN] {fname}: {e}", file=sys.stderr)
    elif args.file:
        all_rows = process_file(args.file, case_counter)
    else:
        print(json.dumps({"error": "Provide --file or --dir"}))
        sys.exit(1)

    # Compute ranks within experiment groups
    compute_ranks(all_rows)

    # Write training table
    train_path = os.path.join(args.output_dir, "recipe_recommendation_train.csv")
    write_csv(all_rows, train_path, TRAIN_COLUMNS, args.append)

    # Stats
    files = set(r.get("_source_file", "") for r in all_rows)
    scored = sum(1 for r in all_rows if r.get("recipe_score"))
    best_count = sum(1 for r in all_rows if r.get("is_best_in_experiment_group") == "1")

    print(json.dumps({
        "output_dir": args.output_dir,
        "files": {
            "train": train_path,
        },
        "mode": "append" if args.append else "write",
        "rows_written": len(all_rows),
        "columns_train": len(TRAIN_COLUMNS),
        "files_processed": len(files),
        "experiments_with_score": scored,
        "best_in_group_count": best_count,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
