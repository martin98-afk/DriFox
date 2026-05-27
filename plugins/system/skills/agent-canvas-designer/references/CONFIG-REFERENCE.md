# 配置字段速查

## start
```json
{
  "params": "sys.query = 用户输入"
}
```

---

## end
```json
{}
```

---

## llm / agent

### llm 基础配置
```json
{
  "title": "节点名",
  "model": "gpt-4",
  "system_prompt": "你是[角色]。负责[一句话]。\n\n需要输出：\n1. [字段1]：[说明]\n2. [字段2]：[说明]\n\n规则：\n1. [硬约束]\n2. 只输出JSON",
  "user_prompt": "[任务描述]\n\n输入：\n- 用户输入：{{sys.query}}\n- 上游输出：{{n2.output}}\n\n输出要求：[具体格式]"
}
```

### agent 完整配置
```json
{
  "title": "节点名",
  "model": "gpt-4",
  "system_prompt": "你是[角色]...",
  "user_prompt": "...",
  "tools": "[\"工具A\",\"工具B\",\"工具C\"]",
  "max_iterations": "5",
  "output_schema": "{\"field1\":\"说明\",\"field2\":\"说明\"}"
}
```

**⚠️ 关键约束**：
- tools 内部引号必须 `\"` 转义
- max_iterations 必须指定，控制成本

---

## knowledge
```json
{
  "kb_type": "知识库名称",
  "query": "{{变量}} 检索词",
  "top_k": "5",
  "threshold": "0.7"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| kb_type | string | 知识库标识名称 |
| query | string | 检索表达式，支持变量插值 |
| top_k | string | 返回最相关结果数量 |
| threshold | string | 相似度阈值（0-1） |

---

## ask-table
```json
{
  "table": "表名",
  "db_type": "MySQL",
  "question": "自然语言查询"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| table | string | 目标表名 |
| db_type | string | 数据库类型（MySQL/PostgreSQL/SQLite） |
| question | string | 自然语言问题 |

---

## api-request
```json
{
  "method": "GET",
  "url": "https://api.example.com/data",
  "headers": "{\"Authorization\": \"Bearer xxx\"}",
  "body": "{\"query\": \"{{n2.output}}\"}"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| method | string | HTTP 方法（GET/POST/PUT/DELETE） |
| url | string | API 地址 |
| headers | string | 请求头（JSON 字符串） |
| body | string | 请求体（JSON 字符串，支持变量） |

---

## reply
```json
{
  "reply_type": "直接回复",
  "content": "✅ 任务完成\n\n📊 结果：{{n3.output.result}}\n\n{{n4.output.suggestion}}"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| reply_type | string | 回复类型（直接回复/模板回复） |
| content | string | 回复内容，支持 {{变量}} 插值 |

---

## condition
```json
{
  "input_var": "{{n4.output.status}}",
  "branches": "成功 → success\n失败 → failure\n超时 → timeout\n默认 → default"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| input_var | string | 条件判断变量 |
| branches | string | 分支定义（每行：标签 → 条件） |

---

## classifier
```json
{
  "input_source": "{{sys.query}}",
  "categories": "技术问题 → 需要技术背景处理\n业务咨询 → 需要业务知识解答\n投诉建议 → 需要人工介入\n其他 → 标准回复",
  "model": "gpt-4o-mini"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| input_source | string | 分类输入源 |
| categories | string | 分类类别定义 |
| model | string | 分类模型（可选，默认 gpt-4o-mini） |

---

## code
```json
{
  "inputs": "history_data: list\ncurrent_threshold: float",
  "outputs": "recommended_threshold: float\nconfidence_interval: list\nstability_score: float",
  "code": "def main(history_data, current_threshold):\n    import statistics\n    values = [d['value'] for d in history_data if d.get('value')]\n    if not values:\n        return {'recommended_threshold': current_threshold, 'confidence_interval': [0, 0], 'stability_score': 0}\n    m = statistics.mean(values)\n    s = statistics.stdev(values) if len(values) > 1 else 0\n    lower = m - 2 * s\n    upper = m + 2 * s\n    stability = max(0, 1 - s / (m + 0.001))\n    return {\n        'recommended_threshold': round(m + 0.5 * s, 2),\n        'confidence_interval': [round(lower, 2), round(upper, 2)],\n        'stability_score': round(stability, 4)\n    }"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| inputs | string | 输入参数定义（每行：名称: 类型） |
| outputs | string | 输出参数定义（格式同 inputs） |
| code | string | Python 代码，必须包含 main 函数 |

**⚠️ 代码规范**：
- **必须**定义 `main(inputs...)` 函数，作为唯一入口
- `main()` 的入参名和顺序**必须**与 `inputs` 字段一一对应
- 返回值**必须**是 dict，key **必须**与 `outputs` 字段一一对应
- 辅助函数定义在 `main()` 内部，保持代码自包含
- 只使用 Python 标准库，避免外部依赖

---

## param-extractor
```json
{
  "model": "gpt-4",
  "input_text": "{{n2.output}}",
  "param_schema": "{\"mechanism_level\":\"故障严重程度\",\"mechanism_phenomenon\":\"异常现象\"}"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| model | string | 提取模型 |
| input_text | string | 待提取的文本 |
| param_schema | string | 参数 Schema（JSON 字符串） |

---

## doc-extractor
```json
{
  "source": "sys.files",
  "extract_mode": "全文提取"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| source | string | 文件来源（sys.files） |
| extract_mode | string | 提取模式（全文提取/关键信息提取） |

---

## container
```json
{
  "container_type": "迭代",
  "loop_desc": "用户与智能体在回路中交互。每轮：智能体输出当前规则 → 用户提修改意见 → 智能体调整 → 确认后进入下一阶段"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| container_type | string | 容器类型（迭代/循环/对话） |
| loop_desc | string | 内部逻辑描述 |