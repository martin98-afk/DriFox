# UI 插件可复用控件库 — 索引

> 来自 `context-usage-stats` 等实际项目中提炼的可复用 PyQt5 自绘控件、工具函数与最佳实践。
> 所有控件都遵循**插件闭包原则**（不导入 `app.core` / `app.widgets`），可独立放到任何 UI 插件的 `ui/` 目录下。

---

## 0. 何时查这个目录

| 你要做的事 | 查哪个文件 |
|------------|-----------|
| 显示一个数字指标（图标+标题+大字+副标题） | `widgets-statcard.md` |
| 画每日会话量、消息量等离散数据柱状图 | `widgets-charts.md §一` |
| 画 token 用量趋势、消息量变化等连续折线 | `widgets-charts.md §二` |
| 画项目分布、文件类型排行等分类数据 | `widgets-charts.md §三` |
| 大数字格式化成 `1.2k` / `1.2M` / 百分比 | `widgets-utils.md §一` |
| 估算一段文本的 token 数（无 tiktoken 依赖） | `widgets-utils.md §二` |
| 把 `"01-15"` 转成 `"01-15\n周一"` 双行格式 | `widgets-utils.md §三` |
| 读取 SQLite 数据库（dev/用户目录兜底） | `widgets-sqlite.md §一` |
| 查询最近 N 天按日分组的数据 | `widgets-sqlite.md §二` |
| 新字段缺失时回退到旧字段估算 | `widgets-sqlite.md §三` |
| 把 ctx["colors"] 转成图表用的 QColor 字典 | `widgets-theme.md §二` |
| 浮动卡片主题色适配（白字固定） | `widgets-theme.md §一/§三` |

---

## 1. 设计原则（所有控件都遵循）

1. **插件闭包**：不导入 `app.core` / `app.widgets`，可独立搬运
2. **依赖最小**：仅 `PyQt5` / `qfluentwidgets` / `loguru` / stdlib
3. **主题色拉模型**：实现 `set_colors(colors: dict)` 方法，由父卡片统一注入（详见 `widgets-theme.md`）
4. **自适应宽度**：所有 `paintEvent` 用 `if w >= 420` 等条件分支调整边距和字号
5. **完整的 paintEvent**：圆角、网格、标签防裁剪等都封装好，调用方只需 `set_data(...)`

---

## 2. 推荐文件组织

### 2.1 整体搬迁（推荐）

直接复制 `context-usage-stats/ui/cards.py` 中的相关类和函数到你插件的 `ui/widgets.py` 中，按需改名/裁切：

```
plugins/<your-plugin>/
├── .drifox-plugin/
│   └── plugin.json
└── ui/
    ├── __init__.py       # register_ui 入口
    ├── cards.py          # 主卡片 widget（含 _render_content 用 widgets）
    ├── widgets.py        # 复用 widgets（直接复制本目录文件中的代码）
    └── async_worker.py   # _DataWorker / _async_load_data 模板
```

或更轻量（无外部依赖时）：

```
plugins/<your-plugin>/
└── ui/
    ├── __init__.py       # register_ui 入口
    └── cards.py          # 主卡片 widget（widgets 直接放这里）
```

### 2.2 按需引入

只复制你需要的部分到现有 `cards.py`，不需要建独立文件。

---

## 3. 最小整合示例

> 想看完整整合示例 → 见 `templates.md §六`。
> 下面只展示"如何在一个新卡片里快速用上这些 widgets"。

```python
# plugins/<your-plugin>/ui/cards.py
from .widgets import (
    # widgets-statcard.md
    _StatCard,
    # widgets-charts.md
    _BarChartWidget, _LineChartWidget, _ProjectBarWidget,
    # widgets-utils.md
    _format_number, _fast_estimate_tokens,
    # widgets-sqlite.md
    _get_db_connection,
    # widgets-theme.md
    _make_chart_colors_from_context,
)


class MyCard(QWidget):
    def _render_content(self, data: dict):
        # 清空旧内容
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 1. 第一行：3 个 _StatCard（关键指标横排）
        stat_row = QHBoxLayout()
        stat_row.setSpacing(8)
        for ic, title, val, sub in [
            (FluentIcon.FONT, "总 token 数",
             _format_number(data["total_tokens"]), "累计消耗"),
            (FluentIcon.CHAT, "总会话数",
             str(data["total_sessions"]), "近 14 天"),
            (FluentIcon.MESSAGE, "总消息数",
             _format_number(data["total_messages"]), "累计"),
        ]:
            card = _StatCard(ic, title, val, sub)
            if self._chart_style:
                card.set_colors(self._chart_style)
            stat_row.addWidget(card)
        stat_widget = QWidget()
        stat_widget.setLayout(stat_row)
        self._content_layout.addWidget(stat_widget)

        # 2. 折线图
        if data["daily_tokens"]:
            chart = _LineChartWidget("🔤 每日 Token", data["daily_tokens"])
            if self._chart_style:
                chart.set_colors(self._chart_style)
            self._content_layout.addWidget(chart)

        # 3. 柱状图
        if data["daily_sessions"]:
            bar = _BarChartWidget("📊 每日会话", data["daily_sessions"])
            if self._chart_style:
                bar.set_colors(self._chart_style)
            self._content_layout.addWidget(bar)
```

`set_context_provider` / `show_card` / `_apply_latest_theme` / `_async_load_data` 等浮动卡片骨架见 `templates.md §一`。

---

## 4. 文件结构

```
references/
├─ widgets.md               ← 本文件（索引 + 原则 + 整合示例）
├─ widgets-statcard.md      ← _StatCard（多层级统计卡片）
├─ widgets-charts.md        ← _BarChartWidget / _LineChartWidget / _ProjectBarWidget
├─ widgets-utils.md         ← 工具函数（数字格式化 / token 估算 / 日期带星期）
├─ widgets-sqlite.md        ← SQLite 读取模式（路径兜底 / N 天窗口 / fallback）
├─ widgets-theme.md         ← 主题色映射（ctx → QColor 字典）
└─ templates.md §六         ← 把 widgets 集成到浮动卡片骨架
```

---

## 5. 与其他章节的衔接

- **templates.md §一**：浮动卡片骨架（头部 / 比例高度 / 异步 worker）
- **SKILL.md §4.1**：上下文注入（拉模型）
- **SKILL.md §4.2**：比例高度
- **SKILL.md §4.6**：卡片关闭信号
- **本目录**：widgets + 工具函数 + SQLite 模式 + 主题色映射

新建一个 UI 插件的推荐流程：

1. 复制 `templates.md §一` 浮动卡片骨架到 `ui/cards.py`
2. 复制 `widgets-statcard.md` / `widgets-charts.md` / `widgets-utils.md` 等需要的部分到 `ui/widgets.py`
3. 复制 `templates.md §四` register_ui 入口到 `ui/__init__.py`
4. 写 `_fetch_data()` 用 `widgets-sqlite.md` 的查询模式
5. 写 `_render_content()` 用 `widgets-statcard.md` / `widgets-charts.md` 的控件
6. `ruff check` + 触发热重载验证

---

## 6. 注意事项与陷阱（速查表）

> 完整陷阱清单见各子文件末尾"常见陷阱"章节。

| 陷阱 | 修复 | 见哪一节 |
|------|------|---------|
| `QPainter` 不结束会卡 UI | 每个提前 `return` 前 `painter.end()` | `widgets-charts.md §七` |
| 浮动卡片背景暗导致黑色字 | `text` / `text_secondary` 固定白色 | `widgets-theme.md §二.2` |
| 标签被顶部裁剪 | 折线图 `top_margin = max_val * 0.3` | `widgets-charts.md §二.4` |
| 旧数据无新字段 | 精确值 + 估算值 分开查再相加 | `widgets-sqlite.md §三` |
| SQLite 连接阻塞 UI | 用 `_DataWorker` 后台线程跑 | `templates.md §一` |
| 主题色不生效 | 确认 `_apply_latest_theme` 在 `show_card` 中调用 | `widgets-theme.md §四` |
| 图表刷新不及时 | `set_data(...)` 后 `self.update()` | `widgets-charts.md §五` |
| 数据库被锁 | `timeout=3` + 短事务 + 必要时重试 | `widgets-sqlite.md §四.4.2` |
| 路径层级数错（找不到 DB） | 打印 `_PROJECT_ROOT` 验证 | `widgets-sqlite.md §一.3` |
| `messages` JSON 太大 OOM | `str(msg_data)[:100000]` 截断 | `widgets-sqlite.md §三.3` |
| `_BarChartWidget` 柱顶标签溢出 | `if label_y < margin_top: label_y = y + 4` | `widgets-charts.md §一.5` |
| 上下文颜色字符串无效 | try/except + fallback | `widgets-theme.md §六.1` |