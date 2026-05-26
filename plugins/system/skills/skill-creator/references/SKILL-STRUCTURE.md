# 技能文件结构规范

## 标准技能结构

```
skill-name/
├── SKILL.md           # 主入口（必需）
├── manifest.json      # 元数据（可选）
└── references/        # 参考文档（按需）
    ├── CONCEPT-A.md
    └── EXAMPLES/
        └── example/
            └── ...
```

## SKILL.md 规范

### 必须包含

```yaml
---
name: skill-name
description: [能力描述]。[能力描述]。Use when [触发场景关键词]。
---

# Skill Name

## Quick Start
[最小可用示例]

## Workflows
[分步流程]

## Advanced
[高级用法或指向详细文档]
```

### 描述字段要求

**格式**：最大 1024 字符，第三人称，简洁有力

**模板**：
```markdown
---
name: skill-name
description: [核心能力1]。[核心能力2]。Use when [关键词1]、[关键词2]。
---
```

**示例**（web-video-presentation）：
```markdown
description: 把一篇文章或口播稿，做成"看起来像视频"的点击驱动 16:9 网页演示，可选合成口播音频。流程：原始文章 → 一次产出口播稿 + outline 开发计划 → 用户一次对齐 5 件事（稿子 / outline / 主题 / 素材 / 开发模式）→ 网页开发（逐章 / 顺序 / 并行）→ 可选音频合成（默认 MiniMax CLI mmx-cli）。outline 只规划节奏与信息密度，不规划动画 —— 动画由章节开发时按 PRINCIPLES + ANTI-AI 法则即时设计。每次点击推进口播稿的一个节拍，每一步独占整屏，进度条平时隐藏只在悬浮时出现。适用场景：用网页做视频（动态 PPT 但不像 PPT）、把口播稿 / 文章变成可交互的解说、为 B 站 / YouTube / 视频号录屏教程、做有电影感的产品 / talk demo。本 Skill 沉淀的是设计方法论 + 协作流程 —— 不绑定任何特定样式 / 字体 / 颜色 —— 因此能复用到任意主题与美学。
---
```

**⚠️ 注意**：这是 1024 字符的示例，写得足够详细但仍在限制内。

### 主入口行数限制

- **建议**：≤100 行
- **上限**：不超过 200 行（如果内容复杂）
- **原因**：Agent 需要快速理解技能意图，过长会影响效率

### 主入口结构模板

```markdown
# Skill Name

## 快速开始
[2-3 行最小可用示例]

## 工作流总览
[流程图或阶段列表]

## 各阶段说明
### Phase 1
[具体步骤]

## 约束与规范
- [约束1]
- [约束2]

## 自检清单
[完成后检查项]

## 相关资源
| 文件 | 用途 |
|---|---|
| [REF.md] | [描述] |
```

## manifest.json 规范

```json
{
  "name": "skill-name",
  "version": "1.0.0",
  "description": "简短描述",
  "author": "optional",
  "tags": ["tag1", "tag2"]
}
```

## references/ 目录

### 命名约定

- 使用 `kebab-case`：如 `SCRIPT-STYLE.md`、`OUTLINE-FORMAT.md`
- 避免空格，使用连字符
- 可以包含子目录 `EXAMPLES/`、`scripts/`

### 何时创建参考文档

| 情况 | 处理 |
|---|---|
| 单个文件 <50 行 | 不拆分，写入主入口 |
| 内容有领域边界 | 拆分为独立文件 |
| 高级功能不常需要 | 拆分，按需引用 |
| 示例结构 | 放入 `EXAMPLES/` |

### 参考文档命名

| 文档类型 | 命名风格 | 示例 |
|---|---|---|
| 概念解释 | `CONCEPT.md` | `AUDIO.md` |
| 格式规范 | `FORMAT.md` | `OUTLINE-FORMAT.md` |
| 风格指南 | `STYLE.md` | `SCRIPT-STYLE.md` |
| 示例目录 | `EXAMPLES/` | `EXAMPLES/hook-chapter/` |

## scripts/ 目录

### 何时需要脚本

- 操作是确定性的（验证、格式化）
- 相同代码会被重复生成
- 错误需要明确处理

### 脚本命名

```text
scripts/
├── extract-narrations.ts  # 提取数据的脚本
├── synthesize-audio.sh    # 合成音频的脚本
└── scaffold.sh            # 脚手架脚本
```

## 常见反模式

### ❌ 反模式 1：描述太模糊

```markdown
# 错误示例
description: Helps with documents.
```

**问题**：Agent 无法区分这个技能和其他文档技能

### ✅ 正确示例

```markdown
# 正确示例
description: 把一篇文章或口播稿做成网页视频演示，可选合成口播音频。Use when 用户提到"做视频"、"录屏演示"、"口播稿"。
```

### ❌ 反模式 2：SKILL.md 过长

- 超过 200 行的主入口
- 大量详细说明放在主入口

### ✅ 正确做法

- >100 行就考虑拆分
- 详细指南放 `references/`
- 主入口只放索引和关键约束

### ❌ 反模式 3：引用层级过深

```markdown
# SKILL.md
└── references/
    └── SUB-REF.md  ← 3 层，查找困难
```

### ✅ 正确做法

- 最多 2 层引用
- 一级引用：直接子文件
- 二级引用：`EXAMPLES/` 下的示例

### ❌ 反模式 4：缺少触发词

**问题**：无法被正确触发

### ✅ 正确做法

```markdown
description: ...。Use when 用户提到 [触发词1]、[触发词2]。
```

## 文件完整性检查

创建技能后自检：

- [ ] SKILL.md 存在且有 description
- [ ] description 包含触发场景（"Use when..."）
- [ ] 主入口 ≤200 行
- [ ] 引用层级 ≤2
- [ ] 无时间敏感信息（如"今年"、"最新的"）
- [ ] 术语前后一致