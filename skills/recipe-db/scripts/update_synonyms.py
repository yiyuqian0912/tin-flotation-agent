#!/usr/bin/env python3
"""
update_synonyms.py — Scan recipe JSONs and grow the synonym dictionary.
Extracts ore types, mineral names, reagent names, etc. from extracted recipes
and adds them to the synonym map if not already present.

Usage:
    python3 update_synonyms.py --file zhang_2024.json --dict ~/.openclaw/workspace/database/synonyms.json
    python3 update_synonyms.py --dir ~/.openclaw/workspace/database/recipes --dict ~/.openclaw/workspace/database/synonyms.json

The dictionary is created if it doesn't exist (from seed file or empty).
New terms are added under the appropriate category. Existing entries are never overwritten.
"""

import argparse
import json
import os
import re
import sys


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


# ═══════════════════════════════════════════════════
# Term extraction from recipe JSON
# ═══════════════════════════════════════════════════

def extract_terms(data):
    """Extract all classifiable terms from a v2 recipe JSON."""
    terms = {
        "ore_type": set(),
        "minerals": set(),
        "reagents": set(),
        "process": set(),
    }

    ore = data.get("ore_profile", {})

    # Ore type
    for field in ["ore_type", "ore_type_standard"]:
        val = _s(ore.get(field, ""))
        if val:
            terms["ore_type"].add(val)

    # Valuable minerals
    for vm in ore.get("valuable_minerals", []):
        name = _s(vm.get("name", ""))
        if name:
            terms["minerals"].add(name)

    # Gangue minerals
    for gm in ore.get("gangue_minerals", []):
        name = _s(gm.get("name", ""))
        if name:
            terms["minerals"].add(name)

    # Associated minerals
    for am in ore.get("associated_minerals", []):
        name = _s(am.get("name", ""))
        if name:
            terms["minerals"].add(name)

    # Head assay analytes
    for ha in ore.get("head_assays", []):
        analyte = _s(ha.get("analyte", ""))
        if analyte:
            terms["minerals"].add(analyte)

    # Experiments — v3 flat (file IS the experiment) or v2 nested
    if "reagent_scheme" in data:
        experiments = [data]
    else:
        experiments = data.get("experiments", [])

    for exp in experiments:
        # Reagent names
        for r in exp.get("reagent_scheme", []):
            for field in ["name", "name_standard"]:
                val = _s(r.get(field, ""))
                if val:
                    terms["reagents"].add(val)
            # Chemical formula as alias
            formula = _s(r.get("chemical_formula", ""))
            if formula:
                terms["reagents"].add(formula)

            # Target mineral
            target = _s(r.get("target_mineral", ""))
            if target:
                terms["minerals"].add(target)

        # Stage
        stage = _s(exp.get("results", {}).get("stage", ""))
        if stage:
            terms["process"].add(stage)

        # Concentrate type
        conc = _s(exp.get("results", {}).get("concentrate_type", ""))
        if conc:
            terms["process"].add(conc)

        # Pretreatment (v2: ore_context_override, v3: sample_override)
        ctx = exp.get("ore_context_override", exp.get("sample_override", {}))
        pretreat = _s(ctx.get("pretreatment", ""))
        if pretreat:
            terms["process"].add(pretreat)

        # Retrieval tags
        retrieval = exp.get("retrieval", {})
        for tag_field in ["ore_tags", "gangue_tags", "associated_tags", "reagent_tags"]:
            tags = _v(retrieval.get(tag_field, {}), [])
            if isinstance(tags, list):
                for t in tags:
                    if isinstance(t, str) and t.strip():
                        if tag_field == "reagent_tags":
                            terms["reagents"].add(t.strip())
                        else:
                            terms["minerals"].add(t.strip())

    return terms


# ═══════════════════════════════════════════════════
# Dictionary management
# ═══════════════════════════════════════════════════

SEED_TEMPLATE = {
    "_description": "近义词/术语词典。由 update_synonyms.py 自动维护。key=出现过的写法，value=标准化名称。用户可手动修改 value 来建立映射关系。",
    "ore_type": {},
    "minerals": {},
    "reagents": {},
    "pain_points": {},
    "process": {},
}

# Known standard forms for auto-mapping
AUTO_MAP = {
    # Chinese → English mineral names
    "锡石": "cassiterite", "石英": "quartz", "方解石": "calcite",
    "萤石": "fluorite", "长石": "feldspar", "电气石": "tourmaline",
    "绿泥石": "chlorite", "云母": "mica", "白云石": "dolomite",
    "赤铁矿": "hematite", "磁铁矿": "magnetite", "黄铁矿": "pyrite",
    "毒砂": "arsenopyrite", "黄铜矿": "chalcopyrite", "黑钨矿": "wolframite",
    "白钨矿": "scheelite", "磁黄铁矿": "pyrrhotite",
    # Chinese → English reagent names
    "水杨羟肟酸": "SHA", "苯甲羟肟酸": "BHA", "苯乙烯膦酸": "SPA",
    "六偏磷酸钠": "SHMP", "水玻璃": "sodium silicate",
    "羧甲基纤维素": "CMC", "硝酸铅": "lead nitrate",
    "甲基异丁基甲醇": "MIBC", "松醇油": "pine oil",
    "淀粉": "starch", "糊精": "dextrin", "油酸": "oleic acid",
    "油酸钠": "sodium oleate", "单宁酸": "tannic acid",
    "木质素磺酸钠": "sodium lignosulfonate",
    "氧化石蜡皂": "oxidized paraffin soap", "塔尔油": "tall oil",
    # Chinese → English ore types
    "氧化锡": "oxide tin", "氧化锡矿": "oxide tin ore",
    "硫化锡": "sulfide tin", "砂锡矿": "alluvial tin",
    "细粒锡石": "fine grained cassiterite",
    # Abbreviation expansions
    "sodium hexametaphosphate": "SHMP", "Na2SiO3": "sodium silicate",
    "Pb(NO3)2": "lead nitrate", "carboxymethyl cellulose": "CMC",
    "salicylhydroxamic acid": "SHA", "benzohydroxamic acid": "BHA",
    "styrene phosphonic acid": "SPA",
    # Process
    "粗选": "rougher", "精选": "cleaner", "扫选": "scavenger",
}


def load_dict(path):
    """Load existing dictionary or create from seed."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return dict(SEED_TEMPLATE)


def save_dict(d, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def merge_terms(dictionary, new_terms):
    """Add new terms to dictionary. Returns count of new entries added."""
    added = 0

    for category, term_set in new_terms.items():
        if category not in dictionary:
            dictionary[category] = {}

        existing = dictionary[category]
        # Build reverse lookup: standard_form → already known
        existing_lower = {k.lower(): v for k, v in existing.items()}
        existing_values_lower = {v.lower() for v in existing.values()}

        for term in term_set:
            term_clean = term.strip()
            if not term_clean:
                continue

            term_lower = term_clean.lower()

            # Skip if already in dictionary (as key)
            if term_lower in existing_lower:
                continue

            # Skip if already in dictionary (as value)
            if term_lower in existing_values_lower:
                continue

            # Try auto-mapping
            mapped = AUTO_MAP.get(term_clean) or AUTO_MAP.get(term_lower)

            if mapped:
                # Known mapping: add term → standard
                existing[term_clean] = mapped
                existing_lower[term_lower] = mapped
                existing_values_lower.add(mapped.lower())
            else:
                # Unknown term: add as identity mapping (term → term)
                # User can later edit the value to create a real mapping
                existing[term_clean] = term_clean.lower()
                existing_lower[term_lower] = term_clean.lower()
                existing_values_lower.add(term_clean.lower())

            added += 1

    return added


# ═══════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════

def process_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return extract_terms(data)


def main():
    parser = argparse.ArgumentParser(description="Update synonym dictionary from recipe JSONs")
    parser.add_argument("--file", default="", help="Single recipe JSON")
    parser.add_argument("--dir", default="", help="Directory of recipe JSONs")
    parser.add_argument("--dict", required=True, help="Path to synonyms.json dictionary")
    args = parser.parse_args()

    dictionary = load_dict(args.dict)
    all_terms = {"ore_type": set(), "minerals": set(), "reagents": set(), "process": set()}

    files_processed = 0

    if args.dir:
        for fname in sorted(os.listdir(args.dir)):
            if not fname.endswith(".json"):
                continue
            try:
                terms = process_file(os.path.join(args.dir, fname))
                for cat in all_terms:
                    all_terms[cat] |= terms.get(cat, set())
                files_processed += 1
            except Exception as e:
                print(f"[WARN] {fname}: {e}", file=sys.stderr)
    elif args.file:
        terms = process_file(args.file)
        for cat in all_terms:
            all_terms[cat] |= terms.get(cat, set())
        files_processed = 1
    else:
        print(json.dumps({"error": "Provide --file or --dir"}))
        sys.exit(1)

    added = merge_terms(dictionary, all_terms)
    save_dict(dictionary, args.dict)

    # Stats
    total_entries = sum(
        len(v) for k, v in dictionary.items()
        if isinstance(v, dict) and not k.startswith("_")
    )

    total_terms_found = sum(len(s) for s in all_terms.values())

    print(json.dumps({
        "dict_path": args.dict,
        "files_scanned": files_processed,
        "terms_found": total_terms_found,
        "new_entries_added": added,
        "total_dict_entries": total_entries,
        "by_category": {
            cat: len(dictionary.get(cat, {}))
            for cat in ["ore_type", "minerals", "reagents", "process", "pain_points"]
        }
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
