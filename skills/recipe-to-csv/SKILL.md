---
name: recipe-to-csv
description: >
  将配方 JSON 导出为机器学习推荐模型训练用 CSV 数据集。
  生成 recipe_recommendation_train.csv，每行 = 一种矿石 + 一套配方 + 一组条件 + 结果。
  Triggers on: export csv, 导出数据集, save dataset, convert to csv,
  机器学习数据集, ML dataset, build dataset, 生成CSV, 导出CSV, 训练数据.
metadata:
  openclaw:
    emoji: "📋"
    requires:
      bins: ["python3"]
---

# Recipe to CSV — 推荐模型训练数据导出

将 v2 模板配方 JSON（含评分）转换为推荐系统训练表。

核心思路：**"对于这种矿石，这套配方表现如何。"**

## Language rule

你必须用中文回复。技术术语使用中英文标注。

## Display rule

You MUST NOT show intermediate script execution details to the user. This includes:
- Raw bash commands being run
- Script stdout/stderr with [INFO], [WARN] prefixes
- JSON output from scripts before formatting
- File paths of temp files

You MUST only show the final formatted result.
While scripts are running, you may show "正在导出数据集..." but nothing more.
If a script fails, show only the user-facing error message.

## 数据集设计

### 一行代表什么

```
一行 = 一种矿石 + 一套药剂配方 + 一组工艺条件 + 一次实验结果 + 评分
```

### 推荐系统用法

```
用户输入矿石信息 → 枚举候选配方 → 模型预测 recipe_score → 推荐 Top-K
```

### 文件

| 文件 | 用途 |
|------|------|
| `recipe_recommendation_train.csv` | 推荐模型训练表（100列）|

### 列分组（100 列）

| 组 | 列数 | 用途 | 推荐角色 |
|---|------|------|---------|
| 标识 (ID) | 4 | case_id, paper_id, experiment_id, name | 追溯 |
| 矿石特征 (Ore) | 29 | 矿种、脉石、粒度、品位、布尔标志 | **模型输入** |
| 配方 (Recipe) | 30 | 捕收剂×2、抑制剂×2、活化剂、分散剂、絮凝剂、起泡剂、硫化剂 | **推荐目标** |
| 工艺条件 (Conditions) | 18 | pH、温度、浓度、调浆时间、浮选时间 | **推荐目标** |
| 结果 (Results) | 10 | 有价矿物回收率/品位 + 脉石回收率/品位 | **监督信号** |
| 推荐标签 (Labels) | 5 | recipe_score、rank、is_best | **训练标签** |
| 质量溯源 (Quality) | 4 | 来源、置信度、是否论文最优 | 过滤/加权 |

### 关键设计

**药剂用量拆分为 value + unit：**
```
collector_dosage_value = 800
collector_dosage_unit = g/t
```
因为文献混用 g/t、mg/L、mol/L、kg/t，拆分后可统一或保留原始单位。

**结果统一为 valuable vs gangue：**
```
valuable_analyte = SnO2
valuable_recovery_pct = 72.5
valuable_grade_value = 38.2
gangue_analyte = SiO2
gangue_recovery_pct = 15.3
```
不再用 product_1/2/3，直接对应推荐目标。

**自动计算推荐标签：**
```
selectivity_score = valuable_recovery - gangue_recovery
recipe_score = 0.45 × recovery + 0.35 × grade - 0.20 × gangue_recovery
recipe_rank_in_experiment_group = 同一论文内排名
is_best_in_experiment_group = 1/0
```

**矿石布尔标志（自动推导）：**
```
fine_grained_flag       — D80 ≤ 38μm 或文本含 "fine"
ultrafine_flag          — D80 ≤ 20μm
carbonate_gangue_flag   — 脉石含方解石/白云石
silicate_gangue_flag    — 脉石含石英/长石
sulfide_associated_flag — 共生硫化矿
oxide_ore_flag          — 氧化矿
```

### 缺失值

- 空字符串 `""` = 未提及
- 不使用 0（0 有实际含义）
- pandas: `pd.to_numeric(errors='coerce')`

### 标签字段注意

以下字段是训练标签，**不能作为预测输入**：
- `valuable_recovery_pct`, `valuable_grade_value`
- `recipe_score`, `selectivity_score`
- `recipe_rank_in_experiment_group`, `is_best_in_experiment_group`
- `is_promising`

## Step 1: 运行导出

**从整个 recipes 目录导出（推荐）：**
```bash
python3 SKILL_DIR/scripts/recipes_to_csv.py \
  --dir ~/.openclaw/workspace/database/recipes \
  --output-dir ~/.openclaw/workspace/database/datasets 2>/dev/null
```

**从单个文件导出：**
```bash
python3 SKILL_DIR/scripts/recipes_to_csv.py \
  --file ~/.openclaw/workspace/database/recipes/zhang_2024.json \
  --output-dir ~/.openclaw/workspace/database/datasets --append 2>/dev/null
```

- `--append`: 追加到已有 CSV，不覆盖
- `--output-dir`: 输出目录，CSV 文件名固定

## Step 2: 确认

```
✅ 推荐模型训练数据已导出：

文件：~/.openclaw/workspace/database/datasets/recipe_recommendation_train.csv
- 行数：{rows}（每行 = 一个实验）
- 列数：100
- 来源文件数：{files}
- 含评分实验数：{scored}
- 组内最佳实验数：{best_count}

建议后续步骤：
  import pandas as pd
  df = pd.read_csv("recipe_recommendation_train.csv")

  # 数值列转换
  num_cols = ['d80_um','collector_dosage_value','ph_target',
              'valuable_recovery_pct','recipe_score']
  df[num_cols] = df[num_cols].apply(pd.to_numeric, errors='coerce')

  # 过滤高质量样本
  good = df[df['extraction_confidence'].isin(['high','medium'])]

  # 查看组内最佳
  best = df[df['is_best_in_experiment_group'] == '1']
```

## 追加模式

```
第1次：5篇论文 → recipe_recommendation_train.csv (30行)
第2次：3篇新论文 → --append → (48行)
第3次：上传本地PDF → --append → (63行)
```

## Rules

- 你必须运行脚本导出，不能手动拼 CSV。
- 默认输出至 `~/.openclaw/workspace/database/datasets/`。
- 导出后必须展示统计信息。
- 不能填充缺失值。
- 不能更改列名或列顺序。
- 不能删除任何行（包括低分行）。
- 追加模式不重复写表头。
- 标签字段不能混入推荐输入特征说明中。
