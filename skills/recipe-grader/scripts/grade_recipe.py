#!/usr/bin/env python3
"""
grade_recipe.py — Grade flotation recipes on a 100-point scale.
Compatible with recipe_template v2.0 ({"value":..., "source":...} format).

Usage:
    python3 grade_recipe.py --file recipe.json
    python3 grade_recipe.py --dir ~/.openclaw/workspace/database/recipes
    echo '{recipe_json}' | python3 grade_recipe.py

Requires: no external dependencies (pure stdlib)
"""

import argparse
import json
import os
import re
import sys


# ═══════════════════════════════════════════════════
# Value extraction helpers (v2 template)
# ═══════════════════════════════════════════════════

def _v(obj, default=None):
    """Extract value from {value, source} format or plain value."""
    if isinstance(obj, dict) and "value" in obj:
        val = obj["value"]
        return val if val is not None else default
    if obj is None:
        return default
    return obj


def _src(obj):
    """Extract source from {value, source} format."""
    if isinstance(obj, dict) and "source" in obj:
        return obj.get("source", "")
    return ""


def _num(obj, default=None):
    """Extract numeric value."""
    val = _v(obj)
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    m = re.search(r'(\d+\.?\d*)', str(val))
    return float(m.group(1)) if m else default


def _str(obj, default=""):
    """Extract string value, lowercased."""
    val = _v(obj)
    if val is None:
        return default
    return str(val).strip().lower()


# ═══════════════════════════════════════════════════
# Knowledge base
# ═══════════════════════════════════════════════════

COLLECTOR_PH = {
    "sha":        {"optimal": (4.0, 6.0),  "acceptable": (3.0, 7.0)},
    "bha":        {"optimal": (5.0, 7.0),  "acceptable": (4.0, 8.0)},
    "spa":        {"optimal": (3.0, 5.0),  "acceptable": (2.5, 6.0)},
    "hydroxamic": {"optimal": (4.0, 7.0),  "acceptable": (3.0, 8.0)},
    "oleic":      {"optimal": (8.0, 10.0), "acceptable": (7.0, 11.0)},
    "phosphonic": {"optimal": (3.0, 5.5),  "acceptable": (2.5, 6.5)},
}

ANTAGONISMS = [
    ("water glass",  "bha",    "High-dose water glass can suppress BHA adsorption on cassiterite"),
    ("sodium silicate", "bha", "High-dose sodium silicate can suppress BHA adsorption on cassiterite"),
    ("shmp",         "pb",     "SHMP may chelate Pb²⁺ reducing activation effect"),
    ("shmp",         "lead",   "SHMP may chelate Pb²⁺ reducing activation effect"),
    ("starch",       "sha",    "Starch may coat mineral surfaces blocking SHA adsorption"),
    ("cmc",          "sha",    "High-dose CMC may non-selectively depress cassiterite flotation"),
]

HAZARDOUS = ["lead", "pb", "pb(no3)2", "chromate", "cr⁶", "mercury", "hg", "arsenic", "as₂o₃", "铅"]

DOSAGE_RANGES = {
    "collector":   (50, 3000),
    "depressant":  (30, 3000),
    "activator":   (30, 1500),
    "frother":     (5, 150),
    "dispersant":  (30, 2000),
    "ph modifier": (0, 10000),
    "sulfidizer":  (50, 5000),
}


def _in_range(val, r):
    return r[0] <= val <= r[1]


# ═══════════════════════════════════════════════════
# Extract structured data from v2 experiment
# ═══════════════════════════════════════════════════

def _get_reagents_by_role(experiment, role):
    """Get all reagents with a given role from reagent_scheme array."""
    scheme = experiment.get("reagent_scheme", [])
    return [r for r in scheme if _str(r.get("role", "")) == role.lower()]


def _get_all_reagent_names(experiment):
    """Get concatenated lowercase string of all reagent names."""
    scheme = experiment.get("reagent_scheme", [])
    names = []
    for r in scheme:
        name = _str(r.get("name", ""))
        name_std = _str(r.get("name_standard", ""))
        formula = _str(r.get("chemical_formula", ""))
        names.extend([name, name_std, formula])
    return " ".join(names)


def _get_ph(experiment):
    """Extract pH value from pH modifier reagents."""
    ph_reagents = _get_reagents_by_role(experiment, "ph modifier")
    for r in ph_reagents:
        val = _num(r.get("target_ph"))
        if val is not None:
            return val
        # Try range midpoint
        lo = _num(r.get("ph_range_min"))
        hi = _num(r.get("ph_range_max"))
        if lo is not None and hi is not None:
            return (lo + hi) / 2
        if lo is not None:
            return lo
        if hi is not None:
            return hi
    return None


def _get_collector_names(experiment):
    """Get lowercase collector name strings."""
    collectors = _get_reagents_by_role(experiment, "collector")
    names = []
    for c in collectors:
        names.append(_str(c.get("name", "")))
        names.append(_str(c.get("name_standard", "")))
    return [n for n in names if n]


def _get_sn_product(experiment):
    """Find the primary Sn/SnO2/cassiterite product in results."""
    products = experiment.get("results", {}).get("products", [])
    sn_keywords = ["sn", "sno2", "cassiterite", "tin", "锡"]
    for p in products:
        analyte = _str(p.get("analyte", ""))
        if any(k in analyte for k in sn_keywords):
            return p
    # If only one product, assume it's the main one
    if len(products) == 1:
        return products[0]
    return None


# ═══════════════════════════════════════════════════
# Scoring dimensions
# ═══════════════════════════════════════════════════

def score_completeness(experiment, ore_profile):
    """How complete is the recipe? (25 points)"""
    score = 25
    deductions = []

    checks = [
        ("collector",           5, lambda: bool(_get_reagents_by_role(experiment, "collector"))),
        ("collector dosage",    2, lambda: any(_num(r.get("dosage_gpt")) for r in _get_reagents_by_role(experiment, "collector"))),
        ("depressant",          2, lambda: bool(_get_reagents_by_role(experiment, "depressant"))),
        ("pH value",            4, lambda: _get_ph(experiment) is not None),
        ("Sn recovery",         4, lambda: _num((_get_sn_product(experiment) or {}).get("recovery_pct")) is not None),
        ("Sn grade",            3, lambda: _num((_get_sn_product(experiment) or {}).get("grade_value")) is not None),
        ("particle size",       2, lambda: _num(ore_profile.get("particle_size", {}).get("d80_um")) is not None),
        ("ore type",            1, lambda: bool(_str(ore_profile.get("ore_type", "")))),
        ("reagent addition order", 1, lambda: any(_num(r.get("order")) for r in experiment.get("reagent_scheme", []))),
        ("flotation time",      1, lambda: _num(experiment.get("flotation_conditions", {}).get("flotation_time_min")) is not None),
    ]

    for label, pts, check_fn in checks:
        try:
            if not check_fn():
                score -= pts
                deductions.append({"field": label, "deducted": pts, "reason": f"{label} 未提及"})
        except Exception:
            score -= pts
            deductions.append({"field": label, "deducted": pts, "reason": f"{label} 解析失败"})

    return {"score": round(max(score, 0), 1), "max": 25, "deductions": deductions}


def score_chemical_logic(experiment):
    """Is the chemistry internally consistent? (30 points)"""
    score = 30
    deductions = []

    # ── pH vs collector (15 pts) ──
    ph = _get_ph(experiment)
    collector_names = _get_collector_names(experiment)

    ph_evaluated = False
    if ph is not None and collector_names:
        for cname in collector_names:
            for key, ranges in COLLECTOR_PH.items():
                if key in cname:
                    if _in_range(ph, ranges["optimal"]):
                        pass
                    elif _in_range(ph, ranges["acceptable"]):
                        score -= 5
                        deductions.append({
                            "field": "pH-捕收剂匹配",
                            "deducted": 5,
                            "reason": f"pH {ph} 在 {key} 可接受范围但非最优 (最优: {ranges['optimal'][0]}-{ranges['optimal'][1]})"
                        })
                    else:
                        score -= 12
                        deductions.append({
                            "field": "pH-捕收剂匹配",
                            "deducted": 12,
                            "reason": f"pH {ph} 超出 {key} 可接受范围 ({ranges['acceptable'][0]}-{ranges['acceptable'][1]})"
                        })
                    ph_evaluated = True
                    break
            if ph_evaluated:
                break

    if not ph_evaluated:
        if ph is None:
            score -= 10
            deductions.append({"field": "pH-捕收剂匹配", "deducted": 10,
                              "reason": "pH 未提及 — 无法评估"})
        elif not collector_names:
            score -= 10
            deductions.append({"field": "pH-捕收剂匹配", "deducted": 10,
                              "reason": "捕收剂未提及 — 无法评估"})
        else:
            score -= 7
            deductions.append({"field": "pH-捕收剂匹配", "deducted": 7,
                              "reason": f"捕收剂 '{collector_names[0]}' 不在已知pH数据库中"})

    # ── Antagonism check (10 pts) ──
    all_reagents = _get_all_reagent_names(experiment)
    for a, b, reason in ANTAGONISMS:
        if a in all_reagents and b in all_reagents:
            score -= 4
            deductions.append({
                "field": "药剂拮抗",
                "deducted": 4,
                "reason": f"潜在拮抗: {a} + {b} — {reason}"
            })

    # ── Dosage sanity (5 pts) ──
    for reagent in experiment.get("reagent_scheme", []):
        role = _str(reagent.get("role", ""))
        dosage = _num(reagent.get("dosage_gpt"))
        name = _str(reagent.get("name", "")) or _str(reagent.get("name_standard", ""))

        if dosage is not None and role in DOSAGE_RANGES:
            expected = DOSAGE_RANGES[role]
            if not _in_range(dosage, expected):
                score -= 1.5
                deductions.append({
                    "field": f"{role}用量",
                    "deducted": 1.5,
                    "reason": f"{name} 用量 {dosage} g/t 超出 {role} 典型范围 ({expected[0]}-{expected[1]} g/t)"
                })

    return {"score": round(max(score, 0), 1), "max": 30, "deductions": deductions}


def score_results_quality(experiment):
    """How good are the reported results? (25 points)"""
    score = 25
    deductions = []

    sn = _get_sn_product(experiment)

    # ── Recovery (15 pts) ──
    recovery = _num(sn.get("recovery_pct")) if sn else None
    if recovery is not None:
        if recovery >= 80:
            pass
        elif recovery >= 70:
            score -= 3
            deductions.append({"field": "回收率", "deducted": 3,
                              "reason": f"回收率 {recovery}% 良好但低于 80%"})
        elif recovery >= 60:
            score -= 6
            deductions.append({"field": "回收率", "deducted": 6,
                              "reason": f"回收率 {recovery}% 中等 (低于 70%)"})
        elif recovery >= 50:
            score -= 9
            deductions.append({"field": "回收率", "deducted": 9,
                              "reason": f"回收率 {recovery}% 偏低 (低于 60%)"})
        else:
            score -= 12
            deductions.append({"field": "回收率", "deducted": 12,
                              "reason": f"回收率 {recovery}% 很低 (低于 50%)"})
    else:
        score -= 10
        deductions.append({"field": "回收率", "deducted": 10,
                          "reason": "回收率未提及 — 无法评估"})

    # ── Grade (10 pts) ──
    grade = _num(sn.get("grade_value")) if sn else None
    grade_unit = _str(sn.get("grade_unit")) if sn else "%"
    if grade is not None:
        # Normalize: if unit is g/t, skip percentage-based scoring
        if "g/t" in grade_unit or "ppm" in grade_unit:
            pass  # Can't compare g/t grades the same way
        else:
            if grade >= 50:
                pass
            elif grade >= 40:
                score -= 2
                deductions.append({"field": "品位", "deducted": 2,
                                  "reason": f"品位 {grade}% 良好但低于 50%"})
            elif grade >= 30:
                score -= 4
                deductions.append({"field": "品位", "deducted": 4,
                                  "reason": f"品位 {grade}% 中等 (低于 40%)"})
            elif grade >= 20:
                score -= 6
                deductions.append({"field": "品位", "deducted": 6,
                                  "reason": f"品位 {grade}% 偏低 (低于 30%)"})
            else:
                score -= 8
                deductions.append({"field": "品位", "deducted": 8,
                                  "reason": f"品位 {grade}% 很低 (低于 20%)"})
    else:
        score -= 7
        deductions.append({"field": "品位", "deducted": 7,
                          "reason": "品位未提及 — 无法评估"})

    return {"score": round(max(score, 0), 1), "max": 25, "deductions": deductions}


def score_safety(experiment):
    """Environmental and safety assessment. (20 points)"""
    score = 20
    deductions = []

    all_reagents = _get_all_reagent_names(experiment)

    # Check for hazardous reagents
    for hazard in HAZARDOUS:
        if hazard in all_reagents:
            score -= 10
            deductions.append({
                "field": "危险药剂",
                "deducted": 10,
                "reason": f"含有危险药剂: {hazard} — 环保及法规风险"
            })
            break

    # Check environmental_warning flags
    if not deductions:
        for reagent in experiment.get("reagent_scheme", []):
            warn = _v(reagent.get("environmental_warning"), False)
            if warn:
                score -= 8
                deductions.append({
                    "field": "环保标记",
                    "deducted": 8,
                    "reason": f"药剂 {_str(reagent.get('name', ''))} 标记环保风险"
                })
                break

    return {"score": max(score, 0), "max": 20, "deductions": deductions}


# ═══════════════════════════════════════════════════
# Main grading
# ═══════════════════════════════════════════════════

def grade_experiment(experiment, ore_profile, paper_info=None):
    """Grade a single experiment."""
    d1 = score_completeness(experiment, ore_profile)
    d2 = score_chemical_logic(experiment)
    d3 = score_results_quality(experiment)
    d4 = score_safety(experiment)

    total = d1["score"] + d2["score"] + d3["score"] + d4["score"]
    all_deductions = d1["deductions"] + d2["deductions"] + d3["deductions"] + d4["deductions"]
    total_deducted = sum(d["deducted"] for d in all_deductions)

    if total >= 75:
        rating, verdict = "A", "优秀配方 — 建议实验验证"
    elif total >= 60:
        rating, verdict = "B", "较好配方 — 可考虑修改后验证"
    elif total >= 45:
        rating, verdict = "C", "较弱配方 — 存在明显不足"
    else:
        rating, verdict = "D", "不足配方 — 数据缺失或重大问题"

    paper = paper_info or {}
    exp_id = _v(experiment.get("experiment_id", ""), "")
    exp_name = _v(experiment.get("experiment_name", ""), "")

    # Extract key info for display
    sn = _get_sn_product(experiment)
    recovery = _num(sn.get("recovery_pct")) if sn else None
    grade_val = _num(sn.get("grade_value")) if sn else None
    analyte = _str(sn.get("analyte")) if sn else ""

    return {
        "experiment_id": exp_id,
        "experiment_name": exp_name,
        "total_score": round(total, 1),
        "max_score": 100,
        "total_deducted": round(total_deducted, 1),
        "rating": rating,
        "verdict": verdict,
        "breakdown": {
            "完整性 (completeness)":    d1,
            "化学逻辑 (chemical_logic)":  d2,
            "结果质量 (results_quality)": d3,
            "安全性 (safety)":          d4,
        },
        "all_deductions": all_deductions,
        "key_results": {
            "analyte": analyte,
            "recovery_pct": recovery,
            "grade_value": grade_val,
        },
        "paper_title": _v(paper.get("title", ""), ""),
        "paper_year": _v(paper.get("year", ""), ""),
        "is_reported_best": _v(experiment.get("results", {}).get("is_reported_best"), False),
    }


def _detect_format(data):
    """Detect v3 (flat, one experiment per file) vs v2 (nested experiments[])."""
    if "reagent_scheme" in data:
        return "v3"  # flat — file IS the experiment
    if "experiments" in data:
        return "v2"  # nested — file has experiments array
    return "v3"  # default to flat


def process_file(filepath):
    """Process one recipe JSON file. Supports v3 (flat) and v2 (nested)."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    fmt = _detect_format(data)

    if fmt == "v3":
        # Flat: file IS the experiment
        paper = data.get("paper", {})
        ore = data.get("ore_profile", {})
        result = grade_experiment(data, ore, paper)
        result["source_file"] = os.path.basename(filepath)
        return [result]
    else:
        # v2: nested experiments
        paper = data.get("paper", {})
        ore = data.get("ore_profile", {})
        graded = []
        for exp in data.get("experiments", []):
            result = grade_experiment(exp, ore, paper)
            result["source_file"] = os.path.basename(filepath)
            graded.append(result)
        return graded


def merge_scores_into_file(filepath):
    """Grade and write scores back into the JSON file. Supports v3 and v2."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    fmt = _detect_format(data)
    all_scores = []

    if fmt == "v3":
        # Flat: file IS the experiment
        paper = data.get("paper", {})
        ore = data.get("ore_profile", {})
        result = grade_experiment(data, ore, paper)

        data["recommendation_signals"] = {
            "recommendation_score": result["total_score"],
            "grade_rating": result["rating"],
            "is_promising": result["total_score"] >= 60,
            "grade_verdict": result["verdict"],
            "grade_breakdown": result["breakdown"],
            "grade_deductions": result["all_deductions"],
            "total_deducted": result["total_deducted"],
            "key_results": result["key_results"],
        }
        all_scores.append(result["total_score"])

    else:
        # v2: nested experiments
        paper = data.get("paper", {})
        ore = data.get("ore_profile", {})
        for exp in data.get("experiments", []):
            result = grade_experiment(exp, ore, paper)
            exp["recommendation_signals"] = {
                "recommendation_score": result["total_score"],
                "grade_rating": result["rating"],
                "is_promising": result["total_score"] >= 60,
                "grade_verdict": result["verdict"],
                "grade_breakdown": result["breakdown"],
                "grade_deductions": result["all_deductions"],
                "total_deducted": result["total_deducted"],
                "key_results": result["key_results"],
            }
            all_scores.append(result["total_score"])

    # Write back in place
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return {
        "file": os.path.basename(filepath),
        "experiments_graded": len(all_scores),
        "scores": all_scores,
        "average": round(sum(all_scores) / len(all_scores), 1) if all_scores else 0,
        "best": max(all_scores) if all_scores else 0,
        "ratings": {
            "A": sum(1 for s in all_scores if s >= 75),
            "B": sum(1 for s in all_scores if 60 <= s < 75),
            "C": sum(1 for s in all_scores if 45 <= s < 60),
            "D": sum(1 for s in all_scores if s < 45),
        }
    }


def main():
    parser = argparse.ArgumentParser(description="Grade flotation recipes (v2 template, 100-point scale)")
    parser.add_argument("--file", default="", help="Single recipe JSON file")
    parser.add_argument("--dir", default="", help="Directory of recipe JSON files")
    parser.add_argument("--output", default="", help="Save grading report JSON to this path")
    parser.add_argument("--merge", action="store_true",
                        help="Write scores back into source JSON files (in place)")
    args = parser.parse_args()

    # ── Merge mode: grade + write back into source files ──
    if args.merge:
        merge_results = []

        if args.dir:
            for fname in sorted(os.listdir(args.dir)):
                if not fname.endswith(".json"):
                    continue
                try:
                    r = merge_scores_into_file(os.path.join(args.dir, fname))
                    if r:
                        merge_results.append(r)
                except Exception as e:
                    print(f"[WARN] {fname}: {e}", file=sys.stderr)
        elif args.file:
            r = merge_scores_into_file(args.file)
            if r:
                merge_results.append(r)
        else:
            print(json.dumps({"error": "Provide --file or --dir with --merge"}))
            sys.exit(1)

        total_exp = sum(r["experiments_graded"] for r in merge_results)
        all_scores = [s for r in merge_results for s in r["scores"]]

        print(json.dumps({
            "mode": "merge",
            "files_processed": len(merge_results),
            "total_experiments_graded": total_exp,
            "overall_average": round(sum(all_scores) / len(all_scores), 1) if all_scores else 0,
            "overall_best": max(all_scores) if all_scores else 0,
            "per_file": merge_results,
        }, ensure_ascii=False, indent=2))
        return

    # ── Report mode (default): grade + output report ──
    all_graded = []

    if args.dir:
        if not os.path.isdir(args.dir):
            print(json.dumps({"error": f"Directory not found: {args.dir}"}))
            sys.exit(1)
        for fname in sorted(os.listdir(args.dir)):
            if not fname.endswith(".json"):
                continue
            try:
                results = process_file(os.path.join(args.dir, fname))
                all_graded.extend(results)
            except Exception as e:
                print(f"[WARN] Skip {fname}: {e}", file=sys.stderr)
    elif args.file:
        all_graded = process_file(args.file)
    else:
        raw = sys.stdin.read().strip()
        if not raw:
            print(json.dumps({"error": "No input. Provide --file, --dir, or pipe JSON to stdin."}))
            sys.exit(1)
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                all_graded.append(grade_experiment(item, {}))
        else:
            paper = data.get("paper", {})
            ore = data.get("ore_profile", {})
            for exp in data.get("experiments", [data]):
                all_graded.append(grade_experiment(exp, ore, paper))

    # Sort by score
    all_graded.sort(key=lambda x: x["total_score"], reverse=True)

    stats = {
        "total_experiments": len(all_graded),
        "grade_A": sum(1 for g in all_graded if g["rating"] == "A"),
        "grade_B": sum(1 for g in all_graded if g["rating"] == "B"),
        "grade_C": sum(1 for g in all_graded if g["rating"] == "C"),
        "grade_D": sum(1 for g in all_graded if g["rating"] == "D"),
        "average_score": round(sum(g["total_score"] for g in all_graded) / len(all_graded), 1) if all_graded else 0,
        "best_reported_count": sum(1 for g in all_graded if g.get("is_reported_best")),
        "files_processed": len(set(g.get("source_file", "") for g in all_graded if g.get("source_file"))),
    }

    report = {
        "stats": stats,
        "graded_experiments": all_graded,
    }

    output = json.dumps(report, ensure_ascii=False, indent=2)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"[INFO] Report saved to {args.output}", file=sys.stderr)

    print(output)


if __name__ == "__main__":
    main()
