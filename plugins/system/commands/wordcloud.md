---
description: 将当前会话内容快速总结为 ECharts 词云，提取关键术语并可视化展示
type: prompt
argument-hint:
  "[<focus>]": "可选，指定词云提取焦点（如「技术栈」、「需求」、「问题点」），留空则自动提取全量关键术语"
---

## ⚙️ 行为规范（LLM 提示词正文）

### 1. 参数解析

`$ARGUMENTS` 是用户输入的完整字符串（不含 `/wordcloud` 前缀）。

- **`<focus>`** 是可选参数，指定词云提取的焦点方向
- 未提供 focus → 自动分析会话整体内容，提取全量关键术语

### 2. 执行流程

```
1. 分析会话历史：回顾整个对话的上下文、关键概念、高频术语
2. 提取关键词：
   - 提取 15-30 个关键术语/短语
   - 每个词附带权重（value），反映其在对话中的重要程度和出现频次
   - 如果用户指定了 <focus>，优先提取与该焦点相关的术语
3. 生成 ECharts 词云配置（遵循 echarts-wordcloud 扩展格式）
4. 通过 ```echarts 代码块展示
```

### 3. ECharts 词云配置模板

使用以下结构生成词云，字段含义见注释：

```javascript
{
  "tooltip": {
    "show": true,
    "backgroundColor": "rgba(26,31,46,0.9)",
    "borderColor": "#3a3f50",
    "textStyle": { "color": "#e5e5e5", "fontSize": 13 }
  },
  "series": [{
    "type": "wordCloud",
    "shape": "circle",
    "left": "center",
    "top": "center",
    "width": "90%",
    "height": "90%",
    "sizeRange": [14, 60],          // [最小字号, 最大字号]
    "rotationRange": [-90, 90],     // 旋转角度范围
    "rotationStep": 45,             // 旋转步长
    "gridSize": 8,                  // 网格密度（越小越紧凑）
    "drawOutOfBound": false,
    "layoutAnimation": true,
    "textStyle": {
      "fontFamily": "sans-serif",
      "fontWeight": "bold"
      // ⚠️ 不要在此设 color 函数！``echarts 走 JSON.parse，JS 函数会抛出异常
    },
    "data": [
      // 每个词一条记录，value 越大字号越大，颜色通过每项 textStyle.color 静态指定
      {"name": "关键词", "value": 100, "textStyle": {"color": "#6366f1"}},
      {"name": "关键词", "value": 80,  "textStyle": {"color": "#8b5cf6"}},
      ...
    ]
  }]
}
```

### 4. ⚠️ 关键约束

**``echarts 代码块通过 `JSON.parse()` 解析，配置中不能出现任何 JavaScript 函数表达式。**

- ❌ 禁止：`"color": function () { return ... }`、`"formatter": function(params) { ... }`
- ✅ 正确：所有颜色通过每项 `data[i].textStyle.color` 静态指定（见下方配色方案）

### 5. 配色方案

使用 DriFox 品牌色系，根据权重分配颜色：

| 权重区间 | 颜色 |
|----------|------|
| 最高 (top 20%) | `#6366f1` 靛蓝 |
| 高 (20-40%)    | `#8b5cf6` 紫色 |
| 中 (40-60%)    | `#ec4899` 粉色 |
| 低 (60-80%)    | `#f59e0b` 琥珀 |
| 最低 (80-100%) | `#10b981` 翠绿 |

也可以根据内容主题动态选择色系，保持色调统一。

### 6. 输出格式

直接通过 ````echarts` 代码块输出词云配置，代码块前后可附带简要说明文字（2-3 句），解释词云展示的核心主题和洞察。

### 7. 特殊情况处理

| 场景 | 行为 |
|------|------|
| 会话内容过短（< 5 轮对话） | 提示"会话内容较少，词云可能不够丰富"，仍需尽力从已有内容中提取关键词 |
| 无法提取有意义的术语 | 提示并从用户最后一轮输入中提取关键词 |
| 用户指定 focus 但相关术语过少 | 以 focus 相关词为主，适当补充会话中的通用关键词 |
