# UI 插件代码模板

> 包含浮动卡片、内容块渲染器、消息工厂、完整插件的标准骨架。

---

## 一、浮动卡片模板（最常用）

### 1.1 完整骨架

```python
# -*- coding: utf-8 -*-
"""<CardName> 浮动卡片 — <一句话功能描述>

功能：
- <功能1>
- <功能2>

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作直接通过 stdlib 完成
"""

import traceback
from pathlib import Path
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    ScrollArea,
    StrongBodyLabel,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)
from loguru import logger


# ── 主题色辅助 ──────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    """fallback 文字颜色（无上下文时使用）"""
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    """从上下文 colors 中获取文字颜色，无上下文则回退到 _text_color()"""
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    if val:
        return val
    return _text_color(secondary)


def _ctx_border_color(ctx: dict) -> str:
    """从上下文 colors 中获取边框颜色"""
    return ctx.get("colors", {}).get("border", "rgba(128,128,128,0.15)")


# ── 异步工作器 ──────────────────────────────────────────


class _Worker(QObject):
    """后台执行阻塞操作，通过信号返回结果"""

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


# ── 主卡片 ──────────────────────────────────────────────


class MyCardWidget(QWidget):
    """<CardName> 浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_Worker] = None
        self._setup_ui()
        # 首次显示时由 show_card 触发加载，__init__ 不再自动加载

    # ── 拉模型上下文注入 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        """注入上下文提供函数（由 UIPluginRegistry 调用）"""
        self._context_provider = provider

    def show_card(self):
        """卡片显示时：用最新上下文刷新主题色 + 加载数据"""
        self._apply_latest_theme()
        self._async_refresh()
        self.setVisible(True)

    def _apply_latest_theme(self):
        """从上下文拉取最新主题色并刷新全部子控件样式"""
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
        except Exception:
            return

        tc = _ctx_text_color(ctx)
        tcs = _ctx_text_color(ctx, secondary=True)
        border_c = _ctx_border_color(ctx)

        # 更新所有 label 颜色
        for child in self.findChildren(QLabel):
            try:
                if "font-size" in child.styleSheet() or "color" in child.styleSheet():
                    child.setStyleSheet(
                        f"color: {tc}; background: transparent;"
                    )
            except RuntimeError:
                pass

        # 更新搜索框（如果有）
        if hasattr(self, '_search') and self._search is not None:
            try:
                self._search.setStyleSheet(
                    f"background: rgba(128,128,128,0.1); border-radius: 8px; "
                    f"padding: 4px 8px; color: {tc};"
                )
            except RuntimeError:
                pass

        # 更新分隔线
        try:
            for sep in self.findChildren(QFrame):
                if sep.frameShape() == QFrame.HLine:
                    sep.setStyleSheet(f"background: {border_c}; max-height: 1px;")
        except RuntimeError:
            pass

    # ── 界面 ──

    def _setup_ui(self):
        # 比例高度：不设固定高度，让 sizeHint 决定
        self.setMinimumHeight(0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("MyCardWidget { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 头部 ──
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        ic = IconWidget(FluentIcon.<ICON>, header)  # 选择合适的图标
        ic.setFixedSize(22, 22)
        hly.addWidget(ic)

        tl = StrongBodyLabel("卡片标题", header)
        tl.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        hly.addWidget(tl)

        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        hly.addWidget(self._status_lb)
        hly.addStretch(1)

        # 刷新按钮（如需要）
        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._async_refresh)
        hly.addWidget(self._refresh_btn)

        # 关闭按钮
        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        root.addWidget(header)

        # ── 分隔线 ──
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(sep)

        # ── 滚动内容区 ──
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(12, 8, 12, 8)
        self._content_layout.setSpacing(6)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        # ── 空状态提示 ──
        self._empty = QLabel("暂无数据", self)
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet(
            f"color: {_text_color(secondary=True)}; background: transparent;"
        )
        self._empty.setVisible(False)
        root.addWidget(self._empty)

    # ── 比例高度（与系统卡片一致） ──

    def sizeHint(self):
        """与 SystemCardFrame proportional 模式一致：返回窗口高度的 85%"""
        from PyQt5.QtCore import QSize
        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            return QSize(max(base.width(), 200), int(win.height() * 0.85))
        return base

    def showEvent(self, event):
        """显示时安装窗口 resize 事件过滤器，窗口缩放时通知容器重新展开"""
        super().showEvent(event)
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        """监听窗口 resize，触发 updateGeometry → CardContainer 重算高度"""
        from PyQt5.QtCore import QEvent
        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)

    # ── 异步刷新 ──

    def _async_refresh(self):
        """在后台线程执行数据加载"""
        self._set_loading(True)
        self._cleanup_worker()
        w = _Worker(self._fetch_data)  # _fetch_data 是同步函数
        t = QThread(self)
        w.moveToThread(t)
        t.started.connect(w.run)
        w.finished.connect(self._on_refresh_done)
        w.error.connect(self._on_refresh_error)
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._worker, self._worker_thread = w, t
        t.start()

    def _fetch_data(self):
        """同步函数：在后台线程中执行，返回数据（子类重写）"""
        # e.g. return _scan_files()
        return []

    def _on_refresh_done(self, data):
        """数据加载完成（子类重写）"""
        self._set_loading(False)
        self._render_content(data)

    def _on_refresh_error(self, err: str):
        """数据加载出错"""
        self._set_loading(False)
        self._empty.setText(f"加载失败：{err[:60]}")
        self._empty.setVisible(True)

    def _set_loading(self, loading: bool):
        """设置加载状态"""
        if hasattr(self, '_refresh_btn'):
            self._refresh_btn.setEnabled(not loading)
        if loading:
            self._status_lb.setText("加载中…")
        else:
            self._status_lb.setText("")

    def _render_content(self, data):
        """渲染数据到内容区（子类重写）"""
        # 清空旧内容
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        # 添加新内容...
        pass

    # ── 关闭 ──

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def _cleanup_worker(self):
        """安全清理旧的 worker/thread"""
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(500)
            except RuntimeError:
                pass
            self._worker_thread = None
        self._worker = None

    def deleteLater(self):
        self._cleanup_worker()
        super().deleteLater()
```

### 1.2 按需裁切

按实际需求删减模板：
- **不需要异步**：去掉 `_Worker` 类和所有 `_async_refresh` / `_cleanup_worker` 相关代码
- **不需要主题注入**：去掉 `set_context_provider` / `show_card` / `_apply_latest_theme`
- **不需要刷新按钮**：去掉 `_refresh_btn`
- **不需要状态标签**：去掉 `_status_lb`

---

## 二、内容块渲染器模板

### 2.1 入口

在 `ui/__init__.py` 中注册：

```python
def register_ui(registry):
    # 清理旧子模块缓存（热重载兼容）
    import sys
    prefix = "ui_plugin_<plugin_name>."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .renderers import render_my_content

    registry.register_content_renderer(
        plugin_name="<plugin-name>",
        type_name="<custom_type>",       # content 中 custom_type 字段
        render_func=render_my_content,
        priority=10,
        metadata={"description": "自定义内容渲染"},
    )
```

### 2.2 渲染器函数

```python
# -*- coding: utf-8 -*-
"""内容块渲染器"""
from html import escape
from typing import Any, Dict


def render_my_content(data: Dict[str, Any], context) -> str:
    """渲染自定义内容块

    Args:
        data: 内容数据（来自消息的 data 字段）
        context: 可选上下文

    Returns:
        HTML 字符串
    """
    # 从 data 中提取数据
    items = data.get("items", [])
    if not items:
        return '<div class="my-empty">无数据</div>'

    cards_html = ""
    for item in items:
        name = escape(item.get("name", ""))
        desc = escape(item.get("description", ""))
        cards_html += f"""
        <div class="my-card">
            <h4>{name}</h4>
            <p>{desc}</p>
            <button class="my-btn" data-id="{escape(item.get('id', ''))}">操作</button>
        </div>
        """

    return f"""
    <div class="my-container">
        {cards_html}
    </div>
    <style>
        .my-container {{ ... }}
        .my-card {{ ... }}
        .my-btn {{ ... }}
    </style>
    """
```

> 交互按钮可以用 `data-*` 属性，通过 WebView 的 bridge 透传到 Python 侧。

---

## 三、消息元素工厂模板

```python
# ui/__init__.py
def register_ui(registry):
    from .factories import my_message_condition, my_message_factory

    registry.register_message_factory(
        plugin_name="<plugin-name>",
        name="my_custom_message",
        condition_func=my_message_condition,
        factory_func=my_message_factory,
        priority=10,
    )


# ui/factories.py
def my_message_condition(message: dict) -> bool:
    """判断消息是否由此工厂处理"""
    return message.get("role") == "assistant" and "my_flag" in message


def my_message_factory(message: dict, parent) -> QWidget:
    """创建自定义消息 widget"""
    # 返回 QWidget，兜底走默认 MessageCard
    return None  # 暂时回退到默认渲染
```

---

## 四、完整插件注册入口 + plugin.json

### 4.1 plugin.json

```json
{
    "name": "<plugin-name>",
    "description": "<插件描述>",
    "version": "0.1.0",
    "author": {
        "name": "DriFox Contributors"
    },
    "type": "system",
    "components": {
        "ui": true
    }
}
```

> 如果插件放在 `plugins/<plugin-name>/`（系统插件），`type` 可以省略。
> 用户插件放在 `~/.drifox/plugins/<plugin-name>/`。

### 4.2 ui/__init__.py 完整模板

```python
# -*- coding: utf-8 -*-
"""<plugin-name> UI 组件入口"""

import sys
from pathlib import Path

from loguru import logger


def register_ui(registry):
    """注册 <plugin-name> 的 UI 组件

    热重载兼容：
    清理 sys.modules 中残留的子模块缓存，确保 Python 重新从 .py 源文件编译，
    避免旧的 __pycache__/.pyc 导致 NameError 等异常。

    注意：不主动删除 __pycache__/ 目录，Python 的 import 系统已通过
    源文件时间戳自动判断是否需要重新编译 .pyc，主动删除只会触发不必要的
    文件系统变更，导致插件热更新监视器误判为跨插件修改。

    外部依赖（如有）：
    如果插件需要 PyInstaller 未声明的纯 Python 包，可将包目录复制到
    ui/_vendor/，本函数开头会自动加入 sys.path。详见 SKILL.md §4.7。
    """
    # ── 加载 _vendor/（如有） ──
    vendor_dir = Path(__file__).parent / "_vendor"
    if vendor_dir.exists() and str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))
        logger.info(f"[<plugin-name>] _vendor/ 已加入 sys.path: {vendor_dir}")

    # 清理旧子模块缓存（避免热重载时 Python 用旧 sys.modules 缓存）
    safe_name = "<plugin_name>"  # 连字符替换为下划线
    prefix = f"ui_plugin_{safe_name}."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import MyCardWidget

    # 注册浮动卡片（自动注册对应命令 /<card-id>）
    # container="bottom"：与系统配置卡片一致，显示在 chat_layout 下方并隐藏输入区
    registry.register_floating_card(
        plugin_name="<plugin-name>",
        card_id="<card-id>",
        widget_class=MyCardWidget,
        container="bottom",          # "bottom" | "top"
        title="卡片标题",
        default_visible=False,
    )

    # 注册内容块渲染器（可选）
    # from .renderers import render_my_content
    # registry.register_content_renderer(...)

    logger.info("[<plugin-name>] UI components registered")
```

---

## 五、外部依赖管理（_vendor/ 模式）

### 5.1 适用场景

> **UI 插件打包后从市场下载使用，不能再次打包主程序。**

PyInstaller `--onedir` 打包后，`dist/Drifox/_internal/` 只包含构建期检测到的第三方包。如果某个 UI 插件需要构建时未声明的纯 Python 包（如 `requests`、`markdown`、`jsonschema`），运行时 `import` 会失败。

**解决方案**：将包目录完整复制到 `plugins/<plugin>/ui/_vendor/`，在 `register_ui` 开头加入 `sys.path`。

### 5.2 目录结构

```
plugins/<plugin-name>/
└── ui/
    ├── __init__.py           # 含 _vendor/ 加载逻辑
    ├── cards.py              # 可 import _vendor/ 中的包
    └── _vendor/              # 第三方纯 Python 依赖
        ├── requests/
        │   ├── __init__.py
        │   ├── api.py
        │   ├── models.py
        │   └── ...
        └── markdown/
            ├── __init__.py
            └── ...
```

### 5.3 复制依赖到 _vendor/

```bash
# 从开发环境复制（PowerShell）
$pkg = python -c "import requests, os; print(os.path.dirname(requests.__file__))"
Copy-Item -Path $pkg -Destination "plugins/my-plugin/ui/_vendor/requests" -Recurse

# Bash / Git Bash
python -c "import requests, os, shutil; shutil.copytree(os.path.dirname(requests.__file__), 'plugins/my-plugin/ui/_vendor/requests')"

# 复制后清理 __pycache__/（可选，运行时会自动生成）
Get-ChildItem plugins/my-plugin/ui/_vendor -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
```

**注意事项**：
- ✅ **必须**：复制完整包目录（含所有子模块和 `__init__.py`）
- ✅ **建议**：删除 `__pycache__/` 和 `.pyc`（减小体积）
- ⚠️ **不能**：直接复制 `site-packages/<pkg>.dist-info/`（PyInstaller 不需要，但会污染插件）
- ⚠️ **限制**：仅限**纯 Python 包**或**目标平台 + Python 版本完全一致**的 C 扩展包

### 5.4 cards.py 中使用 _vendor/ 包

```python
# -*- coding: utf-8 -*-
"""<plugin-name> 浮动卡片"""

# 在 register_ui 已经把 _vendor/ 加入 sys.path 后，
# 这里直接 import 即可，运行时从 _vendor/ 加载
import requests
from markdown import markdown

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget


class MyCard(QWidget):
    def fetch_data(self):
        # 使用 _vendor/ 中的 requests
        resp = requests.get("https://example.com/api/data")
        resp.raise_for_status()
        return resp.json()

    def render_markdown(self, text: str) -> str:
        # 使用 _vendor/ 中的 markdown
        return markdown(text)
```

### 5.5 完整 register_ui 模板（含 _vendor/）

```python
# -*- coding: utf-8 -*-
"""<plugin-name> UI 组件入口"""

import sys
from pathlib import Path

from loguru import logger


def register_ui(registry):
    """注册 <plugin-name> 的 UI 组件

    外部依赖（vendored 到 ui/_vendor/）：
        - requests: HTTP 请求
        - markdown: Markdown 渲染

    _vendor/ 机制：
        把第三方纯 Python 包放在 ui/_vendor/，本函数开头将其加入 sys.path。
        打包后即便 PyInstaller 未声明该包，import 也能成功。
        仅限纯 Python 包；含 C 扩展的包不支持（需主程序构建期声明）。

    ⚠️ sys.modules 缓存陷阱（关键！）：
        Python 的 import 机制：如果模块已在 sys.modules 中，直接返回缓存，**完全忽略**
        sys.path 顺序。如果 DriFox 启动时（或另一个插件）提前 import 了 _vendor/ 里的
        同名包（如 darkdetect），register_ui 中 sys.path.insert(0, vendor_dir) 不生效，
        cards.py 中 `import darkdetect` 仍会从原来的路径加载。
        必须在 sys.path.insert 之前显式删除缓存，强制重新从 _vendor/ 加载。

    热重载兼容：
        重新执行 register_ui 时 sys.path.insert 是幂等操作（前面的判断会跳过）；
        旧 sys.modules 子模块缓存会被清理，确保新代码生效。
    """
    # ── 1. 清理可能已缓存的 vendored 包（关键！） ──
    vendor_packages = ["requests", "markdown"]  # ← 替换成实际的 vendored 包名
    cleaned = []
    for pkg_name in vendor_packages:
        for mod_name in list(sys.modules.keys()):
            if mod_name == pkg_name or mod_name.startswith(f"{pkg_name}."):
                mod_obj = sys.modules[mod_name]
                mod_file = getattr(mod_obj, "__file__", "") or ""
                # 仅删除非 _vendor/ 来源的缓存（避免热重载时重复清理）
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

    # ── 3. 验证 _vendor/ 中的包真的可用（便于诊断） ──
    try:
        import requests  # noqa: F401

        if "_vendor" in requests.__file__:
            logger.info(f"[<plugin-name>] ✓ requests 已从 _vendor/ 加载: {requests.__file__}")
        else:
            logger.error(f"[<plugin-name>] ✗ requests 仍从非 _vendor/ 加载: {requests.__file__}")
    except ImportError as e:
        logger.error(f"[<plugin-name>] ✗ requests 加载失败: {e}")

    # ── 4. 清理旧子模块缓存（热重载兼容） ──
    safe_name = "<plugin-name>".replace("-", "_").replace(":", "_")
    prefix = f"ui_plugin_{safe_name}."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    # ── 5. 注册组件 ──
    from .cards import MyCardWidget

    registry.register_floating_card(
        plugin_name="<plugin-name>",
        card_id="<card-id>",
        widget_class=MyCardWidget,
        container="bottom",
        title="<卡片标题>",
        default_visible=False,
    )

    logger.info("[<plugin-name>] UI components registered")
```

### 5.5.1 常见陷阱：sys.modules 缓存

**症状**：在 dev 环境下运行插件，明明 `register_ui` 里把 `_vendor/` 加到了 `sys.path[0]`，但 `cards.py` 中 `import xxx` 仍然从 site-packages / venv 加载。

**原因**：
```python
# 假设 DriFox 启动时已 import 了 darkdetect（用于主题检测）
import darkdetect  # ← 现在 darkdetect 缓存在 sys.modules

# 你的 register_ui 执行
sys.path.insert(0, vendor_dir)  # sys.path 顺序已对
import darkdetect  # ← 但 sys.modules 已有缓存，直接返回，**不查 sys.path**
```

**修复**：必须在 `sys.path.insert` 之前清理缓存：

```python
# 正确的 register_ui 开头
for pkg_name in ["darkdetect", "packaging"]:
    for mod_name in list(sys.modules.keys()):
        if mod_name == pkg_name or mod_name.startswith(f"{pkg_name}."):
            mod_obj = sys.modules[mod_name]
            mod_file = getattr(mod_obj, "__file__", "") or ""
            if "_vendor" not in mod_file:
                del sys.modules[mod_name]
```

**判断是否真的从 `_vendor/` 加载**：在 `register_ui` 末尾加断言：

```python
import requests
assert "_vendor" in requests.__file__, (
    f"requests 应从 _vendor/ 加载，实际: {requests.__file__}"
)
```

**打包环境 vs 开发环境的差异**：
- **PyInstaller 打包后**：vendored 包根本不在 `_internal/` 中，第一次 import 必从 `_vendor/` 加载，没有此陷阱
- **dev 环境**：如果任何代码（包括主程序、其他插件）提前 import 了 vendored 包，必须手动清理缓存

### 5.6 验证 _vendor/ 在 PyInstaller 打包后可用

参考 `references/testing-vendor.md` 的完整测试脚本。
