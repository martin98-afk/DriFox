---
name: ui-plugin-creator
description: "DriFox UI 插件开发技能。用于创建、修改、调试UI插件（浮动卡片 / 内容块渲染器 / 消息元素工厂）。当用户说"做个插件卡片""加个UI插件""开发个UI组件""写个浮动卡片""做个插件市场界面""搞个统计卡片""注册新命令卡片""自定义消息渲染"时，必须加载本技能。即使看起来简单的UI改动（如"加个设置界面""显示个图表"）也应加载本技能——UI插件架构涉及的约定（注册模式、热重载、上下文注入、比例高度、闭包约束）容易遗漏，不加载容易写出不合规的代码。"
---

# ui-plugin-creator —— DriFox UI 插件开发技能

> 快速、规范地从用户意图转化为可工作的UI插件代码。

---

## 0. 加载流程

```
Step 1  理解意图 → 按 §1 决策树分派组件类型
Step 2  读 references/ 对应文件
Step 3  按 §2 工作流推进
Step 4  按 §3-4 模板生成代码
Step 5  验证：ruff check + 实际加载测试
```

---

## 1. 组件类型决策树

根据用户意图选择要创建的UI组件类型：

| 用户说 | 组件类型 | 必读 references |
|--------|---------|-----------------|
| "加个卡片""做个设置界面""显示统计面板""搞个管理界面" | **浮动卡片**（FloatingCard） | `templates.md` §一 |
| "在聊天里显示HTML""渲染自定义内容""做个消息卡片样式" | **内容块渲染器**（ContentRenderer） | `templates.md` §二 |
| "替换消息气泡""自定义消息控件""做个消息widget" | **消息元素工厂**（MessageFactory） | `templates.md` §三 |
| "做个插件市场""安装插件""插件管理" | **完整插件**（全组件） | `templates.md` §四 + `architecture.md` |
| "改现有插件""加个按钮""调样式" | **修改现有插件** | 先读目标插件的代码 |

> ⚠️ **新插件优先走浮动卡片**——这是最常见的UI插件形态。
> ⚠️ **内容渲染器只做"展示"，交互按钮用 data 属性桥接。**
> ⚠️ **消息工厂是高级用法——99% 场景用浮动卡片就够。**

---

## 2. 开发工作流（新建插件）

### 2.1 澄清需求（必走，即使看起来简单）

用 1-2 问搞清楚：
- 这组件放在**底部**（`container="bottom"`，与系统配置卡片一致，显示在聊天下方并隐藏输入区）还是**顶部**（`container="top"`，独立浮动）？
- 数据来源是**本地**（SQLite / 文件扫描）还是**远程**（HTTP API）？
- 需要**异步操作**吗（列表加载、安装/卸载等）？→ 用 QThread + pyqtSignal
- 需要**上下文注入**吗（跟随主题色/字体变化）？→ 用 set_context_provider + 拉模型

### 2.2 创建插件目录结构

```
plugins/<plugin-name>/
├── .drifox-plugin/
│   └── plugin.json          # 插件清单，声明 "ui": true
└── ui/
    ├── __init__.py           # register_ui 入口
    ├── cards.py              # 浮动卡片 widget（可选）
    └── renderers.py          # 内容块渲染器（可选）
```

### 2.3 按 §3 模板生成代码

### 2.4 验证

```bash
ruff check plugins/<plugin-name>/ui/
# 然后重启 DriFox 或触发插件热重载
```

---

## 3. 代码模板

### 3.1 浮动卡片（最常用）

参考 `references/templates.md` §一，包含：
- 完整 QWidget 子类骨架
- `set_context_provider` + `show_card` + `_apply_latest_theme`（主题注入）
- 异步工作器（QThread + pyqtSignal）
- `sizeHint` + `showEvent` + `eventFilter`（比例高度 + 窗口resize响应）
- 头部（图标 + 标题 + 刷新 + 关闭）
- ScrollArea 内容区 + 分隔线
- 空状态 / 加载中占位

**设计约束（插件闭包）**：
- ❌ 不导入 `app.core` 或 `app.widgets` 内部的任何模块
- ✅ 用 `pathlib` + `shutil` + `sqlite3` 等stdlib操作
- ✅ 用 `qfluentwidgets.isDarkTheme()` 做主题检测（但优先用上下文注入的colors）
- ✅ 用 `loguru` 做日志

### 3.2 内容块渲染器

参考 `references/templates.md` §二。

### 3.3 完整插件注册入口

参考 `references/templates.md` §四，包含：
- `register_ui(registry)` 函数
- 热重载兼容的 `sys.modules` 清理
- 同时注册多种组件

---

## 4. 核心模式与约定

### 4.1 上下文注入（拉模型）

**不要直接推数据**。卡片通过 `set_context_provider(provider)` 注入一个**无参函数**，在需要时自行调用获取最新上下文（主题色、字体、项目信息等）。

```python
def set_context_provider(self, provider):
    self._context_provider = provider

def show_card(self):
    self._apply_latest_theme()  # 显示时拉取最新主题
    self._load_data()
    self.setVisible(True)

def _apply_latest_theme(self):
    ctx = self._context_provider()
    colors = ctx.get("colors", {})
    # 用 colors 中的 text_primary / text_secondary / border / accent 等刷新样式
```

### 4.2 比例高度（与系统卡片一致）

所有浮动卡片必须移除固定 `setMinimumHeight(400)`，改用 `sizeHint()` + 窗口 resize 响应：

```python
def sizeHint(self):
    from PyQt5.QtCore import QSize
    base = super().sizeHint()
    win = self.window()
    if win and win.height() > 0:
        return QSize(max(base.width(), 200), int(win.height() * 0.85))
    return base

def showEvent(self, event):
    super().showEvent(event)
    win = self.window()
    if win:
        win.installEventFilter(self)
        self.updateGeometry()

def eventFilter(self, obj, event):
    from PyQt5.QtCore import QEvent
    if obj is self.window() and event.type() == QEvent.Resize:
        self.updateGeometry()
    return super().eventFilter(obj, event)
```

### 4.3 异步操作

```python
class _Worker(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")

# 使用：
def _async_refresh(self):
    self._cleanup_worker()
    w = _Worker(_fetch_data)
    t = QThread(self)
    w.moveToThread(t)
    t.started.connect(w.run)
    w.finished.connect(self._on_done)
    w.error.connect(self._on_error)
    w.finished.connect(t.quit)
    w.error.connect(t.quit)
    w.finished.connect(w.deleteLater)
    w.error.connect(w.deleteLater)
    t.finished.connect(t.deleteLater)
    self._worker, self._worker_thread = w, t
    t.start()
```

### 4.4 热重载兼容

在 `ui/__init__.py` 中必须清理旧模块缓存：

```python
import sys
prefix = "ui_plugin_{safe_name}."
stale = [k for k in sys.modules if k.startswith(prefix)]
for k in stale:
    del sys.modules[k]
```

### 4.5 主题色获取

从上下文 colors 中取，有 fallback：

```python
def _ctx_text_color(ctx, secondary=False):
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    if val:
        return val
    return _text_color(secondary)  # fallback
```

### 4.6 信号链（卡片关闭）

卡片必须发射 `closed` 信号，让 `UIPluginRegistry` 同步 `CardManager` 状态：

```python
class MyCard(QWidget):
    closed = pyqtSignal()

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()
```

---

## 5. 修改现有插件

1. 先读目标插件的 `ui/__init__.py` 了解已注册的组件
2. 找到对应 `cards.py` / `renderers.py` 中的类和方法
3. 修改后运行 `ruff check plugins/<plugin-name>/ui/`
4. 触发热重载验证

---

## 6. 验证清单

- [ ] `ruff check` 通过
- [ ] `plugin.json` 声明了 `"ui": true`
- [ ] `ui/__init__.py` 有 `register_ui(registry)` 函数
- [ ] 热重载兼容（清理 `sys.modules`）
- [ ] 浮动卡片实现了 `closed` 信号
- [ ] 浮动卡片没有 `setMinimumHeight(400)` 固定高度
- [ ] 浮动卡片实现了 `sizeHint()` + `showEvent/eventFilter` 比例高度
- [ ] 浮动卡片实现了 `set_context_provider` + `show_card` + `_apply_latest_theme`
- [ ] 没有导入 `app.core` 或 `app.widgets`
- [ ] 异步操作正确管理 worker 生命周期（`_cleanup_worker` + `deleteLater`）

---

## 7. 与其它技能的衔接

```
ui-plugin-creator (本技能)
  ├─ 需要 brainstorm 功能设计 → 先 brainstorming
  ├─ 遵循 drifox-dev 的编码规范 → 按需读 drifox-dev/references/conventions.md
  └─ 复杂功能拆多步 → subagent-driven-development
```
