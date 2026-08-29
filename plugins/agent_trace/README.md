# agent_trace — 智能体轨迹查看器

仿照 DeepSeek Harness 的「轨迹」面板。常驻标题栏 `轨迹` tab，点击进入 full 覆盖层。

## 功能

- **实时采集**：订阅 `ChatBackend` 的 `tool_call_started` / `tool_result_received` /
  `hook_status_changed` / `stream_finished` / `_hook_messages_updated` 信号，按时间顺序
  累积成「轨迹条目」(TraceRecord)。
- **完整显隐**：列出当前会话全部 history message 中能看到的角色——
  `SYSTEM` / `USER` / `CONTEXT`(hook 注入) / `ASSISTANT` / `TOOL`，
  含内容预览、单步耗时与绝对时间戳。
- **DevTools 风格三栏**：
  - 上：Timeline 瀑布条（Duration / Turns / Calls 摘要 + 时间刻度网格）
  - 左：条目列表（等宽字体、紧凑 24px 行高、彩色类型圆点）
  - 右：详情面板（Summary 键值表 / Preview / Raw / Source 四 tab）

## 设计要点

| 项 | 说明 |
|---|---|
| 字体 | `Cascadia Mono, Consolas, Menlo, monospace`（DevTools 观感） |
| 配色 | 全部经 `ctx["colors"]` 注入，**不硬编码** `rgba(255,255,255,x)`（浅色主题会不可见） |
| 列表实现 | `QListWidget` + `QStyledItemDelegate` 自绘（避免动态 QWidget 行的 show 时序问题） |
| 选中态 | 由 `QListWidget.setCurrentRow` 维护，实时追加数据不丢失 |
| 侧边栏 | `metadata.hide_sidebar=True`，入口只有标题栏常驻 tab |

## 数据范围

仅当前会话；切换/新建会话自动清空（不持跨会话历史，零磁盘开销）。

## 已知约束

- 显示卡片必须用 `UIPluginRegistry.toggle_floating_card(card_id)`，
  **不能**用 `card_manager.show_card` —— 后者不创建实例，首次点击会静默失败。
- `register_titlebar_tab` 的 `icon_path` 无主题感知（`CustomTabButton` 用单一
  `QIcon(path).pixmap`），故本插件 tab 不传图标，只显示纯文字。

## 依赖

- PyQt5、qfluentwidgets、loguru
- 主程序 `app.core.backend.ChatBackend`、`app.plugins.registries.ui_plugin_registry`

## 手工验证

```bash
uv run python -X utf8 tests/_smoke_agent_trace.py        # 结构/注册/实例化
uv run python -X utf8 tests/_diag_agent_trace_click.py   # 点击回调走对 API、无 emoji
uv run python -X utf8 tests/_diag_detail_panel.py        # 详情面板内容填充
uv run python -X utf8 tests/_diag_trace_card_click.py    # 三栏联动集成
```
