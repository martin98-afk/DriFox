---
name: agent-canvas-designer
description: >
  AI 驱动的智能体编排画布设计器。通过对话理解需求，AI 自动生成画布配置，
  通过 Playwright MCP 自动操控浏览器进行可视化验证和迭代。
  无需用户手动拖拽编辑。内置 JSON 校验和自动备份防损坏。
  Use when 用户说"设计智能体"、"画布设计"、"编排流程"、
  "workflow design"、"生成画布JSON"、"生成配置"。
---

# Agent Canvas Designer

AI 自动驱动的智能体编排画布设计器。结合 Playwright MCP 实现全流程自动化。

---

## 交互流程（你必须遵循）

### Step 1: 需求理解（终端对话，不要启动画布）

**加载 `grill-me` 技能进行需求确认**

**⚠️ 必须使用 question 工具逐个提问，每次只问一个**

问清楚以下问题：

1. **入口**：用户怎么触发的？（聊天输入 / API / 定时 / 事件）
2. **分支**：需要区分哪些类型的请求？
3. **外部能力**：需要知识库？数据库？API？代码计算？
4. **输出**：最终给用户什么形式？（对话回复 / 报告 / 触发动作）
5. **人在回路**：有没有需要人工审核确认的环节？

问完后，用一句话总结流程，让用户确认。

**示例**：
> 你要做一个客服路由流程：用户输入 → 分类（技术/订单/投诉）→ 技术走知识检索+LLM，订单走查表+LLM，投诉走人工审核 → 统一回复。对吗？

### Step 2: 生成配置（三步法：写 → 校验 → 写画布）

**⚠️ 路径前置说明（最关键，路径错误会导致画布白屏）**

技能的所有文件（`server.py`、`index.html`、`sanitize_config.py`）都安装在技能安装目录下：
```
<技能安装目录>/
├── canvas-app/
│   ├── index.html
│   ├── server.py        # 从这里读取 config.json
│   └── config.json      # 配置文件必须写到这里
└── tools/
    └── sanitize_config.py  # 校验工具在这里
```

**AI 执行时的当前工作目录（PWD）可能与技能目录不同**，因此：
- **`config.json` 必须写入到技能目录的 `canvas-app/` 下**（因为 `server.py` 从自己所在目录读取配置）
- **`sanitize_config.py` 必须通过技能目录的绝对路径调用**

本文中所有 `SKILL_WORKSPACE` 符号代表技能安装目录，执行时必须**替换为实际的绝对路径**。
> 获取方式：AI 通过 `Skill` 工具加载本技能后，从返回的 `Skill workspace:` 信息中获取路径。

用户确认后，**你必须严格按照这个顺序执行**，每一步都不能跳过。

#### 2a) 组装配置 JSON

根据需求设计节点和连接，组装出完整的配置对象。

**⚠️ JSON 防损坏规则（违反会导致画布白屏）：**
- **所有 `system_prompt` / `user_prompt` 中的中文引号必须用 `「」`**，不要用 ASCII `"`！
- 示例：`判定为「不通过」` ✅  |  `判定为"不通过"` ❌
- 所有 `config` 值必须是字符串：`"5"` ✅  |  `5` ❌
- 所有变量引用必须是 `{{xxx}}` 格式
- **不要使用 CDATA 标签**，直接写入 JSON

#### 2b) 写入配置文件

**⚠️ 路径规则（必须遵守，画布白屏的常见原因）**：

配置文件 `config.json` **必须与 `server.py` 在同一目录**（即技能目录的 `canvas-app/` 下）。

**方式 A（推荐）**：直接写入到技能目录的 `canvas-app/` 下

```xml
<write path="SKILL_WORKSPACE/canvas-app/config.json">
<content>{ ... }</content>
</write>
```

> 将 `SKILL_WORKSPACE` 替换为技能的实际安装路径。

**方式 B**：先写入到当前项目目录，再复制到技能目录

```xml
<write path="canvas-app/config.json">
<content>{ ... }</content>
</write>
<bash command="copy canvas-app\config.json SKILL_WORKSPACE\canvas-app\config.json" />
```

**❌ 错误写法（CDATA 会导致解析失败）**：
```xml
<content><![CDATA[{ ... }]]></content>
```

#### 2c) 运行校验工具（**必须执行，不可跳过**）

```xml
<bash command="python SKILL_WORKSPACE/tools/sanitize_config.py fix SKILL_WORKSPACE/canvas-app/config.json" />
```

> 校验工具 `sanitize_config.py` 在技能目录的 `tools/` 下，必须使用绝对路径。


必须看到 `✅ 检查通过` 或 `✅ 已修复` 才能继续。如果看到 `❌ JSON 语法错误`，修复后再重试。

#### 2d) 启动服务器（如果尚未运行）

```xml
<bg_start command="cd SKILL_WORKSPACE/canvas-app && python server.py" />
```

**方式 B（备选）**：配置文件在当前项目目录，使用 `--config` 指定路径

```xml
<bg_start command="cd SKILL_WORKSPACE/canvas-app && python server.py --config ../../project/canvas-app/config.json" />
```

> `server.py` 在技能目录的 `canvas-app/` 下。默认从自己所在目录读取 `config.json`。
>
> 使用 `--config <路径>` 参数可以让服务器从任意位置读取配置文件，适合保留配置在项目目录的场景。

#### 2e) 用 Playwright 自动刷新 + 截图验证

写入配置后，**用 Playwright 自动刷新浏览器**，确认画布正确渲染：

```xml
<!-- 打开/刷新画布 -->
<mcp__playwright__browser_navigate>
<url>http://localhost:8081</url>
</mcp__playwright__browser_navigate>

<!-- 等待渲染完成 -->
<mcp__playwright__browser_wait_for>
<time>2</time>
</mcp__playwright__browser_wait_for>

<!-- 截图验证 -->
<mcp__playwright__browser_take_screenshot>
<type>png</type>
</mcp__playwright__browser_take_screenshot>

<!-- 读取画布快照确认节点渲染 -->
<mcp__playwright__browser_snapshot></mcp__playwright__browser_snapshot>
```

#### 2f) 通过 `/api/validate` 做结构校验

```xml
<bash command="curl -s http://localhost:8081/api/validate" />
```

期望输出：`{"valid": true, "node_count": 8, "conn_count": 7, "errors": []}`

**⚠️ 常见错误处理**：
- `"节点 n17 没有出边"` → 该节点必须连接到下游节点
- `"节点 xxx 没有入边"` → 检查连接关系

### Step 3: AI 直接修改 config.json 迭代

**所有修改都通过直接编辑 config.json 完成，禁止用 Playwright 去点画布上的节点。**

Playwright 仅用于：改完配置后自动刷新浏览器 + 截图验证。

**路径规则**：配置文件和校验工具都在技能目录下，必须使用绝对路径 `SKILL_WORKSPACE`。

标准迭代循环：

```xml
<!-- 1. 读取当前配置 -->
<read path="SKILL_WORKSPACE/canvas-app/config.json" />

<!-- 2. AI 在内存中构造修改后的配置 -->
<!-- ... 增加/删除/修改节点和连接，修改 prompt ... -->

<!-- 3. 写入到技能目录的 canvas-app 下 -->
<write path="SKILL_WORKSPACE/canvas-app/config.json">
<content>{ ... 修改后的完整配置 ... }</content>
</write>

<!-- 4. 校验（使用技能目录的 sanitize 工具） -->
<bash command="python SKILL_WORKSPACE/tools/sanitize_config.py fix SKILL_WORKSPACE/canvas-app/config.json" />

<!-- 5. 通过 API 校验结构 -->
<bash command="curl -s http://localhost:8081/api/validate" />

<!-- 6. Playwright 自动刷新浏览器 + 截图验证（仅此用途） -->
<mcp__playwright__browser_navigate>
<url>http://localhost:8081</url>
</mcp__playwright__browser_navigate>
<mcp__playwright__browser_wait_for>
<time>2</time>
</mcp__playwright__browser_wait_for>
<mcp__playwright__browser_take_screenshot>
<type>png</type>
</mcp__playwright__browser_take_screenshot>
```

**典型迭代场景**（全部通过改 JSON 实现，不碰浏览器交互）：
- 用户要求增加节点 → 在 JSON 的 nodes 中添加，在 connections 中添加连线
- 用户要求修改 prompt → 直接改对应节点的 system_prompt
- 用户要求增加分支 → 在 classifier/condition 中加分类，加节点和连接
- 用户要求删除节点 → 删除节点和所有相关连线

### Step 4: 输出最终设计

用户确认满意后：
1. 读取最终 config.json（用技能目录的绝对路径）
2. 做自检验收（见下方清单）
3. 运行 `python SKILL_WORKSPACE/tools/sanitize_config.py check SKILL_WORKSPACE/canvas-app/config.json` 做最终校验
4. 输出最终 JSON + 流程说明
5. 关闭服务器

```xml
<bg_stop task_id="bg_xxxxxxxx" />
```

---

## 节点选型决策

### 快速判断（按优先级）

```
入口输入
  ↓
需要分类路由？          → classifier
  ↓ 否
需要查数据库？          → ask-table
  ↓ 否
需要查知识库/FAQ？      → knowledge
  ↓ 否
需要调外部API？         → api-request
  ↓ 否
需要提取文档内容？      → doc-extractor
  ↓ 否
需要从文本提取字段？    → param-extractor
  ↓ 否
需要条件分支？          → condition
  ↓ 否
需要代码计算/转换？     → code
  ↓ 否
需要工具调用/多步推理？ → agent
  ↓ 否
单次推理/分析？         → llm
```

### Agent vs LLM

| 选 agent | 选 llm |
|----------|--------|
| 需要调工具 | 纯文本推理 |
| 多步推理迭代 | 单次输入→输出 |
| 需要结构化 JSON 输出 | 自由文本回答 |

### 14 种节点速查

| type | 图标 | 用途 | config 关键字段 |
|------|------|------|----------------|
| `start` | 🚪 | 流程起点 | （无） |
| `end` | 🏁 | 流程终点 | （无） |
| `llm` | 🧠 | 单次大模型调用 | model, system_prompt, user_prompt |
| `agent` | 🛠️ | 带工具多轮 Agent | model, system_prompt, user_prompt, tools, max_iterations, output_schema |
| `knowledge` | 📚 | 知识库检索 | kb_type, query, top_k, threshold |
| `ask-table` | 📊 | NL→SQL 查表 | table, db_type, question |
| `api-request` | 🌐 | HTTP 接口调用 | method, url, headers, body |
| `reply` | 💬 | 输出回复 | reply_type, content |
| `condition` | 🔀 | 条件分支 | input_var, branches |
| `classifier` | 🏷️ | 问题分类 | input_source, categories, model |
| `code` | ⚡ | Python 代码 | inputs, outputs, code |
| `param-extractor` | 🔍 | 结构化提取 | model, input_text, param_schema |
| `doc-extractor` | 📄 | 文件提取 | source, extract_mode |
| `container` | 👤 | 子画布容器 | container_type, loop_desc |

---

## 代码节点规范（必须遵循）

### 核心规则

code 节点的代码**必须**遵循以下规范：

1. **必须定义 `main()` 函数**，作为入口
2. **`main()` 的入参必须与 `inputs` 字段一一对应**（名称和顺序）
3. **`main()` 的返回值必须是 `dict`**，key 必须与 `outputs` 字段一一对应
4. **只使用 Python 标准库**，避免外部依赖
5. **辅助函数定义在 `main()` 内部**，保持代码自包含
6. **不要在代码中引用未定义的变量**

### inputs/outputs 映射表

| inputs 声明 | outputs 声明 | main 函数签名 |
|------------|-------------|---------------|
| `"a: dict\nb: list"` | `"c: dict"` | `def main(a, b):` |
| `"data: dict"` | `"result: list\ncount: int"` | `def main(data):` |

### 正确示例

```json
{
  "inputs": "fault_analysis: dict\nfeature_params: dict",
  "outputs": "complete_model: dict\nmodel_id: string",
  "code": "def main(fault_analysis, feature_params):\n    import time\n    timestamp = time.strftime('%Y%m%d%H%M%S')\n    \n    complete_model = {\n        'model_id': f'MOD-{timestamp}',\n        'device_type': fault_analysis.get('device_type', ''),\n        'feature_mapping': feature_params.get('feature_mapping', [])\n    }\n    \n    return {\n        'complete_model': complete_model,\n        'model_id': complete_model['model_id']\n    }"
}
```

### 错误示例

```python
# ❌ 没有定义 main 函数
complete_model = {'result': 'xxx'}
return {'complete_model': complete_model}

# ❌ 入参名与 inputs 不对应
def main(data, mode):  # inputs 是 fault_analysis, feature_params
    ...

# ❌ 返回值 key 与 outputs 不对应
def main(fault_analysis, feature_params):
    return {'result': 'xxx'}  # outputs 是 complete_model, model_id
    # 应该 return {'complete_model': ..., 'model_id': ...}

# ❌ 使用未定义的变量
def main(fault_analysis, feature_params):
    return {
        'model_json': model_json,  # ❌ model_json 未定义
        'save_status': save_status  # ❌ save_status 未定义
    }
```

---

## 提示词编写（最关键的部分）

### system_prompt 三段式

每个 llm / agent 节点的 system_prompt **必须**包含三段：

```
你是[角色]。负责[一句话职责]。

需要输出：
1. [字段1]：[类型] — [详细说明]
2. [字段2]：[类型] — [详细说明]

规则：
1. [硬约束]
2. [硬约束]
3. 只输出JSON，不要额外文字
```

### 结构化输出必须提供 JSON 示例

当 llm / agent 节点需要输出结构化 JSON 时，**必须**在 system_prompt 末尾提供输出示例：

```
输出示例：
```json
{
  "results": [
    {"index": 0, "is_fixed": true, "reason": "包含配置参数表"}
  ],
  "count": 1,
  "total": 3
}
```
```

**为什么必须提供示例？**
- LLM 看到示例后输出格式一致性提升 80%+
- 避免字段名拼写错误、类型错误
- 减少"额外文字"等格式问题

### user_prompt 模板

```
[任务一句话描述]

输入：
- [来源1]：{{节点ID.output}}
- [来源2]：{{sys.query}}

输出要求：
[具体期望，引用 system_prompt 中的字段]
```

### 变量引用

| 引用 | 含义 |
|------|------|
| `{{sys.query}}` | 用户原始输入 |
| `{{sys.files}}` | 用户上传的文件 |
| `{{n3.output}}` | 节点 n3 的完整输出 |
| `{{n3.output.field}}` | 节点 n3 输出的指定字段 |

---

## JSON 输出规范

### 完整结构

```json
{
  "version": "2.0",
  "flow": "custom",
  "meta": { "title": "流程名" },
  "nodes": [
    {
      "id": "n1",
      "type": "classifier",
      "label": "意图分类",
      "x": 280, "y": 186,
      "config": {
        "input_source": "{{sys.query}}",
        "categories": "技术问题 → 需要技术背景处理\n业务咨询 → 需要业务知识解答",
        "model": "gpt-4o-mini"
      }
    }
  ],
  "connections": [
    { "sourceId": "n1", "targetId": "n2" }
  ]
}
```

### 硬性约束

| 约束 | 说明 |
|------|------|
| version = "2.0" | 固定 |
| flow = "custom" | 固定 |
| **所有 config 值必须是字符串** | 数字写成 `"5"`，不能写 `5` |
| tools 转义 | `"[\"工具A\",\"工具B\"]"`，不是 `["工具A"]` |
| id 唯一 | `n1, n2, n3...` 递增，不可重复 |
| 坐标 | x ≥ 80, y ≥ 80 |
| 必须有 start 和 end | 每个流程有且仅有一个 |

---

## 自检验收清单

### 写入前检查（AI 手动检查）

| # | 检查项 | 怎么查 |
|---|--------|--------|
| 1 | 有 start 和 end | nodes 中有 type=start 和 type=end |
| 2 | id 不重复 | 每个 id 只出现一次 |
| 3 | 连接闭合 | 所有非 end 节点都有出边 |
| 4 | 连接指向存在 | connections 中所有 id 都在 nodes 中 |
| 5 | config 值全是字符串 | max_iterations 是 `"5"` 不是 `5` |
| 6 | tools 转义 | `"[\"A\",\"B\"]"` 格式 |
| 7 | 变量引用正确 | `{{n3.output}}` 双花括号 |
| 8 | system_prompt 有三段 | 角色→输出→规则 |
| 9 | 每个分支有处理链 | classifier 的每个分类都有下游节点 |
| 10 | **code 节点变量对应** | inputs/outputs 与 main 函数签名一致 |
| 11 | **无孤立节点** | 每个知识库节点都要有下游连接 |

### 写入后自动校验（**必须执行，不可跳过**）

```xml
<bash command="python SKILL_WORKSPACE/tools/sanitize_config.py fix SKILL_WORKSPACE/canvas-app/config.json" />
```

> ⚠️ 注意使用绝对路径：`sanitize_config.py` 在技能目录的 `tools/` 下，`config.json` 在技能目录的 `canvas-app/` 下。

此工具自动检查：
1. ✅ JSON 语法是否合法
2. ✅ 替换中文引号 `"通过"` → `「通过」`
3. ✅ 将非字符串 config 值转为字符串
4. ✅ 报告结构问题（孤立节点、重复 id 等）

---

## 布局规划指南

### 推荐布局结构

**四层水平布局**：
```
┌─────────────────────────────────────────────────────────────────┐
│ 第一层（输入层）：start → 意图理解 → 意图分类                     │
│                                                                     │
│ 第二层（查询层）：知识库检索 → API查询 → 解析                       │
│                                                                     │
│ 第三层（功能层）：功能一~六 垂直排列                                │
│                                                                     │
│ 第四层（输出层）：组装 → JSON输出 → 回复 → end                      │
└─────────────────────────────────────────────────────────────────┘
```

### 布局参数

- **水平间距**：200-240px
- **垂直间距**：80-120px
- **功能模块间距**：140px
- **start 在最左**，**end 在最右**
- **知识库节点**：放在左侧下方，垂直排列
- **API 节点**：放在主流程线上
- **6 个功能模块**：垂直等距排列在功能层

### 避免布局混乱

**❌ 不要这样做**：
- 将功能模块散布在不同高度
- 让连线和节点交叉重叠
- 将知识库节点放在主流程中间

**✅ 应该这样做**：
- 功能模块严格垂直对齐
- 知识库单独一组在左侧
- 连线尽量水平，避免斜线

---

## 常见设计模式

### A: 路由分发型
```
start → classifier → [分支xN] → reply → end
```
适用：多类型用户请求分流

### B: RAG 增强型
```
start → knowledge → llm → reply → end
```
适用：需要领域知识支撑的问答

### C: 多步流水线型
```
start → llm → agent → code → llm → reply → end
```
适用：复杂分析报告生成

### D: API查询型
```
start → intent → api-request → parse → [功能模块xN] → assemble → end
```

### E: 多源融合型
```
start → knowledge ─┐
       api-request ─┤→ agent → reply → end
       ask-table   ─┘
```
适用：多数据源综合决策

---

## 防损坏机制

本技能内置三层防护，防止 JSON 配置损坏：

### 第一层：写入前校验（主动防御）

```xml
<bash command="python SKILL_WORKSPACE/tools/sanitize_config.py fix SKILL_WORKSPACE/canvas-app/config.json" />
```

### 第二层：自动备份（被动恢复）

`server.py` 在每次写入新配置前，自动备份旧配置到 `config.json.bak`。

### 第三层：结构校验 API

```xml
<bash command="curl -s http://localhost:8081/api/validate" />
```

返回格式：
```json
{"valid": true, "errors": [], "node_count": 8, "conn_count": 7}
```

### 常见损坏场景及修复

| 场景 | 现象 | 修复 |
|------|------|------|
| system_prompt 中有 ASCII `"` | 画布白屏 | `fix` 命令自动替换为「」 |
| config 值写成了数字 | 画布正常但参数异常 | `fix` 命令自动转字符串 |
| 节点 id 重复 | 画布显示异常 | 检查 nodes，确保 id 唯一 |
| 缺少 start 或 end | 流程不完整 | 添加缺失的节点 |
| **使用了 CDATA 标签** | **JSON 解析失败** | **移除 CDATA，直接写入 JSON** |
| **孤立节点无出边** | **校验警告** | **连接到下游节点** |
| **code 节点变量未定义** | **运行时报错** | **检查 inputs/outputs 与 main 函数一致** |

---

## Playwright MCP 使用规范

本技能中 Playwright **仅用于**：改完 config.json 后自动刷新浏览器 + 截图验证。

**禁止用途**：用 Playwright 点击画布节点、拖拽连线、编辑配置面板。

### 标准操作

```xml
<!-- 打开/刷新画布 -->
<mcp__playwright__browser_navigate><url>http://localhost:8081</url></mcp__playwright__browser_navigate>

<!-- 等待渲染 -->
<mcp__playwright__browser_wait_for><time>2</time></mcp__playwright__browser_wait_for>

<!-- 截图验证 -->
<mcp__playwright__browser_take_screenshot><type>png</type></mcp__playwright__browser_take_screenshot>
```

---

## 文件位置

```
canvas-app/
├── index.html        # 画布（React Flow CDN，零构建）
├── server.py         # Python 服务器（含备份/校验 API）
├── config.json       # 当前配置（读写这个文件）
└── config.json.bak   # 自动备份（写入前生成）

tools/
└── sanitize_config.py  # JSON 校验与清洗工具

references/
├── NODE-GUIDE.md        # 14种节点详解
├── CONFIG-REFERENCE.md  # 各节点config字段
├── PROMPT-TEMPLATE.md   # 提示词模板与变量
├── OUTPUT-FORMAT.md     # JSON输出规范
├── PITFALLS.md          # 常见错误避坑
└── EXAMPLES/            # 完整示例
```

**标准操作流程**（所有路径使用技能目录的绝对路径 `SKILL_WORKSPACE`）：
```xml
<!-- 1. 写入到技能目录的 canvas-app 下 -->
<write path="SKILL_WORKSPACE/canvas-app/config.json">
<content>{ ... }</content>
</write>

<!-- 2. 校验（必须执行，使用技能目录的 sanitize 工具） -->
<bash command="python SKILL_WORKSPACE/tools/sanitize_config.py fix SKILL_WORKSPACE/canvas-app/config.json" />

<!-- 3. API 验证 -->
<bash command="curl -s http://localhost:8081/api/validate" />
```

**停止服务器**：
```xml
<bg_stop task_id="bg_xxxxxxxx" />
```