# 输出格式

## JSON 结构规范

```json
{
  "version": "2.0",
  "flow": "custom",
  "meta": {
    "title": "流程名称",
    "description": "流程描述（可选）"
  },
  "nodes": [...],
  "connections": [...]
}
```

---

## nodes 数组

### 节点基本结构
```json
{
  "id": "n1",
  "type": "start",
  "label": "显示名称",
  "x": 80,
  "y": 240,
  "config": {}
}
```

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| id | string | ✓ | 唯一标识，格式 n1, n2, n3... |
| type | string | ✓ | 节点类型（见 NODE-GUIDE.md） |
| label | string | ✓ | 画布上显示的名称 |
| x | number | ✓ | 左上角 x 坐标 |
| y | number | ✓ | 左上角 y 坐标 |
| config | object | ✓ | 节点配置（见 CONFIG-REFERENCE.md） |

---

## connections 数组

### 连接基本结构
```json
{
  "sourceId": "n1",
  "targetId": "n2"
}
```

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| sourceId | string | ✓ | 源节点 id |
| targetId | string | ✓ | 目标节点 id |

---

## 完整示例

```json
{
  "version": "2.0",
  "flow": "custom",
  "meta": {
    "title": "客服工单路由",
    "description": "根据用户意图自动分流处理"
  },
  "nodes": [
    {
      "id": "n1",
      "type": "start",
      "label": "开始",
      "x": 80,
      "y": 240,
      "config": {}
    },
    {
      "id": "n2",
      "type": "classifier",
      "label": "意图分类",
      "x": 320,
      "y": 240,
      "config": {
        "input_source": "{{sys.query}}",
        "categories": "退货 → 用户要求退货退款\n换货 → 用户要求更换商品\n咨询 → 用户询问商品或服务信息\n投诉 → 用户表达不满或投诉",
        "model": "gpt-4o-mini"
      }
    },
    {
      "id": "n3",
      "type": "llm",
      "label": "退货处理",
      "x": 560,
      "y": 160,
      "config": {
        "title": "退货处理智能体",
        "model": "gpt-4",
        "system_prompt": "你是电商退货处理专家。负责处理用户退货请求。\n\n需要输出：\n1. approval: 是否批准（true/false）\n2. reason: 审批理由\n3. next_steps: 下一步操作指引\n\n规则：\n1. 超过30天原则上不批准\n2. 涉及VIP用户可特批\n3. 只输出JSON",
        "user_prompt": "用户退货请求：{{sys.query}}\n订单信息：{{n2.output}}",
        "max_iterations": "2"
      }
    },
    {
      "id": "n4",
      "type": "reply",
      "label": "输出结果",
      "x": 800,
      "y": 240,
      "config": {
        "reply_type": "直接回复",
        "content": "✅ 您的请求已处理\n\n📋 处理结果：{{n3.output.approval ? '已批准退货' : '需进一步审核'}}\n📝 详情：{{n3.output.reason}}"
      }
    },
    {
      "id": "n5",
      "type": "end",
      "label": "结束",
      "x": 1040,
      "y": 240,
      "config": {}
    }
  ],
  "connections": [
    {"sourceId": "n1", "targetId": "n2"},
    {"sourceId": "n2", "targetId": "n3"},
    {"sourceId": "n2", "targetId": "n4"},
    {"sourceId": "n3", "targetId": "n4"},
    {"sourceId": "n4", "targetId": "n5"}
  ]
}
```

---

## 硬性约束

| 约束 | 说明 |
|------|------|
| version | 必须是 "2.0" |
| flow | 必须是 "custom" |
| 所有 config 值 | 必须是字符串，数字写成 "5" |
| tools 引号 | 内部引号转义 `\"` |
| 连接格式 | 只用 sourceId/targetId，不加 uuid |
| 坐标范围 | y ≥ 80，x ≥ 80 |
| 节点 id | 唯一性，格式 n + 数字 |
| JSON 合法性 | 必须是无语法错误的有效 JSON |

---

## 导出与导入

### 导出
生成的 JSON 可直接保存为 `.json` 文件，导入画布模拟器使用。

### 导入验证
导入前检查：
1. JSON 语法是否合法
2. nodes 数组是否非空
3. connections 中的 id 是否都在 nodes 中存在
4. 是否有 start 和 end 节点