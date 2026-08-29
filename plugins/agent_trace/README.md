# agent_trace — 智能体轨迹查看器

仿照 DeepSeek Harness 的「轨迹」面板。常驻标题栏 `轨迹` tab，点击进入 full 覆盖层。

## 功能

- **实时采集**：订阅 `ChatBackend` 的 `tool_call_started` / `tool_result_received` /
  `hook_status_changed` / `stream_finished` / `messages_updated` 信号，按时间顺序
  累积成「轨迹条目」(TraceEntry)。
- **完整显隐**：列出当前会话全部 history message 中能看到的角色——
  System / User / Context (hook 注入) / Assistant / Tool，
  含首行内容预览、单步耗时与绝对时间戳。
- **DeepSeek-style Timeline**：横向条状时间线（`Duration / Turns / Calls` 摘要），
  一眼对比每轮 turn 总耗时与工具调用次数。
- **右侧详情**：Summary / Preview / Raw / Source 四 Tab 内容展示，
  Preview 渲染 markdown 预览、Raw 给出原始 JSON / 文本、Source 标出注入来源
  （例如 SessionStart hook、PostToolUse hook、模型为 `xxx`）。

## 数据范围

仅当前会话；切换/新建会话自动清空（不持跨会话历史，零磁盘开销）。

## 依赖

- PyQt5
- qfluentwidgets
- loguru
- 主程序 `app.core.backend.ChatBackend`
- 主程序 `app.plugins.registries.ui_plugin_registry`
