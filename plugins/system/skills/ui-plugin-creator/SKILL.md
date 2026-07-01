---
name: ui-plugin-creator
description: DriFox UI 插件开发技能。用于创建、修改、调试UI插件（浮动卡片 / 内容块渲染器 / 消息元素工厂）。
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
| "插件需要 requests/PIL/... 等第三方包""打包后再加依赖" | **外部依赖（_vendor/）** | `templates.md` §五 |
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
    ├── renderers.py          # 内容块渲染器（可选）
    └── _vendor/              # 可选：第三方纯 Python 依赖（见 §4.7）
        └── <package>/        # 整个包目录
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

### 4.7 外部依赖管理（_vendor/ 模式）

**场景**：UI 插件从 PyInstaller exe 解包后下载到 `~/.drifox/plugins/` 使用，**不能再次打包**。

**核心约束**：
- PyInstaller `--onedir` 打包后，`_internal/` 只包含构建时检测到的包
- 用户从市场下载的插件无法重新打包
- 含 C 扩展的包（`.pyd`/`.so`）不能跨版本/架构复制，必须在主程序构建期声明

**解决方案**：将**纯 Python**依赖放在 `plugins/<plugin>/ui/_vendor/`，在 `register_ui` 开头加入 `sys.path`。

#### 4.7.1 目录约定

```
plugins/<plugin-name>/
└── ui/
    ├── __init__.py           # register_ui 中 sys.path.insert(0, vendor_dir)
    ├── cards.py
    ├── renderers.py
    └── _vendor/              # 第三方纯 Python 包（完整复制）
        ├── requests/
        └── markdown/
```

#### 4.7.2 register_ui 标准模板（含 _vendor/）

```python
# -*- coding: utf-8 -*-
"""<plugin-name> UI 组件入口"""

import sys
from pathlib import Path

from loguru import logger


def register_ui(registry):
    """注册 <plugin-name> 的 UI 组件

    外部依赖（打包到 ui/_vendor/）：
        - <package-a>: <用途>
        - <package-b>: <用途>

    _vendor/ 机制：
        把第三方纯 Python 包放在 ui/_vendor/，本函数开头将其加入 sys.path。
        打包后即便 PyInstaller 未声明该包，import 也能成功。
        仅限纯 Python 包；含 C 扩展的包不支持（需主程序构建期声明）。

    ⚠️ sys.modules 缓存陷阱（dev 环境必看）：
        Python 的 import 机制：如果模块已在 sys.modules 中，直接返回缓存，**忽略**
        sys.path 顺序。如果 DriFox 启动时已 import 了同名的 vendored 包，
        `sys.path.insert` 不会生效，必须显式删缓存。详见 §4.7.6。
    """
    # ── 1. 清理可能已缓存的 vendored 包（关键！见 §4.7.6） ──
    vendor_packages = ["<package-a>", "<package-b>"]
    cleaned = []
    for pkg_name in vendor_packages:
        for mod_name in list(sys.modules.keys()):
            if mod_name == pkg_name or mod_name.startswith(f"{pkg_name}."):
                mod_obj = sys.modules[mod_name]
                mod_file = getattr(mod_obj, "__file__", "") or ""
                if "_vendor" not in mod_file:
                    del sys.modules[mod_name]
                    cleaned.append(mod_name)
    if cleaned:
        logger.info(f"[<plugin-name>] 已清理 vendored 包缓存: {cleaned}")

    # ── 2. 加载 _vendor/ ──
    vendor_dir = Path(__file__).parent / "_vendor"
    if vendor_dir.exists() and str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))
        logger.info(f"[<plugin-name>] _vendor/ 已加入 sys.path: {vendor_dir}")
    else:
        logger.warning(
            f"[<plugin-name>] _vendor/ 不存在或已加入: {vendor_dir} "
            f"(exists={vendor_dir.exists()})"
        )

    # ── 3. 验证 _vendor/ 中的包真的可用 ──
    try:
        import <package-a>

        if "_vendor" in <package-a>.__file__:
            logger.info(f"[<plugin-name>] ✓ <package-a> 已从 _vendor/ 加载")
        else:
            logger.error(f"[<plugin-name>] ✗ <package-a> 仍从非 _vendor/ 加载: {<package-a>.__file__}")
    except ImportError as e:
        logger.error(f"[<plugin-name>] ✗ <package-a> 加载失败: {e}")

    # ── 4. 清理旧子模块缓存（热重载兼容） ──
    safe_name = "<plugin-name>".replace("-", "_").replace(":", "_")
    prefix = f"ui_plugin_{safe_name}."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import MyCard

    registry.register_floating_card(
        plugin_name="<plugin-name>",
        card_id="<card-id>",
        widget_class=MyCard,
        container="bottom",
        title="<卡片标题>",
        default_visible=False,
    )
    logger.info("[<plugin-name>] UI components registered")
```

#### 4.7.3 ⚠️ sys.modules 缓存陷阱（dev 环境最常见的坑）

**症状**：在 dev 环境下运行插件，`register_ui` 里把 `_vendor/` 加到了 `sys.path[0]`，但 `cards.py` 中 `import xxx` 仍然从 site-packages 加载，路径显示是 venv 不是 `_vendor/`。

**原因**：
```python
# 假设 DriFox 启动时已 import 了 darkdetect（用于主题检测）
import darkdetect  # ← 现在 darkdetect 缓存在 sys.modules

# 你的 register_ui 执行
sys.path.insert(0, vendor_dir)  # sys.path 顺序已对
import darkdetect  # ← 但 sys.modules 已有缓存，直接返回，**不查 sys.path**
```

**修复**：必须在 `sys.path.insert` 之前清理缓存，详见 §4.7.2 模板步骤 1。

**判断是否真的从 `_vendor/` 加载**：
```python
import darkdetect
assert "_vendor" in darkdetect.__file__, f"应从 _vendor/ 加载，实际: {darkdetect.__file__}"
```

**打包环境 vs dev 环境差异**：
- **PyInstaller 打包后**：vendored 包根本不在 `_internal/` 中，第一次 import 必从 `_vendor/` 加载，没有此陷阱
- **dev 环境**：如果任何代码（包括主程序、其他插件）提前 import 了同名包，必须手动清理缓存

**调试技巧**：在 `register_ui` 末尾加诊断日志，确认加载路径：
```python
import darkdetect
logger.info(f"[my-plugin] darkdetect loaded from: {darkdetect.__file__}")
if "_vendor" not in darkdetect.__file__:
    logger.error("[my-plugin] ⚠️ darkdetect 未从 _vendor/ 加载！")
```

#### 4.7.4 复制依赖到 _vendor/

```bash
# 从 dev 环境复制完整包
python -c "import requests, os; src=os.path.dirname(requests.__file__); \
  import shutil; shutil.copytree(src, 'plugins/my-plugin/ui/_vendor/requests')"
```

**关键点**：
- 必须复制**完整包目录**（含 `__init__.py`、所有子模块）
- `__pycache__/` 可省略（运行时会自动生成）
- 包内自带的 `.so`/`.dll`（非 Python 标准库依赖）也可复制，但**必须**匹配目标平台的 Python 版本和架构

#### 4.7.5 何时用 _vendor/，何时不用

| 场景 | 推荐做法 |
|------|---------|
| 纯 Python 包（`requests`, `markdown`, `jsonschema`） | ✅ **用 _vendor/** |
| 含 C 扩展但 PyInstaller 已声明（如 `numpy`, `PIL`） | ❌ 直接 `import` 即可 |
| 含 C 扩展但 PyInstaller 未声明 | ⚠️ **不要用 _vendor/**，跨平台/版本会崩溃；改为主程序构建期声明（`build.py` 加 `--hidden-import`） |
| 巨型包（`numpy`, `torch`, `pandas`） | ❌ 不适合 _vendor/（体积太大），应要求用户安装主程序依赖 |
| 动态下载（用户运行插件时拉取） | ⚠️ 可以但要管理下载目录和清理逻辑，更复杂 |

#### 4.7.6 验证 _vendor/ 是否真的可用

由于 PyInstaller 打包后不能直接 `python -c "import my_plugin"`，需要**打一个测试 exe** 验证：

```bash
# 最小测试：在 plugins/ 目录下放你的插件，跑一个 test_main.py 用 importlib 加载
# 打包 → 把 plugins/ 复制到 dist/<exe>/ 旁 → 运行 exe 验证
```

参考 `references/testing-vendor.md` 的完整测试脚本与打包流程。

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

### 6.1 外部依赖（_vendor/）额外检查

- [ ] `ui/_vendor/<package>/` 下每个包都有 `__init__.py`
- [ ] `register_ui` 开头将 `vendor_dir` 加入 `sys.path`，并用 `Path(__file__).parent` 而非硬编码路径
- [ ] **dev 环境下**：register_ui 开头**先清理 sys.modules 中已缓存的 vendored 包**（避免被提前 import 的同名包绕过 _vendor/）
- [ ] **register_ui 末尾**：加 `assert "_vendor" in <pkg>.__file__` 或日志，确认实际加载来源
- [ ] 热重载后 `vendor_dir` 仍生效（`Path(__file__)` 在热重载时指向新位置，`sys.path.insert` 重复执行是安全的）
- [ ] 不在 `_vendor/` 放含 C 扩展但跨平台的包（`numpy`、`Pillow` 等），除非能保证目标环境完全匹配
- [ ] 用 PyInstaller 打包测试：把整个 `plugins/<plugin-name>/` 复制到 `dist/<exe>/`，运行后能 `import` 第三方包并调用 API

---

## 7. 与其它技能的衔接

```
ui-plugin-creator (本技能)
  ├─ 需要 brainstorm 功能设计 → 先 brainstorming
  ├─ 遵循 drifox-dev 的编码规范 → 按需读 drifox-dev/references/conventions.md
  └─ 复杂功能拆多步 → subagent-driven-development
```
