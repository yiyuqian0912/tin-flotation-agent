---
name: recipe-extractor
description: >
  Extract flotation reagent recipes from paper PDFs. Works in two modes:
  (1) Given a list of paper URLs from a search, fetch PDFs and extract recipes.
  (2) Given a user-uploaded local PDF file, extract recipes directly.
  Triggers on: extract recipe, fetch paper, get full text, 提取配方,
  fetch articles, extract reagent, recipe extraction, uploaded PDF,
  analyze this paper, read this PDF, extract from this file.
metadata:
  openclaw:
    emoji: "🧪"
    requires:
      bins: ["python3"]
---

# Recipe Extractor

Extract flotation reagent recipes from paper PDFs.

## Language rule

You MUST respond in Chinese (中文) by default.
Technical terms MUST be written in Chinese with English in parentheses, e.g., 捕收剂 (collector).
Reagent names use Chinese with English, e.g., 水杨羟肟酸 (SHA).
Paper titles, author names, and links stay in their original language.

Two modes:
- **Mode A — Local PDF**: user uploads or provides a file path → extract recipes directly
- **Mode B — Search results**: given paper URLs from a search → fetch PDFs, skip failures, extract recipes


## Display rule

You MUST NOT show intermediate script execution details to the user. This includes:
- Raw bash commands being run
- Script stdout/stderr with [INFO], [WARN] prefixes
- JSON output from scripts before formatting
- File paths of temp files
- pip install output or dependency checks

You MUST only show the final formatted result as defined in the output templates.
While scripts are running, you may show a brief status like "正在搜索论文..." or "正在提取配方..." but nothing more.
If a script fails, show only the user-facing error message, not the raw traceback.

## Core rule

Read the full PDF text. Extract data into the JSON template defined in `references/recipe_template.json`.
Only extract values explicitly written in the paper. If not stated, leave as `null`.
Skip any PDF that cannot be read.

## JSON Template

Before extracting, read the template file:

```
SKILL_DIR/references/recipe_template.json
```

You MUST follow this template structure exactly. Key rules:
- All fields use plain values. `null` = not found in the paper.
- The `experiments` array holds one entry per experiment. Copy the experiment template for each.
- `reagent_scheme` is a flat array — one entry per reagent, differentiated by `role` field.
- The `results.products` array holds one entry per metal/mineral reported.
- Omit fields that are `null` to keep JSON small (scripts handle missing fields).

## Storage & Auto-grading

### One experiment = one file

Each experiment is saved as a separate, self-contained JSON file.
One paper with 5 experiments → 5 files. Each file contains:
- `paper`: 论文来源信息
- `experiment_id`, `experiment_name`, `test_variable`, `data_location`
- `ore_profile`: 矿石特征（从论文级复制到每个文件）
- `sample_override`: 样品差异（如有）
- `flotation_conditions`: 浮选条件
- `reagent_scheme`: 药剂制度
- `results`: 实验结果
- `recommendation_signals`: 评分（由 grading script 自动填入）

### Save location

```
~/.openclaw/workspace/database/recipes/
```

If the directory does not exist, create it:
```bash
mkdir -p ~/.openclaw/workspace/database/recipes
```

### Filename format

```
{first_author}_{year}_exp{N}.json
```

Examples:
- `zhang_2024_exp1.json`, `zhang_2024_exp2.json`, `zhang_2024_exp3.json`
- `li_2023_exp1.json`

If duplicate exists, increment: `zhang_2024_exp1_2.json`

### Save procedure

For a paper with N experiments, you MUST save N separate files:

```bash
cat > ~/.openclaw/workspace/database/recipes/zhang_2024_exp1.json << 'EOF'
{
  "paper": {"title": "...", "year": 2024, ...},
  "experiment_id": "zhang_2024_exp1",
  "ore_profile": { ... same for all experiments from this paper ... },
  "reagent_scheme": [ ... this experiment's reagents ... ],
  "results": { ... this experiment's results ... },
  ...
}
EOF
```

Repeat for exp2, exp3, etc. The `paper` and `ore_profile` sections are identical across all files from the same paper. Only `experiment_id`, `reagent_scheme`, `flotation_conditions`, `results`, and `notes` differ.

### Post-save pipeline (run for EACH file)

After saving each experiment file, run these three steps in order:

**Step 1: Grade**
```bash
python3 GRADER_SKILL_DIR/scripts/grade_recipe.py --file ~/.openclaw/workspace/database/recipes/{filename} --merge 2>/dev/null
```

This writes `recommendation_signals` (score, rating, breakdown, deductions) directly into the file.

**Step 2: Update synonym dictionary**
```bash
python3 RECIPE_DB_SKILL_DIR/scripts/update_synonyms.py --file ~/.openclaw/workspace/database/recipes/{filename} --dict ~/.openclaw/workspace/database/synonyms.json 2>/dev/null
```

This extracts new mineral/reagent/ore terms and adds them to the shared dictionary.

**Step 3 (optional, batch):** After all experiments from a paper are saved:
```bash
python3 GRADER_SKILL_DIR/scripts/grade_recipe.py --dir ~/.openclaw/workspace/database/recipes --merge 2>/dev/null
python3 RECIPE_DB_SKILL_DIR/scripts/update_synonyms.py --dir ~/.openclaw/workspace/database/recipes --dict ~/.openclaw/workspace/database/synonyms.json 2>/dev/null
```

Replace `GRADER_SKILL_DIR` with `~/.openclaw/workspace/skills/recipe-grader`.
Replace `RECIPE_DB_SKILL_DIR` with `~/.openclaw/workspace/skills/recipe-db`.

### Confirmation

After saving ALL experiments from a paper, confirm:

```
✅ 配方已提取并评分：
- 论文：{paper_title} ({year})
- 保存文件：{count} 个（每个实验一个文件）
  - {filename_1} → [{rating}] {score}/100
  - {filename_2} → [{rating}] {score}/100
  - ...
- 平均分：{average}/100
- 最高分：{best}/100
- 近义词词典：新增 {n} 条术语（共 {total} 条）
```

## Prerequisite

This skill requires `pymupdf`. If the fetch script returns an error about pymupdf:
- You MUST tell the user: `Run: pip3 install pymupdf`
- You MUST NOT proceed without it.

---

## Mode A: Local PDF upload

### When to use

Use this mode when:
- The user uploads one or more PDF files
- The user provides local file paths (e.g., "extract recipes from these papers")
- The user says "analyze these papers" or "read these PDFs" with files attached

### Command

For EACH uploaded PDF, run:

```bash
python3 SKILL_DIR/scripts/fetch_paper.py --file "FILE_PATH" 2>/dev/null
```

Replace `SKILL_DIR` with the absolute path to this skill's directory.
Replace `FILE_PATH` with the absolute path to each PDF file.

### Rules

- You MUST process ALL uploaded files. Do NOT skip any.
- You MUST run each file as a separate command.
- If a file does not exist or is not a PDF, report the error and continue to the next file.
- After extracting all files, present recipes using the same output format as Mode B.

---

## Mode B: Search results (remote fetch)

### Input

You will receive a list of papers from a search. Each paper has: title, url, pdf_url (may be empty), source.

### Step 1: Fetch full text

For EACH paper, run:

```bash
python3 SKILL_DIR/scripts/fetch_paper.py --url "PAPER_URL" --pdf-url "PDF_URL" 2>/dev/null
```

Replace `SKILL_DIR` with the absolute path to this skill's directory.
Replace `PAPER_URL` with the paper's url field.
Replace `PDF_URL` with the paper's pdf_url field. If pdf_url is empty, omit the `--pdf-url` flag.

The script will:
1. Try the pdf_url if provided
2. Try arXiv PDF conversion if the URL is an arXiv link
3. Try direct download if the URL ends in .pdf
4. Return `"success": false` if none work

The script automatically:
- Strips useless sections (references, acknowledgments, supplementary)
- Keeps tables in full (tables are the gold for recipe data)
- If text exceeds 60k chars: keeps head (abstract+methods, 60%) + tail (results+conclusion, 40%) + all tables
- Reports `"truncated": true` if truncation occurred

If the script reports `truncated: true`, you still have all tables and the most important text sections. Proceed with extraction normally.

You MUST use BOTH the running text AND the table data (under `EXTRACTED TABLES`) for extraction.

### Fetch rules

- You MUST attempt every paper in the list.
- You MUST run each fetch as a separate command.
- If `"success": false` → skip this paper, move to the next. Do NOT retry.
- If `"success": true` → proceed to extraction.
- You MUST NOT invent or guess the text of a paper you could not fetch.

## Step 2: Extract recipes — THOROUGH SCAN

For each successfully fetched paper, you MUST perform a systematic scan of the ENTIRE text.
Do NOT skim. Do NOT stop after finding one recipe. A single paper often contains 5-20+ experiments.

### Where to look (scan ALL of these sections)

Papers follow a standard structure. You MUST check EVERY section:

| Section | What to extract | Common headings |
|---------|-----------------|-----------------|
| Abstract | Quick overview of best results | Abstract, Summary |
| Introduction | Reagent names mentioned as prior work | Introduction, Background |
| Materials & Methods | Ore composition, particle size, equipment, base procedure | Materials, Methods, Experimental, Sample preparation |
| Reagents section | Full reagent list with suppliers and purity | Reagents, Chemicals, Materials |
| Flotation procedure | Step-by-step process, conditioning times, cell volume, rpm | Flotation tests, Flotation procedure, Micro-flotation |
| Experimental design | Ranges tested for pH, dosage, temperature | Experimental design, Test conditions, Variables |
| Single-variable tests | One variable changed at a time — EACH test is a separate recipe | Effect of pH, Effect of collector dosage, Effect of depressant dosage, Effect of temperature |
| Results & Discussion | Tables and figures with recovery/grade for EACH condition | Results, Results and discussion |
| Optimization | Best conditions found | Optimization, Optimum conditions, Best results |
| Tables | Structured data — EVERY row in a results table is a potential recipe | Table 1, Table 2, etc. |
| Conclusion | Summary of best recipe | Conclusion, Conclusions |

### What to extract — COMPLETE checklist

For EACH experiment or condition tested, extract ALL of the following:

**Ore characterization:**
- Mineral composition (% SnO₂, % quartz, etc.)
- Ore type / source / deposit name
- Particle size distribution (D80, D50, mesh, μm)
- Specific gravity
- Liberation analysis results

**Flotation conditions:**
- Cell type and volume (e.g., XFG 40ml, Denver D-12)
- Impeller speed (rpm)
- Pulp density (% solids or g in mL)
- Temperature (°C)
- Conditioning time for EACH reagent (minutes)
- Flotation time (minutes)
- Airflow rate (L/min or cm³/min)
- Water type (tap, deionized, process)

**Reagent regime — for EACH reagent, capture:**
- Exact name (commercial and chemical)
- Role (collector, depressant, activator, frother, pH modifier)
- Dosage with unit (g/t, mg/L, mol/L, kg/t)
- Addition order (which reagent added first, second, etc.)
- Conditioning time after addition
- Supplier and purity if stated

**pH control:**
- Target pH value or range
- pH modifier used (NaOH, HCl, H₂SO₄, Na₂CO₃, lime)
- pH modifier dosage if stated
- Whether pH was measured before or after reagent addition

**Results — for EACH experiment (必须标注金属/矿物类别):**
- WHICH mineral/metal the recovery and grade refer to — this is CRITICAL
  - Common: SnO₂, Sn (metallic tin), cassiterite
  - Polymetallic: Cu, Pb, Zn, WO₃, Fe, S, As
  - Look at table headers: "SnO₂ recovery", "Sn grade", "Cu recovery"
  - Look at column units: "% SnO₂", "% Sn", "% Cu"
- Recovery (%) — for each reported mineral
- Concentrate grade (%) — for each reported mineral, with mineral name
- Enrichment ratio
- Mass pull / yield (%)
- Selectivity index if stated
- Whether this is rougher, cleaner, or scavenger result
- Concentrate type: what is the final product called (tin concentrate, bulk sulfide concentrate, Cu-Sn concentrate, etc.)

**Flowsheet:**
- Number of stages (rougher, cleaner, scavenger)
- Recirculation streams
- Regrind between stages
- Combined concentrate results — again, specify which mineral

### How to handle tables

Tables are the richest source of recipe data. When you encounter a table:

1. Read the table caption to understand what variable is being tested
2. Extract EVERY row as a separate experiment/recipe
3. The column headers tell you the fields
4. Include the table number in the notes

Example: A table titled "Effect of SHA dosage on cassiterite flotation" with rows for 200, 400, 600, 800, 1000 g/t → extract 5 separate recipes, each with a different dosage and its corresponding recovery/grade.

### How to handle figures

If the text describes figure data (e.g., "As shown in Fig. 3, recovery reached 78% at pH 5"), extract those values even though you cannot see the figure itself.

### Multiple experiments = multiple recipes

A single paper typically tests:
- Multiple pH values → one recipe per pH
- Multiple collector dosages → one recipe per dosage
- Multiple depressant dosages → one recipe per dosage
- Different reagent combinations → one recipe per combination
- Different ore samples → one recipe per sample
- Rougher vs cleaner stages → one recipe per stage

You MUST extract ALL of them, not just the "best" one.
If the paper reports 15 experiments, you should have close to 15 recipes.

### Reagent name standardization

| Found in text | Standard name |
|---------------|---------------|
| salicylhydroxamic acid, SHA | SHA (salicylhydroxamic acid) |
| benzohydroxamic acid, BHA | BHA (benzohydroxamic acid) |
| styrene phosphonic acid, SPA | SPA (styrene phosphonic acid) |
| sodium hexametaphosphate, SHMP, (NaPO₃)₆ | SHMP (sodium hexametaphosphate) |
| sodium silicate, water glass, Na₂SiO₃ | Water glass (sodium silicate) |
| carboxymethyl cellulose, CMC | CMC (carboxymethyl cellulose) |
| lead nitrate, Pb(NO₃)₂ | Lead nitrate |
| MIBC, methyl isobutyl carbinol | MIBC |

## Step 3: Output

For each paper, you MUST:
1. Extract all experiments
2. Save each experiment as a separate JSON file
3. Run grade + synonym update on each file
4. Present a human-readable summary in chat

### Step 3a: Fill the template

For EACH experiment, create a self-contained JSON following `references/recipe_template.json` (v3 flat format):
- `paper` section: same for all experiments from this paper
- `ore_profile` section: same for all experiments from this paper (copy it into each file)
- `experiment_id`, `experiment_name`, `test_variable`, `data_location`: unique per experiment
- `reagent_scheme`, `flotation_conditions`, `results`: unique per experiment
- Omit null fields to keep JSON small
- Do NOT include `_template_version`, `_description`, `_examples`, `_note` keys

### Step 3b: Save each experiment

One paper with N experiments → N separate files:

```bash
mkdir -p ~/.openclaw/workspace/database/recipes

# Experiment 1
cat > ~/.openclaw/workspace/database/recipes/zhang_2024_exp1.json << 'EOF'
{
  "paper": {"paper_id": "zhang_2024", "title": "...", "year": 2024},
  "experiment_id": "zhang_2024_exp1",
  "experiment_name": "SHA dosage test",
  "ore_profile": { ... },
  "reagent_scheme": [ ... ],
  "results": { ... }
}
EOF

# Experiment 2
cat > ~/.openclaw/workspace/database/recipes/zhang_2024_exp2.json << 'EOF'
{ ... }
EOF
```

### Step 3c: Run post-save pipeline on each file

For each saved file, run grade + synonym update (see "Storage & Auto-grading" section above).

### Step 3d: Human-readable summary in chat

After saving ALL experiments from one paper:

```
===================================================================
论文：{paper title} ({year})
链接：{url}
研究矿石：{ore type, composition}
基础粒度：D80 {d80} μm
提取实验数：{count}
===================================================================

  {filename_1} → [{rating}] {score}/100
    测试变量：{test_variable}
    药剂：{collector} {dosage} g/t, pH {ph}
    结果：[{analyte}] 回收率 {rec}%, 品位 {grade}%

  {filename_2} → [{rating}] {score}/100
    测试变量：{test_variable}
    药剂：{collector} {dosage} g/t, pH {ph}
    结果：[{analyte}] 回收率 {rec}%, 品位 {grade}%

  ...

本文最优：{filename} → [{rating}] {score}/100
```

### Summary

所有论文处理完毕后：

本地PDF模式（Mode A）：
```
---
汇总：
- 接收文件数：{total}
- 成功读取：{success_count}
- 失败：{fail_count} — {list or "无"}
- 提取实验总数：{experiment_count}，来自 {files_with_recipes} 个文件
- 保存文件数：{experiment_count} 个 JSON（每个实验一个文件）
- 保存目录：~/.openclaw/workspace/database/recipes/
- 近义词词典：新增 {n} 条术语
```

搜索模式（Mode B）：
```
---
汇总：
- 接收论文数：{total}
- 成功获取PDF：{fetched}
- 跳过：{skipped} — {list of skipped titles}
- 提取实验总数：{experiment_count}，来自 {papers_with_recipes} 篇论文
- 保存文件数：{experiment_count} 个 JSON
- 保存目录：~/.openclaw/workspace/database/recipes/
- 近义词词典：新增 {n} 条术语
```

## Avoiding output truncation — CRITICAL

JSON output truncation is the #1 failure mode. Follow these rules strictly:

### Rule 1: Write JSON to file FIRST, then summarize in chat

NEVER try to output the full v2 template JSON in chat. Instead:
1. Build the JSON in your working memory
2. Write it directly to the database file using `cat > ... << 'EOF'` or equivalent
3. Then show only the human-readable summary in chat

### Rule 2: One experiment per save — natural batching

Since each experiment is a separate file, there is no need for complex batching.
For a paper with 10 experiments:
1. Extract experiment 1, save `zhang_2024_exp1.json`, grade it, update synonyms
2. Extract experiment 2, save `zhang_2024_exp2.json`, grade it, update synonyms
3. ... repeat for each experiment
4. Show summary at the end

Each file is small (~2-5KB). No truncation risk per file.

### Rule 3: Keep output JSON concise

The template file (`references/recipe_template.json`) contains placeholder structure for your reference. The actual output JSON must be clean:

**Strip from output (template-only keys):**
- `_template_version`
- `_description`

**Strip empty data:**
- Fields with `null` value → omit entirely
- Empty arrays with only placeholder objects → omit (e.g., if no `gangue_minerals`, omit the key)
- Empty `sample_override` → omit if all sub-fields are null
- Empty `flowsheet` → omit if not reported
- Empty `retrieval` → omit (auto-generated later)

**Keep in output:**
- All fields with actual extracted values
- The overall structure: `paper`, `experiment_id`, `ore_profile`, `reagent_scheme`, `results`
- `recommendation_signals` section (even if empty — grading script fills it)

**Example — template has:**
```json
{
  "name": null,
  "name_standard": null,
  "role": null,
  "chemical_formula": null,
  "dosage_gpt": null,
  "environmental_warning": false,
  "note": null
}
```

**Output should only include non-null fields:**
```json
{
  "name": "SHA",
  "name_standard": "salicylhydroxamic acid",
  "role": "collector",
  "dosage_gpt": 800,
  "order": 4
}
```

No null fields. No template-only keys. Smaller JSON = no truncation.

### Rule 4: Never echo the PDF text back

After running fetch_paper.py, you receive the paper text. Do NOT echo it back to the user. Extract data from it silently and output only the structured results.

### Rule 5: One paper at a time

When processing multiple papers:
- Finish one paper completely (extract → save → grade → update synonyms)
- Then move to the next
- Do NOT try to hold all papers in memory simultaneously

## Extraction rules

- You MUST scan the ENTIRE paper text, not just the abstract or conclusion.
- You MUST extract EVERY experiment, not just the best one.
- You MUST extract data from tables — every row is a potential recipe.
- You MUST extract data mentioned in figure descriptions even though you cannot see figures.
- You MUST capture reagent addition order when stated.
- You MUST capture conditioning times for each reagent when stated.
- You MUST only extract values explicitly written in the paper text.
- You MUST NOT guess dosages, pH, or recovery from your training knowledge.
- You MUST NOT fill in "typical" values.
- You MUST NOT merge data from different papers into one recipe.
- You MUST NOT merge data from different experiments in the same paper into one recipe.
- You MUST write "not stated" for any field not found — do NOT omit fields.
- If a recipe mentions lead nitrate, lead ions, or Pb²⁺, you MUST add: `⚠️ Contains lead — environmental risk`
- If a fetched paper contains NO extractable recipe data, you MUST write: `No recipe data found in this paper.`

## EXTRACTION ACCURACY RULE — CRITICAL

- Only extract values that are **explicitly written** in the paper text or tables.
- Do NOT guess, interpolate, or fill in "typical" values from your training knowledge.
- If a value is not stated in the paper, leave it as `null` in the JSON.
- For the `data_location` field in each experiment, note where the data came from (e.g., "Table 3", "Section 3.2") — this is the only location tracking needed.

## VERIFICATION PASS — CRITICAL

After extracting all recipes from a paper, you MUST perform a verification pass.

### Step A: Re-read the text

Go through the paper text again, section by section, and check:

1. **Did I miss any tables?** — Count the tables in the text. Count the tables you extracted from. They should match.
2. **Did I miss any experiments?** — Look for phrases like "Effect of X on Y", "X was varied from A to B", "Tests were conducted at pH 3, 4, 5, 6, 7". Each variation = one recipe.
3. **Did I capture all reagents?** — Look for the Reagents/Chemicals section. Every reagent listed there should appear somewhere in your extracted recipes.
4. **Did I capture the flowsheet?** — Look for "rougher", "cleaner", "scavenger", "stages". If present, did I extract results for each stage?
5. **Did I get the ore characterization?** — Look for mineral composition percentages, XRD results, particle size distribution.

### Step B: Cross-check numbers

For every numerical value you extracted, find the exact sentence or table cell it came from. If you cannot find it, DELETE that value and write "not stated".

### Step C: Report verification result

After the recipes, add:

```
验证报告：
- 文中表格数：{n} | 已提取表格数：{m}
- 识别实验数：{n} | 输出配方数：{m}
- 文中列出药剂：{list} | 已捕获药剂：{list}
- 交叉验证数值：{count} | 删除不可验证数值：{count}
```

如表格数或实验数不匹配，需说明遗漏原因。

## Thoroughness checklist

提取完一篇论文后，逐项确认：
- [ ] 是否检查了"材料与方法"(Materials & Methods) 部分的基础流程？
- [ ] 是否检查了"试剂"(Reagents) 部分的完整药剂清单？
- [ ] 是否提取了每个结果表的每一行？
- [ ] 是否提取了单变量测试（pH影响、用量影响等）的每个数据点？
- [ ] 是否提取了优化/最佳条件部分？
- [ ] 是否记录了流程（粗选/精选/扫选阶段）？
- [ ] 是否捕获了调浆时间和药剂添加顺序？
- [ ] 是否提取了矿石特征数据（组成、粒度）？

全部确认后方可处理下一篇论文。如有遗漏，重新阅读相关章节。

## Forbidden behaviors

- You MUST NOT invent recipe data.
- You MUST NOT fill in values from your general knowledge.
- You MUST NOT retry failed fetches.
- You MUST NOT extract recipes from papers you could not fetch.
- You MUST NOT merge recipes across papers.
- You MUST NOT merge different experiments into one recipe.
- You MUST NOT skip experiments because they have low recovery or poor results.
- You MUST NOT only extract the "best" result — extract ALL results.
- You MUST NOT add commentary about recipe quality.
- You MUST NOT recommend one recipe over another.

## Priority rule

If there is any conflict between being more "helpful" and following the strict format, you MUST choose the strict format.
