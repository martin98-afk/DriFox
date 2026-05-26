# Web Video Presentation 设计模式分析

这是 skill-creator 分析 `web-video-presentation` 技能后提取的设计模式。

## 技能概览

**用途**：把文章或口播稿做成可录屏的网页视频演示

**触发词**：做视频、口播稿、录屏演示、动态 PPT

## 设计模式提取

### 模式 1：阶段 + 硬性 Checkpoint

```
Phase 1 内容编写
   1.1 识别用户输入
   1.2 一次产出 script.md + outline.md
        ↓
[Checkpoint Plan] ← 必须停！一次对齐 5 件事
        ↓
Phase 2 网页开发
   2.1 脚手架
   2.2 第 1 章（强制 anchor）
        ↓
   [硬节点] 用户验收第 1 章 ← 不可跳过！
        ↓
   2.3 第 2~N 章
        ↓
[Checkpoint Audio]
        ↓
Phase 3 音频合成
        ↓
Phase 4 录屏+后期
```

**关键发现**：
- 4 个阶段，2 个强制 Checkpoint
- 第 1 章必须完整验收（锚点）
- Checkpoint 是防止走偏的机制

### 模式 2：分层文档架构

```
SKILL.md (440行)
    ↓ 按需引用
references/
├── SCRIPT-STYLE.md (404行) — Phase 1.2 必读
├── OUTLINE-FORMAT.md (203行) — Phase 1.2 必读
├── CHAPTER-CRAFT.md (224行) — Phase 2.4 每章必读
├── THEMES.md (324行) — 选主题时读
├── AUDIO.md — Phase 3 才读
└── RECORDING.md — Phase 4 才读
```

**设计原理**：
- Phase 2.4 会重复 N 次 → 单一入口文件
- 不同阶段读不同文件
- "何时读"标注避免信息过载

### 模式 3：单一真相源

```markdown
narrations.ts 是 step 数和音频合成的唯一真相源。

约束：章节 .tsx 里的 if (step === N) 出现的最大 N + 1 
必须等于 narrations.length。
```

**保证五处永不漂**：
1. script.md（口播稿节拍）
2. outline.md（开发计划）
3. 章节 .tsx（视觉实现）
4. chapters.ts（注册表）
5. 音频文件（合成输出）

### 模式 4：口述检查清单

```markdown
## 完工自检（完成后强制执行）

> ⚠️ 硬性流程：完成后必须走自检 → 修复 → 汇报 三步。

**执行方式**（按能力降级）：
1. Agent Teams（最优）
2. subAgent（次优）
3. 自检（兜底）

拿到结论后：先改完，再汇报。
```

**清单项示例**（CHAPTER-CRAFT.md）：
- 每章至少 1~2 处视觉演示
- 不同 step 的主导动作不一样
- 颜色和字体家族走 token
- narrations.ts 存在且正确

### 模式 5：降级策略

```markdown
**执行方式**（按能力降级）：
1. Agent Teams（最优）— 需要 Teams 能力
2. subAgent（次优）— 能开子智能体
3. 自检（兜底）— 当前 agent 自己执行
```

## 文件结构

```
web-video-presentation/
├── SKILL.md
├── references/
│   ├── SCRIPT-STYLE.md     # 口播稿风格指南
│   ├── OUTLINE-FORMAT.md   # 大纲格式规范
│   ├── CHAPTER-CRAFT.md    # 章节开发指引
│   ├── THEMES.md           # 主题系统
│   ├── AUDIO.md            # 音频合成
│   └── RECORDING.md        # 录屏指南
├── scripts/
│   └── scaffold.sh         # 脚手架脚本
└── themes/
    ├── terminal-green/
    ├── paper-press/
    └── ...（内置主题）
```

## 核心约束

| 约束 | 说明 |
|---|---|
| 16:9 固定舞台 | 内容 1920×1080，transform scale |
| 全局 step 计数器 | 章节是 step 的纯函数 |
| 每步独占整屏 | if (step === N) return <FullScene /> |
| 颜色走 token | 禁止硬编码 hex/rgb |
| 字体走 token | 禁止硬编码字体名 |
| narrations.ts 唯一性 | step 数 = 数组长度 |

## 可复用要素

| 要素 | 适用场景 |
|---|---|
| 阶段 + Checkpoint | 复杂多阶段任务 |
| 单一真相源 | 多文件协作项目 |
| 按需渐进披露 | 大型技能的信息管理 |
| 自检协议 | 交付质量保障 |
| 降级策略 | 容错性和鲁棒性 |
| 约束 + 自由度 | 主题适配系统 |