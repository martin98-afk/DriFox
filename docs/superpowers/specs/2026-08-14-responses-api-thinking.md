# GPT-5.x 系列思考渲染：Responses API 支持 设计文档

> 版本: v1
> 日期: 2026-08-14
> 状态: 已实现

## 1. 概述

### 1.1 问题

`gpt-5.6-luna` 等 GPT-5.x 系列模型**没有思考过程渲染**。实测（OpenCode Go 网关 `opencode.ai/zen/go/v1`）：

| 通道 | 流式 delta 字段 |
|---|---|
| `chat/completions`（DriFox 原路径） | 只有 `content`，**无任何 reasoning 字段** |
| `/v1/responses` | ✅ `response.reasoning_summary_text.delta` 返回思考摘要 |

对照同网关其他模型：`deepseek-v4-flash` / `kimi-k2.7-code` 的 `chat/completions` 流式 delta 均返回 `reasoning_content`，DriFox 原有解析路径正常渲染。

**根因**：GPT-5.x 的思考内容只在 Responses API（`/v1/responses`）的 `reasoning_summary_text` 事件中返回；OpenCode Go 网关的 `chat/completions` 端点剥离了 reasoning。DriFox 只走 `chat/completions` → 上游无思考数据 → 渲染端空白。这不是 DriFox 解析 bug，models.dev 的 `supports_thinking=true` 与网关实际行为不一致。

### 1.2 目标

- GPT-5.x 系列模型（模型名以 `gpt-5` 开头）自动切换 Responses API
- 解析 `reasoning_summary_text` 事件 → 复用既有 `reasoning_content_received` 信号 → 思考块正常渲染
- 工具调用、多轮对话与 chat/completions 行为一致

### 1.3 非目标

- 其他模型系列切换 Responses API（deepseek/kimi 等 chat/completions 已正常）
- Responses API 的完整思考原文（网关只暴露 summary 摘要）
- `include` 参数（`reasoning.encrypted_content` 等）透传

---

## 2. 架构

### 2.1 触发条件（chat_worker.py `_use_responses_api`）

```
llm_config["使用ResponsesAPI"] 显式 True/False → 强制开关（可覆盖）
否则模型名小写以 "gpt-5" 开头 → True
```

### 2.2 请求构造（`_build_responses_kwargs`）

内部消息 → Responses API 请求参数（`message_content.py::messages_to_responses_input`）：

| 内部消息 | Responses input item |
|---|---|
| `role=system` | → `instructions` 顶层参数（input 中不允许 system role） |
| `role=user` | `{"type":"message","role":"user","content":[input_text/input_image]}` |
| `role=assistant`（纯文本） | `{"type":"message","role":"assistant","content":[output_text]}` |
| `role=assistant`（带 tool_calls） | `{"type":"function_call","call_id","name","arguments"}` |
| `role=tool` | `{"type":"function_call_output","call_id","output"}` |
| 图片块 | `input_image`（`image_url` 为**字符串**，chat/completions 是对象） |

其他映射：
- `思考模式` → `reasoning: {"effort": low/medium/high}`；关闭 → `{"effort": "none"}`
- tools → 扁平格式（`{"type":"function","name","description","parameters"}`）
- `最大Token` → `max_output_tokens`（复用 `_cap_max_output_tokens` 上限保护）
- 流式强制 `stream=True`（解析器仅支持事件流）

### 2.3 流式事件解析（`_process_responses_stream`）

| Responses 事件 | DriFox 动作 |
|---|---|
| `response.reasoning_summary_text.delta` | `thinking_started`（首次）+ 累积 → `reasoning_content_received` |
| `response.output_text.delta` | 累积 → `content_received` |
| `response.output_item.added` (function_call) | 建 `_tool_calls_buffer`（item_id → name） |
| `response.function_call_arguments.delta` | 参数累积 |
| `response.output_item.done` (function_call) | 补全 `call_id` → `_current_tool_calls` → `tool_call_started` + `tool_args_updated` |
| `response.failed` / `error` | 抛 `StreamInterruptedError` |
| `response.completed` | 正常结束 |

**关键坑**：SDK 事件的 `item` 是 pydantic 对象（`ResponseFunctionToolCall`）而非 dict，访问字段用 `_responses_item_get`（兼容两者）。

### 2.4 多轮工具调用

第一轮返回 `function_call` → DriFox 执行工具 → 第二轮消息经 `messages_to_responses_input` 转成 `function_call` + `function_call_output` items。实测 OpenCode Go 网关**不要求**回传 reasoning item，简化回传可行。

---

## 3. 验证结果

| 验证项 | 结果 |
|---|---|
| `pytest tests/test_responses_api_support.py` | 8 passed（转换 3 + 流解析 5） |
| 既有 chat_worker 测试回归 | 43 passed |
| 真实 API 思考渲染（gpt-5.6-luna） | ✅ reasoning 384 字符经信号输出 |
| 真实 API 工具调用两轮 | ✅ call_id/arguments 解析 + tool result 回传 + 最终答案 |
| deepseek/kimi 回归 | ✅ `_use_responses_api` 返回 False，走原路径 |
| ruff check / format | ✅ |

---

## 4. 变更文件

| 文件 | 变更 |
|---|---|
| `app/core/message_content.py` | 新增 `messages_to_responses_input` + 辅助函数 |
| `app/core/workers/chat_worker.py` | `_use_responses_api` / `_build_responses_kwargs` / `_process_responses_stream` / `_make_api_call` 分支 |
| `tests/test_responses_api_support.py` | 新增：消息转换 + 流式事件解析单测 |
