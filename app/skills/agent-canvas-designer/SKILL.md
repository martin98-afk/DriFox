---
name: agent-canvas-designer
description: >
  可交互的智能体编排画布设计器。通过对话理解需求，启动画布展示流程，
  用户可拖拽编辑，大模型读取反馈迭代。人在回路交互。
  Use when 用户说"设计智能体"、"画布设计"、"编排流程"、
  "workflow design"、"生成画布JSON"、"生成配置"。
---

# Agent Canvas Designer

通过浏览器画布 + 对话交互，设计智能体编排流程。

---

## 交互流程（你必须遵循）

### Step 1: 需求理解（终端对话，不要启动画布）

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

### Step 2: 启动画布 + 生成初始配置

用户确认后，按以下顺序操作：

**1) 启动服务器**
```xml
<bg_start command="cd /d \"D:\work\DriFoxx\.drifox\skills\agent-canvas-designer\canvas-app\" && python server.py" />
```

**2) 写入配置**
```xml
<write path="D:/work/DriFoxx/.drifox/skills/agent-canvas-designer/canvas-app/config.json">
<content><![CDATA[{ ... }]]></content>
</write>
```

**3) 打开浏览器**
```xml
<bash command="start http://localhost:8081" />
```

然后告诉用户：
> 画布已打开！你可以在浏览器中拖拽节点、编辑配置、添加连线。改完告诉我，我会读取你的修改。

### Step 3: 人在回路迭代

用户回复"改好了"或提出修改意见后：

**读取当前状态**：
```xml
<read path="D:/work/DriFoxx/.drifox/skills/agent-canvas-designer/canvas-app/config.json" />
```

根据用户反馈修改配置，重新写入 config.json，画布会自动刷新加载。

**典型迭代**：
- 用户拖了新节点 → 你读取后帮它补齐配置（system_prompt 等）
- 用户修改了连线 → 你读取后检查逻辑是否闭环
- 用户要求增加分支 → 你在 JSON 中添加节点和连接
- 用户说某个节点的 prompt 不对 → 你修改后重新写入

### Step 4: 输出最终设计

用户确认满意后：
1. 读取最终 config.json
2. 做自检验收（见下方清单）
3. 输出最终 JSON + 流程说明
4. 关闭服务器

```xml
<bg_stop task_id="bg_xxxxxxxx" />
```

### Step 5: 模拟运行验证（进阶，可选）

当用户提供了**样例文件**或**测试数据**时，可以进入模拟运行阶段，由 AI 智能体实际执行验证，逐节点验证效果。

**触发条件**：
- 用户主动要求"测试一下"
- 用户提供了样例文件（如 docx/pdf/xlsx）
- 流程中有 code 节点或 llm 节点，需要验证输出格式

**⚠️ 核心：这不是画布运行，而是 AI 用实际工具执行验证**

**操作流程**：

1. **读取当前配置**，确认流程完整
2. **按节点顺序逐个模拟**（AI 用工具执行）：
   - **doc-extractor 节点**：用样例文件实际提取（读取文件、解析内容）
   - **code 节点**：用 `bash` 工具执行 Python，调用 `main()` 函数，传入测试数据，检查输出
   - **llm/agent 节点**：用 `bash` 工具调用 LLM API，传入构造的 prompt，检查输出格式是否符合 JSON 示例
   - **condition 节点**：根据上游输出验证分支逻辑是否正确
   - **api-request 节点**：构造请求体，验证格式是否正确（可跳过实际调用）
3. **标记验证结果**：成功的节点在 config.json 中设置 `validated: true`
4. **输出验证报告**：每个节点的输入/输出/是否通过

**模拟执行示例**：

**code 节点验证** — AI 直接用 bash 执行：
```xml
<bash command="python -c "
import json
# 从画布配置中提取的代码
exec('''
def main(tables, doc_type):
    # ... 代码内容 ...
    return {'html_tables': html_tables, 'total_count': len(html_tables)}
''')
# 构造测试输入
test_tables = [['姓名','年龄'],['张三','25'],['李四','30']]
result = main(test_tables, 'auto')
print(json.dumps(result, ensure_ascii=False, indent=2))
"" />
```

**llm/agent 节点验证** — AI 直接审查 prompt 质量：
- system_prompt 是否遵循三段式（角色→输出→规则）？
- 是否包含 JSON 输出示例？
- 输出字段是否明确定义了类型和说明？
- user_prompt 中的变量引用 `{{n3.output}}` 是否正确指向上游节点？
- 规则是否足够具体，能否避免歧义？
- output_schema 与 system_prompt 中的字段是否一致？

无需实际调用 LLM API，AI 根据自身经验判断 prompt 质量。

**验证通过后更新画布**：
```xml
<read path="D:/work/DriFoxx/.drifox/skills/agent-canvas-designer/canvas-app/config.json" />
<!-- AI 修改对应节点的 validated 为 true -->
<edit path="..." oldString="..." newString="... validated: true ..." />
```

**验证报告格式**：
```
📋 模拟运行报告

| 节点 | 类型 | 输入 | 输出 | 结果 |
|------|------|------|------|------|
| n3 | doc-extractor | 样例.docx | 3个表格 | ✅ |
| n4 | code | 3个表格 | HTML列表 | ✅ |
| n5 | agent | HTML列表 | 判断结果 | ❌ 输出格式不符 |
| n7 | agent | 有效表格 | JSON数据 | ⏭️ 跳过（n5失败） |

❌ n5 问题：缺少 count 字段
💡 建议：在 system_prompt 中增加输出示例
```

**失败处理**：
- AI 分析失败原因 → 修改 prompt/代码 → 重新模拟 → 直到通过
- 通过后 AI 将 `validated: true` 写入 config.json → 画布上节点显示 ✓ 勾选标记

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

### 正确示例

```json
{
  "inputs": "tables: list\ndoc_type: string",
  "outputs": "html_tables: list\ntotal_count: int",
  "code": "def main(tables, doc_type):\n    import re\n    \n    def _table_to_html(table, doc_type):\n        # 内部辅助函数\n        ...\n        return html\n    \n    html_tables = []\n    for idx, table in enumerate(tables):\n        html = _table_to_html(table, doc_type)\n        html_tables.append(html)\n    \n    return {\n        'html_tables': html_tables,\n        'total_count': len(html_tables)\n    }"
}
```

### 错误示例

```python
# ❌ 没有定义 main 函数
html_tables = []
for table in tables:
    html = '<table>...</table>'
    html_tables.append(html)
return {'html_tables': html_tables}

# ❌ 入参名与 inputs 不对应
def main(data, mode):  # inputs 是 tables, doc_type
    ...

# ❌ 返回值 key 与 outputs 不对应
def main(tables, doc_type):
    return {'result': html_tables, 'count': len(html_tables)}
    # outputs 是 html_tables, total_count
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

**⚠️ 常见错误**：模糊的 prompt → 模糊的输出。必须列出具体字段名、类型和详细说明。

### 结构化输出必须提供 JSON 示例

当 llm / agent 节点需要输出结构化 JSON 时，**必须**在 system_prompt 末尾提供输出示例：

```
输出示例：
```json
{
  "results": [
    {
      "index": 0,
      "is_fixed": true,
      "reason": "包含配置参数表",
      "table_name": "系统配置参数"
    }
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

### 详细提示词示例

**意图分类（classifier）**：
```
你是意图分类专家。负责将用户输入分类到预设类别。

需要输出：
1. category: string — 分类结果，必须是以下之一：（技术问题/业务咨询/投诉建议/其他）
2. confidence: float — 置信度，范围 0-1，保留1位小数
3. reasoning: string — 分类理由，30字以内

规则：
1. 严格按照预设类别分类，不得自行创造新类别
2. 置信度低于0.6时，reasoning字段需标注"需要澄清"
3. 技术问题：涉及代码、系统、配置、故障
4. 业务咨询：涉及产品、价格、流程、政策
5. 投诉建议：涉及不满、建议、改进、投诉
6. 只输出JSON，不要额外文字

输出示例：
```json
{
  "category": "技术问题",
  "confidence": 0.9,
  "reasoning": "涉及系统配置问题"
}
```
```

**知识问答（knowledge + llm）**：
```
你是领域知识专家。负责基于检索到的文档回答用户问题。

需要输出：
1. answer: string — 直接回答，50字以内，简洁明确
2. source: string — 依据的文档来源，格式为"文档名-段落号"
3. confidence: string — 置信度，必须是以下之一：（高/中/低）
4. extra_info: string — 补充说明，如无则填空字符串

规则：
1. 只使用检索到的信息回答，不得编造
2. 无法确定的明确回答"无法确定"，confidence设为"低"
3. 高置信度：检索结果直接包含答案
4. 中置信度：检索结果间接推导出答案
5. 低置信度：检索结果无法支撑答案
6. 只输出JSON，不要额外文字

输出示例：
```json
{
  "answer": "最大并发数为1000",
  "source": "系统配置手册-第3.2节",
  "confidence": "高",
  "extra_info": "默认配置为500，可调整"
}
```
```

**表格判断（agent）**：
```
你是表格分析专家。负责遍历HTML表格列表，判断每个表格是否包含"定值"。

定值定义：
- 包含可作为配置、参数、标准、规格的固定数据
- 具有明确的键值对结构或参数表
- 例如：配置参数表、规格表、常数表、系数表、对照表

非定值示例：
- 纯描述性文字表格
- 流程说明表格
- 无固定数值的日志/记录表格

需要输出：
1. results: array — 每个表格的判断结果，每个元素包含：
   - index: int — 表格索引
   - is_fixed: bool — 是否包含定值
   - reason: string — 判断理由，20字以内
   - table_name: string — 建议的表格名称
2. count: int — 包含定值的表格数量
3. total: int — 总表格数量

规则：
1. 包含配置/参数/标准/规格类固定数据 → is_fixed=true
2. 纯描述性/说明性/日志性表格 → is_fixed=false
3. 不确定时优先判断为true
4. table_name要能体现表格内容，如"电压等级对照表"
5. 只输出JSON，不要额外文字

输出示例：
```json
{
  "results": [
    {
      "index": 0,
      "is_fixed": true,
      "reason": "包含电压等级参数表",
      "table_name": "电压等级对照表"
    },
    {
      "index": 1,
      "is_fixed": false,
      "reason": "纯流程说明",
      "table_name": "操作步骤说明"
    }
  ],
  "count": 1,
  "total": 2
}
```
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

### 布局建议

- 水平间距：240px
- 垂直间距：80px
- 分支节点纵向排列，y 间隔 140px
- start 在左，end 在右

---

## 自检验收清单

写入 config.json 前，逐项检查：

| # | 检查项 | 怎么查 |
|---|--------|--------|
| 1 | 有 start 和 end | nodes 中有 type=start 和 type=end |
| 2 | id 不重复 | 每个 id 只出现一次 |
| 3 | 连接闭合 | 所有非 end 节点都有出边 |
| 4 | 连接指向存在 | connections 中所有 id 都在 nodes 中 |
| 5 | config 值全是字符串 | max_iterations 是 "5" 不是 5 |
| 6 | tools 转义 | `"[\"A\",\"B\"]"` 格式 |
| 7 | 变量引用正确 | `{{n3.output}}` 双花括号 |
| 8 | system_prompt 有三段 | 角色→输出→规则 |
| 9 | 每个分支有处理链 | classifier 的每个分类都有下游节点 |

---

## 画布功能（告诉用户的）

| 功能 | 操作 |
|------|------|
| 添加节点 | 左侧工具栏**拖入**画布，或**点击** |
| 编辑配置 | **点击**节点 → 右侧面板编辑 |
| 连接节点 | 从节点右边缘**拖出连线**到左边缘 |
| 删除节点 | 点击节点 → Delete 键或右侧删除按钮 |
| 移动节点 | **拖拽**到新位置（**不保存**） |
| 缩放 | Ctrl+滚轮 或 右下角 +/- |
| 复制内容 | textarea 右上角**复制**按钮 |
| 智能保存 | **只保存**节点增删、参数修改、连线变化；拖拽移动**不保存** |

---

**⚠️ 重要：画布的每次拖拽都会触发保存，但智能体写入的配置只有在用户确认后才覆盖。**

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

### D: 人在回路型
```
start → agent → container → llm → reply → end
```
适用：需要用户确认的生成任务

### E: 多源融合型
```
start → knowledge ─┐
       api-request ─┤→ agent → reply → end
       ask-table   ─┘
```
适用：多数据源综合决策

---

## 文件位置

```
canvas-app/
├── index.html    # 画布（React Flow CDN，零构建）
├── server.py     # Python 服务器
└── config.json   # 当前配置（读写这个文件）

references/
├── NODE-GUIDE.md        # 14种节点详解
├── CONFIG-REFERENCE.md  # 各节点config字段
├── PROMPT-TEMPLATE.md   # 提示词模板与变量
├── OUTPUT-FORMAT.md     # JSON输出规范
├── PITFALLS.md          # 常见错误避坑
└── EXAMPLES/            # 完整示例
```

**读取画布状态**（推荐直接读文件）：
```xml
<read path="D:/work/DriFoxx/.drifox/skills/agent-canvas-designer/canvas-app/config.json" />
```

**写入配置**（画布自动刷新加载）：
```xml
<write path="D:/work/DriFoxx/.drifox/skills/agent-canvas-designer/canvas-app/config.json">
<content><![CDATA[{ ... }]]></content>
</write>
```

**停止服务器**：
```xml
<bg_stop task_id="bg_xxxxxxxx" />
```
