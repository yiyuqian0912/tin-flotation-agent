---
name: recipe-db
description: >
  向量数据库配方推荐系统。基于 FAISS 对矿石特征进行向量化索引（含同义词映射），
  用户输入矿石信息后通过相似度 + 配方评分加权排序，推荐最佳药剂配方。
  Triggers on: recommend recipe, 推荐配方, find similar recipe, 配方推荐,
  what reagent should I use, 用什么药剂, best recipe for my ore,
  similar experiments, 相似实验, build index, 建立索引, update database, 更新数据库.
metadata:
  openclaw:
    emoji: "🔍"
    requires:
      bins: ["python3"]
---

# Recipe DB — 浮选配方向量推荐系统

基于矿石相似度 + 配方评分加权排序的药剂配方推荐。

## Language rule

你必须用中文回复。技术术语使用中英文标注。

## Display rule

You MUST NOT show intermediate script execution details to the user. This includes:
- Raw bash commands being run
- Script stdout/stderr with [INFO], [WARN] prefixes
- JSON output from scripts before formatting
- File paths of temp files

You MUST only show the final formatted result.
If a script fails, show only the user-facing error message.

## 核心原理

```
1. 同义词映射：用户输入 "氧化锡矿" → 标准化为 "oxide tin ore"
2. 向量搜索：只嵌入矿石特征，找相似矿石（不含药剂）
3. 加权排序：final_score = 0.7 × 相似度 + 0.3 × 归一化评分
4. 返回推荐：匹配到的矿石 → 对应的完整药剂配方
```

## Prerequisites

```bash
pip3 install faiss-cpu sentence-transformers
```

### HuggingFace 镜像

```bash
# 方式1：命令行
python3 build_index.py --mirror ...

# 方式2：环境变量（一劳永逸）
echo 'export HF_ENDPOINT=https://hf-mirror.com' >> ~/.zshrc && source ~/.zshrc
```

## 数据流

```
~/.openclaw/workspace/
├── skills/recipe-db/
│   └── scripts/
│       ├── build_index.py
│       ├── query_index.py
│       └── update_synonyms.py    ← 近义词自动更新脚本
└── database/
    ├── recipes/                   ← 配方 JSON（v2模板）
    ├── synonyms.json              ← 近义词词典（自动增长）
    └── faiss_index/               ← FAISS 索引
        ├── recipes.index
        └── metadata.pkl
```

## 近义词词典

文件位置：`~/.openclaw/workspace/database/synonyms.json`

### 自动增长

每次 recipe-extractor 提取新配方时，自动扫描 JSON 中出现的所有术语（矿物名、药剂名、矿石类型、工艺阶段）并添加到词典。

- 已知中文术语自动映射为英文标准名（如 "石英" → "quartz"）
- 未知术语以 identity 形式加入（如 "新药剂X" → "新药剂x"），用户日后可手动编辑 value
- 已存在的条目不会被覆盖

### 手动编辑

用户可直接编辑 `synonyms.json` 来：
- 修正自动映射错误
- 为新术语指定标准名称
- 添加特定领域术语

### 词典结构

```json
{
  "ore_type": {
    "氧化锡矿": "oxide tin ore",
    "细粒锡石": "fine grained cassiterite"
  },
  "minerals": {
    "石英": "quartz",
    "cassiterite": "cassiterite"
  },
  "reagents": {
    "水杨羟肟酸": "SHA",
    "sodium silicate": "sodium silicate"
  },
  "process": {
    "粗选": "rougher"
  },
  "pain_points": {
    "回收率低": "low recovery"
  }
}
```

### 手动更新词典

```bash
python3 SKILL_DIR/scripts/update_synonyms.py \
  --dir ~/.openclaw/workspace/database/recipes \
  --dict ~/.openclaw/workspace/database/synonyms.json 2>/dev/null
```

## 加权排序

```
final_score = similarity_weight × cosine_similarity
            + score_weight × normalized_recipe_score
```

默认权重：`similarity=0.7, score=0.3`

算法：
1. 用矿石向量搜索 top-k×5 候选
2. 将候选中的 recipe_score 归一化到 [0, 1]
3. 加权合并 → 按 final_score 降序
4. 返回 top-k

这确保：
- 矿石很相似但评分差的配方会被降权
- 矿石较相似且评分高的配方会被提升
- 用户可通过 `--similarity-weight` 和 `--score-weight` 调节偏好

---

## 建库模式

### Command

```bash
python3 SKILL_DIR/scripts/build_index.py \
  --recipes-dir ~/.openclaw/workspace/database/recipes \
  --mirror \
  --rebuild 2>/dev/null
```

- `--mirror`: 使用 hf-mirror.com
- `--rebuild`: 强制重建
- `--synonyms PATH`: 自定义同义词文件

### Report

```
✅ 向量索引已建立：
- 索引位置：~/.openclaw/workspace/database/faiss_index/
- 处理论文数：{files_processed}
- 索引实验数：{total_experiments}
- 含评分实验数：{experiments_with_score}
- 索引内容：矿石特征（同义词标准化后）
- 嵌入模型：all-MiniLM-L6-v2

矿石特征示例：
1. "{sample_ore_text_1}"
2. "{sample_ore_text_2}"
```

---

## 推荐模式

### Step 1: 收集矿石参数

至少需要 ore_type。参数越全匹配越精准。支持中文输入（自动映射）。

| 参数 | 示例（中文自动映射） |
|------|---------------------|
| ore_type | 氧化锡矿 → oxide tin ore |
| gangue | 石英 → quartz |
| gangue_secondary | 方解石 → calcite |
| d80_um | 45 |
| sno2_pct | 1.2 |
| associated | 黄铁矿 → pyrite |
| pain_point | 回收率低 → low recovery |

### Step 2: 运行查询

```bash
python3 SKILL_DIR/scripts/query_index.py \
  --query '{"ore_type":"氧化锡矿","gangue":"石英","d80_um":"45"}' \
  --top-k 5 \
  --similarity-weight 0.7 \
  --score-weight 0.3 \
  --mirror 2>/dev/null
```

### Step 3: 呈现推荐

```
🔍 基于矿石相似度 + 配方评分，推荐以下药剂配方：

您的矿石（标准化后）：
  {query_text_normalized}

排序权重：相似度 {similarity_weight} + 评分 {score_weight}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

推荐 1（综合分：{final_score} = 相似度 {similarity} + 评分贡献 {score_contribution}）
📄 来源：{paper_title} ({year})
🔬 实验：{experiment_name}
📁 文件：{filename}
🏆 论文最优：{是/否}
⭐ 配方评分：{recipe_score}/100 [{grade_rating}]

匹配矿石特征：
  {ore_matched_on}

推荐药剂制度：
  pH调整：{modifier} → pH {value}
  抑制剂：{name} — {dosage} g/t（针对 {target}）
  活化剂：{name} — {dosage} g/t    ⚠️ 含铅盐（如有）
  捕收剂：{name} — {dosage} g/t
  起泡剂：{name} — {dosage} g/t

实验结果：
  [{analyte}] 回收率：{recovery}%，品位：{grade}%
  阶段：{stage}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

推荐 2 ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

汇总：
- 数据库实验总数：{index_size}
- 返回推荐数：{results_returned}
- 排序方式：{similarity_weight}×相似度 + {score_weight}×评分
```

### Recommendation rules

- 你必须运行 query_index.py。不能凭记忆推荐。
- 你必须展示 final_score 的分解（相似度 + 评分贡献）。
- 你必须展示匹配到的矿石特征（ore_matched_on）。
- 你必须展示完整药剂制度和实验结果。
- 含铅药剂标注：⚠️ 含铅盐 — 环保风险
- 论文最优实验加标 🏆。

## Index not found

```
❌ 向量索引未找到。

1. 确认 recipes/ 中有配方 JSON
2. 运行：建立索引（国内加 --mirror）

如果 recipes 目录为空，先用 tin-consultant 搜索论文并用 recipe-extractor 提取配方。
```

## 词典文件未找到

如果 `~/.openclaw/workspace/database/synonyms.json` 不存在，脚本仍可运行（跳过映射），但会提示：
```
⚠️ 近义词词典未找到，搜索将不进行术语标准化。
建议先用 recipe-extractor 提取至少一篇论文，词典会自动创建。
或手动运行：python3 SKILL_DIR/scripts/update_synonyms.py --dir recipes/ --dict synonyms.json
```

## Forbidden behaviors

- 不能在没有运行脚本的情况下推荐配方。
- 不能从训练知识中编造推荐。
- 不能修改 final_score 或相似度。
- 不能隐藏低分推荐。
- 不能合并多个推荐为一个。
