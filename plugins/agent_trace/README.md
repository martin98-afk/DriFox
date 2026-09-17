# agent_trace — 智能体轨迹查看器（v2）

仿照 DeepSeek Harness 的「轨迹」面板。常驻标题栏 `轨迹` tab，点击进入 full 覆盖层。

## 功能（v2 重写）

- **三泳道甘特图**：`Input` / `Model` / `Tools` 三条泳道，hover 显示 tooltip、
  点击选中记录。顶栏两个互斥开关切换宽度语义：
  - `Duration`：条带宽度 ∝ 真实耗时；
  - `Token`：条带宽度 ∝ token 占比（顺序仍是时间序），顶部刻度换画**累积 token**。
  
  两个都不开 = 等宽铺满（默认）。三者共用同一条时间视口：滚轮以鼠标所在
  时刻为锚点缩放，等宽 / Token 下只铺视口内记录并在窗内重新归一化。
  Token 模式带像素级下限 + water-filling 分配，小条目（30 tok 对 8k tok）
  也保证可见可点。
- **类型过滤 + 搜索**：列表顶部 chips（全部/系统/用户/钩子/助手/工具）一键
  隐藏 hook 刷屏；有失败时额外出现「失败 N」chip，点切只看失败。
  顶栏搜索框全文匹配（label/preview/raw），命中片段在行内高亮，回车跳到首条。
- **turn 分组**：真实 USER 消息行标注 `T{n}` 可点徽章；点它 = 只看该轮，
  同时泳道图自动放大到该轮的实际时间跨度（点同一徽章取消）。
  ⚠️ 轮次归属对齐主程序 `get_user_round_ranges`：user 之前的 hook
  （PreUserMessage 等）归入**紧随其后**那一轮，`SessionStart` 属会话级不归任何轮。
- **完整内容**：右侧详情按条目类型给 tab（System Prompt / Tools Schema /
  Request / Response / Thinking / Preview / Raw / Info / 统计），自动换行、可复制。
- **右键菜单**：列表右键可复制名称 / 摘要 / 入参 / 结果 / 完整内容，
  **从这里分支**（以**选中的那一条消息**为界复制成新会话）、跳该轮、只看失败。
  分支会自动修补工具调用配对（丢弃孤立 tool 结果、剥掉结果落在截断点之后的
  `tool_calls`），不会产生 API 拒绝的悬空调用。SYSTEM 是合成行，无此入口。
- **键盘**：`Ctrl+F` 聚焦搜索（卡片内任意位置）、`Esc` 清筛选（无筛选时收起详情）。
- **底部统计栏**：`N 条 · M 轮 | LLM 总时长 · 工具总时长 | 上下文 tok · 占比`，
  有失败时右侧额外显示可点的「N 失败」。

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
| 字体 | UI 文字走系统字体（`ctx.font_family`）；代码/JSON/数字列用 `Cascadia Mono, Consolas, Menlo, monospace`（DevTools 观感） |
| 配色 | 全部经 `ctx["colors"]` 注入；透明度一律 `with_alpha(QColor(hex), a)` 派生。**禁止** `QColor("rgba(...)")` 字符串 —— Qt 解析失败静默返回黑色（v1 黑块根因） |
| 工具行摘要 | `tool_arg_summary` 从入参 JSON 里挑可读主参数（`bash cd /d ...`、`read x.py:420-470`、`grep _eq_order in plugins/`），不再直接甩原始 JSON |
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
