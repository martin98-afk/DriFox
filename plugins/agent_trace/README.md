# agent_trace — 智能体轨迹查看器（v2）

仿照 DeepSeek Harness 的「轨迹」面板。常驻标题栏 `轨迹` tab，点击进入 full 覆盖层。

## 功能（v2 重写）

- **三泳道甘特图**：`Input` / `Model` / `Tools` 三条泳道，条带按时间比例排布，
  hover 显示 tooltip、点击选中记录。顶栏 `Duration` / `Turns` / `Calls` 切换：
  - Duration：真实时间轴
  - Turns：每个 turn 等宽分段（段内仍按真实时间）
  - Calls：只看工具调用
- **类型过滤 + 搜索**：列表顶部 chips（全部/系统/用户/上下文/助手/工具）一键
  隐藏 hook 刷屏；顶栏搜索框全文匹配（label/preview/raw）。
- **turn 分组**：真实 USER 消息行标注 `Turn N` 分隔线；详情标题显示所属轮次。
- **完整内容**：右侧详情 `Summary` / `Preview` / `Raw` / `Source` 四 tab，
  Preview/Raw 自动换行、可复制；多模态 content 提取 text 段。
- **底部统计栏**：`N 轮 · M 步 | LLM 总时长 · 工具总时长 | 上下文 tok`。

## 架构（数据正确性三原则）

| 原则 | 实现 |
|---|---|
| messages 唯一事实源 | `TraceCollector` 把 `session.messages` 1:1 投影成 records，实时信号**不再**另建记录 → 无重复、无错位 |
| 实时信号只写 timing 表 | `tool_call_started/result` → `_timing[tool_call_id]`；`stream_started/finished` → `_streams[k]`；投影时按 id/序号回填精确起止 |
| 增量 diff 信号 | 全量重建后与旧列表比对，只发 `recordsReset` / `recordsAppended(start,count)` / `recordsUpdated(start,count)`；UI 不再无脑全量刷新 → 滚动/选中稳定 |

时长语义（修 v1「持续时长一直增长」）：

- 消息类条目 duration = 下一条消息 timestamp − 本条 timestamp（存续间隔）；
- TOOL / ASSISTANT 优先用实时信号精确起止；
- 已完成记录的 `duration_ms` 是**固定值**（不再回退 `time.time()`）；
- in-flight（流式生成中 / 工具执行中）在列表尾部单独展示，落盘后自动被正式记录取代。

## 设计要点

| 项 | 说明 |
|---|---|
| 字体 | `Cascadia Mono, Consolas, Menlo, monospace`（DevTools 观感） |
| 配色 | 全部经 `ctx["colors"]` 注入；透明度一律 `with_alpha(QColor(hex), a)` 派生。**禁止** `QColor("rgba(...)")` 字符串 —— Qt 解析失败静默返回黑色（v1 黑块根因） |
| 列表实现 | `QListWidget` + `QStyledItemDelegate` 自绘；过滤/搜索走「可见索引映射」（`Qt.UserRole+1` 存 record 索引） |
| 选中态 | record 索引制：过滤、追加、回填都不丢选中 |
| 心跳 | 1s QTimer，仅存在 in-flight 记录时重绘（时长走动） |
| 侧边栏 | `metadata.hide_sidebar=True`，入口只有标题栏常驻 tab |

## 数据范围

仅当前会话；切换/新建会话自动清空（不持跨会话历史，零磁盘开销）。

## 已知约束

- 显示卡片必须用 `UIPluginRegistry.toggle_floating_card(card_id)`，
  **不能**用 `card_manager.show_card` —— 后者不创建实例，首次点击会静默失败。
- `TraceCardWidget.closed` 必须是 `pyqtSignal`（registry 会对它 `connect`）；
  v1 写成 `None` 导致首次创建中断、点击 tab 无反应。
- `register_titlebar_tab` 的 `icon_path` 无主题感知，故 tab 只显示纯文字。

## 依赖

- PyQt5、qfluentwidgets、loguru
- 主程序 `app.core.backend.ChatBackend`、`app.plugins.registries.ui_plugin_registry`

## 手工验证

```bash
uv run python -X utf8 tests/_smoke_agent_trace.py        # 结构/注册/实例化/联动
```
