# 常见错误与避坑指南

## 节点选型错误

### ❌ 错误：knowledge 用 agent 实现
```json
// 错误做法：把知识检索写成 agent
{"type": "agent", "config": {"tools": "[\"知识检索工具\"]"}}

// ✅ 正确做法：直接用 knowledge 节点
{"type": "knowledge", "config": {"kb_type": "FAQ库", "query": "{{sys.query}}"}}
```

### ❌ 错误：需要工具调用却用 llm
```json
// 错误做法：需要调 API 但用了 llm
{"type": "llm", "config": {"system_prompt": "请调用天气API..."}}

// ✅ 正确做法：改用 agent
{"type": "agent", "config": {"tools": "[\"天气API\"]"}}
```

---

## 配置格式错误

### ❌ 错误：tools 引号不转义
```json
// 错误
{"tools": ["工具A", "工具B"]}

// ✅ 正确
{"tools": "[\"工具A\",\"工具B\"]"}
```

### ❌ 错误：config 字段使用数字而非字符串
```json
// 错误
{"max_iterations": 5, "threshold": 0.7}

// ✅ 正确
{"max_iterations": "5", "threshold": "0.7"}
```

---

## 布局错误

### ❌ 错误：y 坐标 < 0 或超出画布
```json
// 错误
{"y": -50}

// ✅ 正确
{"y": 80}
```

### ❌ 错误：三流程横排太宽
```
// 错误：横向排列导致画布过宽
流程1: n1 → n2 → n3 → ... → n10
流程2: n1 → n2 → n3 → ... → n10
流程3: n1 → n2 → n3 → ... → n10

// ✅ 正确：纵向排列
流程1: n1 → n2 → n3 (y=240)
流程2: n1 → n2 → n3 (y=480)
流程3: n1 → n2 → n3 (y=700)
```

---

## 提示词错误

### ❌ 错误：提示词缺具体输出要求
```markdown
// 错误
你是客服，请处理用户问题。

// ✅ 正确
你是电商客服专家。根据订单信息和用户诉求判断处理方案。

需要输出：
1. intent: 用户意图（退货/换货/咨询/投诉）
2. priority: 优先级（紧急/普通/低）
3. action: 建议处理动作

规则：
1. 涉及金额纠纷标记为紧急
2. 只输出JSON
```

### ❌ 错误：忘记加"只输出JSON"
```markdown
// 错误
请分析并输出结果。

// ✅ 正确
请分析并输出结果。

规则：
1. 只输出JSON，不要额外文字说明
```

### ❌ 错误：输出格式描述模糊
```markdown
// 错误
输出你的分析结果，包括主要发现和建议。

// ✅ 正确
输出：
1. findings: 主要发现（列表，每项50字以内）
2. suggestions: 改进建议（列表，每项100字以内）

格式示例：
{"findings": ["发现1", "发现2"], "suggestions": ["建议1", "建议2"]}
```

---

## 连接错误

### ❌ 错误：end 节点之后还有连接
```json
// 错误：n3 是 end，不应该有出边
{"sourceId": "n3", "targetId": "n4"}

// ✅ 正确：end 节点是终点
```

### ❌ 错误：连接了不存在的节点 id
```json
// 错误：n99 不存在
{"sourceId": "n1", "targetId": "n99"}

// ✅ 正确：所有 targetId 都在 nodes 中存在
```

### ❌ 错误：缺少 start 或 end
```json
// 错误：只有 start 没有 end
// ✅ 正确：每个流程必须有 start 和 end
```

---

## 变量引用错误

### ❌ 错误：变量引用格式错误
```json
// 错误
{"query": "{n2.output.text}"}
{"query": "{{n2.output}}"}
{"query": "$n2.output"}

// ✅ 正确
{"query": "{{n2.output}}"}
{"query": "{{n2.output.text}}"}
```

---

## 自检验查清单

| 检查项 | 检查方法 |
|--------|----------|
| 节点类型正确 | 对照 NODE-GUIDE.md 确认每个节点的 type |
| tools 转义 | 搜索 `"tools":` 确认格式是 `"[\"...\"]"` |
| config 字符串 | 确认所有 config 值都是字符串 |
| 坐标有效 | y ≥ 80, x ≥ 80 |
| 连接闭合 | 所有非 end 节点都有出边 |
| JSON 合法 | 用 JSON.parse() 验证 |

---

## 快速修复指南

### 修复 tools 引号
```javascript
// 原始
tools: ["工具A", "工具B"]

// 修复后
tools: "[\"工具A\",\"工具B\"]"
```

### 修复数字为字符串
```javascript
// 原始
{"max_iterations": 5, "threshold": 0.7}

// 修复后
{"max_iterations": "5", "threshold": "0.7"}
```

### 修复变量引用
```javascript
// 原始
"query": "{n2.output.text}"
"query": "$n2.output"

// 修复后
"query": "{{n2.output.text}}"
```