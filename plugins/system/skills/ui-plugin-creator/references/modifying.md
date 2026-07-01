# 修改现有插件

> 本文是 `SKILL.md §6` 的完整展开。
> 新建插件见 `references/workflow.md`，常见模式见 `references/patterns.md`。

---

## 1. 修改前必读

### 1.1 找到目标插件

```bash
# 系统插件
ls plugins/<plugin-name>/

# 用户插件（运行时下载的）
ls ~/.drifox/plugins/<plugin-name>/
```

### 1.2 阅读入口

先读 `ui/__init__.py` 了解已注册的组件：

```python
# ui/__init__.py
def register_ui(registry):
    from .cards import MyCardWidget
    registry.register_floating_card(
        plugin_name="<plugin-name>",
        card_id="my-card",
        widget_class=MyCardWidget,
        container="bottom",
        title="我的卡片",
    )
```

### 1.3 找到对应代码

| 文件 | 内容 |
|------|------|
| `ui/cards.py` | 主卡片 widget 类 |
| `ui/renderers.py` | 内容块渲染器函数 |
| `ui/factories.py` | 消息工厂 |
| `ui/__init__.py` | register_ui 入口 |
| `.drifox-plugin/plugin.json` | 插件清单 |

---

## 2. 修改步骤

### 2.1 加按钮

**场景**：想在卡片头部加一个"导出"按钮。

```python
# ui/cards.py
# 1. 在 _setup_ui 的头部布局加按钮
self._export_btn = ToolButton(FluentIcon.SHARE, header)
self._export_btn.setToolTip("导出数据")
self._export_btn.clicked.connect(self._on_export)
hly.addWidget(self._export_btn)  # 在 close_btn 之前

# 2. 实现 _on_export
def _on_export(self):
    data = self._current_data  # 用已加载的数据
    # ... 导出逻辑 ...

# 3. 重新触发热重载验证
```

### 2.2 调样式

**场景**：改统计卡片的字号或颜色。

读 `widgets-theme.md §四` 和 `widgets-statcard.md §一.5`，按 objectName 修改 setStyleSheet。

```python
# 找到对应 widget（_StatCard._apply_card_style）
# 修改 val_size 计算公式
val_size = max(round(base_font_size * 24 / 14), 18)  # 原 22/14 → 改 24/14
```

### 2.3 加图表

**场景**：现有卡片只有统计卡片，想加一个柱状图。

读 `widgets-charts.md §一`，复制 `_BarChartWidget` 类到 `ui/cards.py`（或独立 `widgets.py`）。

```python
# ui/cards.py 顶部
from PyQt5.QtWidgets import ...  # 确保 QPainter 等导入

# 加 _BarChartWidget 类（或 import）

# 在 _render_content 中：
bar = _BarChartWidget("📊 每日统计", data["daily"])
if self._chart_style:
    bar.set_colors(self._chart_style)
self._content_layout.addWidget(bar)
```

### 2.4 改数据源

**场景**：原来读 SQLite，现在想接 HTTP API。

```python
# 1. 改 _fetch_data 签名（如果是 _Worker(_fetch_data) 直接传）
# 2. 新 _fetch_data 返回同样格式的 dict
def _fetch_data(self) -> dict:
    import requests
    resp = requests.get("https://api.example.com/stats", timeout=5)
    resp.raise_for_status()
    data = resp.json()
    return {
        "total_sessions": data.get("total", 0),
        "daily_sessions": [(d["date"], d["count"]) for d in data.get("daily", [])],
        "error": None,
    }

# 3. 如果插件不在 _vendor/，需要把 requests 加进去（见 templates.md §五）
```

### 2.5 改容器（bottom → top）

```python
# ui/__init__.py
registry.register_floating_card(
    plugin_name="<plugin-name>",
    card_id="my-card",
    widget_class=MyCardWidget,
    container="top",  # ← 改这里
    title="我的卡片",
)
```

---

## 3. 验证清单

### 3.1 改完后必做

```bash
# 1. ruff 检查
ruff check plugins/<plugin-name>/ui/

# 2. 触发热重载
# 主程序会自动检测变更，重新执行 register_ui
# 观察日志确认无 ImportError

# 3. 实际测试卡片功能
# - 显示卡片：/my-card 命令或菜单
# - 检查主题色是否正确
# - 检查数据是否加载
# - 检查关闭按钮是否工作
```

### 3.2 常见错误

| 错误 | 原因 | 修复 |
|------|------|------|
| `ImportError: cannot import name 'X'` | 加了新 widget 但忘记在 `cards.py` 顶部 import | 检查 imports |
| `NameError: name 'X' is not defined` | 函数/类拼写错误 | 检查大小写 |
| 修改没生效 | Python sys.modules 缓存 | 检查 `ui/__init__.py` 是否清理了缓存 |
| 主题色不对 | `_apply_latest_theme` 没在 `show_card` 调用 | 见 `patterns.md §1` |
| Worker 内存泄漏 | `deleteLater` 没连接 | 见 `patterns.md §3` |
| 卡片不显示 | `container` 错或 `default_visible` 错 | 检查 `__init__.py` |
| `_vendor/` 加载失败 | sys.path 顺序问题 + sys.modules 缓存 | 见 `templates.md §五.5.1` |

完整验证清单见 `checklist.md`。

---

## 4. 调试技巧

### 4.1 加诊断日志

```python
def register_ui(registry):
    logger.info(f"[<plugin>] register_ui called")
    # ... 原有逻辑 ...
    logger.info(f"[<plugin>] UI registered: {card_ids}")

def show_card(self):
    logger.debug(f"[<plugin>] show_card, ctx={self._context_provider}")
    self._apply_latest_theme()
    self._async_load_data()
```

### 4.2 在主程序中观察日志

```python
# 主程序通常有日志面板；或在终端运行
python main.py
# 观察输出中的 [<plugin>] 前缀日志
```

### 4.3 检查 sys.modules 缓存

```python
import sys
# 找出 plugin 的模块
plugin_modules = [k for k in sys.modules if '<plugin-name>' in k]
print(f"模块缓存: {plugin_modules}")
# 热重载前应该有残留，热重载后应该被清空
```

### 4.4 单独测试 fetch_data

```python
# 在 Python REPL 中模拟 worker 调用
def _fetch_data():
    conn = _get_db_connection()
    # ... 实际查询 ...
    return result

result = _fetch_data()
print(result)
```

这样可以脱离 UI 直接调试数据读取逻辑。

---

## 5. 提交修改

```bash
git checkout -b fix/<plugin-name>-xxx
# 修改...
git add plugins/<plugin-name>/
git commit -m "fix(<plugin-name>): <简短描述>"
git push origin fix/<plugin-name>-xxx
# 提 PR，主程序维护者 review
```

---

## 6. 何时考虑新建插件而非修改

- 修改会破坏现有功能 → 新建
- 功能差异太大（如完全不同的卡片）→ 新建
- 需要不同的容器/数据源 → 新建
- 只是改样式/调参数 → 修改
- 加小按钮/加图标 → 修改
- 加数据源但保留原有 → 修改