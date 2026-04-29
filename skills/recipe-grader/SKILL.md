---
name: recipe-grader
description: >
  Grade and rank flotation reagent recipes on a 100-point scale.
  Use after recipes have been extracted, when the user wants to evaluate,
  compare, rank, score, or grade recipes.
  Triggers on: grade recipe, score recipe, rank recipes, evaluate recipe,
  compare recipes, which recipe is best, rate recipe, 评分, 打分, 评价配方.
metadata:
  openclaw:
    emoji: "📊"
    requires:
      bins: ["python3"]
---

# Recipe Grader

Grade flotation recipes on a 100-point scale across 4 dimensions.

## Language rule

You MUST respond in Chinese (中文) by default.
Technical terms MUST be written in Chinese with English in parentheses.
Paper titles and reagent names follow the same bilingual format.
The JSON report stays in English (machine-readable).


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

You MUST convert extracted recipes into JSON, run the grading script, and present the scores. You MUST NOT grade recipes manually or from your own knowledge.

## Scoring dimensions (100 points total)

| Dimension | Points | What it measures |
|-----------|--------|------------------|
| Completeness | 25 | How many fields are filled (collector, dosage, pH, recovery, grade, etc.) |
| Chemical logic | 30 | pH-collector compatibility, dosage ranges, antagonism checks |
| Results quality | 25 | Recovery rate and concentrate grade values |
| Safety | 20 | Presence of hazardous reagents (lead, mercury, chromate) |

## Rating scale

| Score | Rating | Meaning |
|-------|--------|---------|
| 75–100 | A | Strong recipe — recommended for experimental validation |
| 60–74 | B | Decent recipe — consider with modifications |
| 45–59 | C | Weak recipe — significant gaps or concerns |
| 0–44 | D | Poor recipe — insufficient data or major issues |

## Step 1: 运行评分（写回原文件）

评分脚本直接读取 recipe JSON，评分后写回同一文件。不生成独立报告文件。

**单个文件：**
```bash
python3 SKILL_DIR/scripts/grade_recipe.py --file ~/.openclaw/workspace/database/recipes/zhang_2024.json --merge 2>/dev/null
```

**批量评分整个目录：**
```bash
python3 SKILL_DIR/scripts/grade_recipe.py --dir ~/.openclaw/workspace/database/recipes --merge 2>/dev/null
```

Replace `SKILL_DIR` with the absolute path to this skill's directory.

`--merge` 将评分写入每个实验的 `recommendation_signals` 字段，包括：
- `recommendation_score`: 0-100 分
- `grade_rating`: A/B/C/D
- `grade_verdict`: 中文评定
- `is_promising`: true/false
- `grade_breakdown`: 四维度详细扣分
- `grade_deductions`: 所有扣分列表
- `total_deducted`: 总扣分
- `key_results`: 回收率和品位

评分结果与配方数据在同一个 JSON 文件中，不产生额外文件。

## Step 2: 展示结果

You MUST do TWO things:
1. Show a human-readable report in chat
2. Confirm scores have been written into the source JSON files

### Part A: 评分报告（可读版）

按分数从高到低展示：

```
配方评分报告（按分数排序）：

1. [A] 83/100 — SHA dosage optimization
   来源：Zhang et al. 2024 | 文件：zhang_2024.json
   扣分合计：17 分

   完整性 (completeness)：25/25
   （无扣分）

   化学逻辑 (chemical_logic)：27/30
   - -3 分 | 回收率 | 回收率 72.5% 良好但低于 80%

   结果质量 (results_quality)：21/25
   - -4 分 | 品位 | 品位 38.2% 中等 (低于 40%)

   安全性 (safety)：10/20
   - -10 分 | 危险药剂 | 含有危险药剂: lead — 环保及法规风险

   关键结果：[SnO2] 回收率 72.5%，品位 38.2%
   评定：优秀配方 — 建议实验验证

---

2. [B] 63/100 — incomplete test
   来源：Zhang et al. 2024 | 文件：zhang_2024.json
   扣分合计：37 分

   完整性 (completeness)：15/25
   - -2 分 | depressant | depressant 未提及
   - -4 分 | pH value | pH value 未提及
   - -3 分 | Sn grade | Sn grade 未提及
   - -1 分 | reagent addition order | reagent addition order 未提及

   化学逻辑 (chemical_logic)：20/30
   - -10 分 | pH-捕收剂匹配 | pH 未提及 — 无法评估

   结果质量 (results_quality)：8/25
   - -9 分 | 回收率 | 回收率 55% 偏低 (低于 60%)
   - -7 分 | 品位 | 品位未提及 — 无法评估

   安全性 (safety)：20/20
   （无扣分）

   关键结果：[Sn] 回收率 55%，品位 未提及
   评定：较好配方 — 可考虑修改后验证

---
汇总：
- 评分实验数：{total}（来自 {files} 个文件）
- A级 (75+)：{count}
- B级 (60-74)：{count}
- C级 (45-59)：{count}
- D级 (0-44)：{count}
- 平均分：{avg}/100
- 论文标注最优实验数：{best_reported_count}
```

### Part B: 确认写入

评分已写入原 JSON 文件，无独立报告文件。确认：

```
✅ 评分已写入原文件：
- 处理文件数：{files_processed}
- 评分实验数：{total_experiments}
- 平均分：{average}/100
- 最高分：{best}/100
- 评分位置：每个实验的 recommendation_signals 字段

已评分文件：
- ~/.openclaw/workspace/database/recipes/{filename1}
- ~/.openclaw/workspace/database/recipes/{filename2}
```

### Output rules

- You MUST show EVERY deduction from the script output. Do NOT omit any.
- For each dimension, if there are no deductions, write `(no deductions)`.
- You MUST show the deducted points, the field name, and the reason for EACH deduction.
- You MUST show "Total deducted: X points" for each recipe.
- You MUST preserve the score ordering (highest first).
- You MUST show the full breakdown for EVERY recipe, not just the top one.
- You MUST always provide the full JSON output after the human-readable report.
- You MUST NOT truncate or summarize the JSON. Show the complete output.

## Grading rules

- You MUST run the grading script. You MUST NOT assign scores manually.
- You MUST show ALL deductions returned by the script — every single one.
- You MUST preserve the score ordering (highest first).
- You MUST show the full breakdown for every recipe, not just the top one.
- You MUST NOT change or override scores returned by the script.
- You MUST NOT suppress or summarize deductions. Show each one individually.
- You MUST NOT add your own commentary about which recipe to use. The scores speak for themselves.
- If the user asks "which is best", point to the highest-scored recipe and its deductions.

## Forbidden behaviors

- You MUST NOT invent scores.
- You MUST NOT score recipes without running the script.
- You MUST NOT adjust scores based on your own judgment.
- You MUST NOT omit low-scoring recipes from the output.
- You MUST NOT fill in missing recipe data to improve a score.

## Priority rule

If there is any conflict between being more "helpful" and following the strict format, you MUST choose the strict format.
