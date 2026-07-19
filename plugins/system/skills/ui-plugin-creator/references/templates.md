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

from PyQt5.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
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
    FluentLabelBase,
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


def _ctx_font(ctx: dict) -> tuple:
    """从上下文提取 font_family 和 font_size，无则返回 fallback"""
    ff = ctx.get("font_family", "Microsoft YaHei")
    fs = ctx.get("font_size", 14)
    return ff, fs


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


def _make_style(color: str, font_family: str = "", font_size: int = 0, extra: str = "") -> str:
    """生成带字体的 QSS 样式串

    在 _apply_latest_theme 中调用，确保所有 QLabel/按钮的
    颜色和字体都跟随上下文变化。

    Args:
        color: 文字颜色值
        font_family: font-family（可为空，为空时不输出）
        font_size: font-size（0 时不输出）
        extra: 额外的 QSS 片段（如 "font-weight: 600;"）
    """
    parts = [f"color: {color};"]
    if font_family:
        parts.append(f"font-family: '{font_family}'")
    if font_size:
        parts.append(f"font-size: {font_size}px;")
    if extra:
        parts.append(extra)
    return " ".join(parts)


def _adjust_color(hex_color: str, amount: int) -> str:
    """简单调亮/调暗一个 hex 颜色，用于按钮渐变

    Args:
        hex_color: 如 "#62a0ea"
        amount: 调整量，正值调亮，负值调暗
    """
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return hex_color
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        r = max(0, min(255, r + amount))
        g = max(0, min(255, g + amount))
        b = max(0, min(255, b + amount))
        return f"#{r:02x}{g:02x}{b:02x}"
    except ValueError:
        return hex_color


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
        """从上下文拉取最新主题色 + 字体并刷新全部子控件样式

        三层字体策略（重要！）：
        1. self.setFont(QFont(family, size)) — 级联到无显式 font 的子控件
        2. _retheme() 替换 QSS 中的 color + font-size — 覆盖已有 font-size: 11px 的标签
        3. FluentLabelBase（如 StrongBodyLabel）直接 setFont — 覆盖其内部自带的字体

        ⚠️ 常见陷阱：
        - QFont(family, 0) 的 size=0 会使字体极小，必须用真实 font_size
        - QSS 的 font-size 优先级高于 QFont 级联，两者必须同步更新
        - StrongBodyLabel 内部有 self.setFont(self.getFont()) 覆盖父级 QFont
        - 动态创建的子控件创建完必须调 _retheme() 刷新
        """
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
        except Exception:
            return

        # ── 缓存上下文值（供动态创建的子控件使用） ──
        font_family, font_size = _ctx_font(ctx)
        tc = _ctx_text_color(ctx)
        tcs = _ctx_text_color(ctx, secondary=True)
        border_c = _ctx_border_color(ctx)
        self._cached_tc = tc
        self._cached_tcs = tcs
        self._cached_font_family = font_family
        self._cached_font_size = font_size

        # ── 第 1 层：QFont 级联（size 用真实值，不传 0） ──
        if font_family:
            self.setFont(QFont(font_family, font_size if font_size else 14))

        # ── 第 2+3 层：替换 QSS 颜色/字号 + 覆盖 FluentLabelBase ──
        self._retheme()

        # 更新按钮（如果有）
        # 注意：按钮样式通常有固定 background/border，不能简单地 _make_style
        # 建议在 _build_* 方法中存储按钮引用（如 self._my_btn），
        # 然后在 _apply_latest_theme 中用专门的方法更新它的 QSS。
        # 示例：
        # if hasattr(self, '_my_btn'):
        #     self._my_btn.setStyleSheet(self._my_btn_style(accent))

        # 更新搜索框（如果有）
        if hasattr(self, '_search') and self._search is not None:
            try:
                self._search.setStyleSheet(
                    f"background: rgba(128,128,128,0.1); border-radius: 8px; "
                    f"padding: 4px 8px; "
                    + _make_style(tc, font_family, font_size)
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

    def _retheme(self):
        """刷新所有子控件的颜色 + 字号（第 2+3 层字体策略）

        必须在动态创建子控件（如 _render_plugins 创建行）后调用。

        第 2 层：对 QLabel，用 re.sub 替换 QSS 中的 color 和 font-size。
                 保留 font-weight 等其他原有属性。
        第 3 层：对 StrongBodyLabel 等 FluentLabelBase，直接 setFont 覆盖
                 其内部自带的 hardcoded 字体。
        """
        tc = getattr(self, "_cached_tc", "rgba(255,255,255,0.9)")
        ff = getattr(self, "_cached_font_family", "")
        fs = getattr(self, "_cached_font_size", 14)

        for child in self.findChildren(QLabel):
            try:
                # 第 3 层：FluentLabelBase 内部有 self.setFont()，必须直接覆盖
                from qfluentwidgets import FluentLabelBase
                if isinstance(child, FluentLabelBase) and ff:
                    child.setFont(QFont(ff, fs))

                # 第 2 层：替换 QSS 中的 color + font-size
                ss = child.styleSheet()
                if not ss:
                    continue
                import re
                new_ss = re.sub(r"color:\s*[^;]+;", f"color: {tc};", ss)
                if fs:
                    new_ss = re.sub(
                        r"font-size:\s*[^;]+;", f"font-size: {fs}px;", new_ss
                    )
                if ff and f"font-family: '{ff}'" not in new_ss:
                    new_ss += f" font-family: '{ff}';"
                child.setStyleSheet(new_ss)
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
            "QScrollBar:vertical {"
            "    width: 6px; background: transparent;"
            "}"
            "QScrollBar::handle:vertical {"
            "    background: rgba(255,255,255,0.12);"
            "    border-radius: 3px; min-height: 30px;"
            "}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            "    height: 0;"
            "}"
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

---

## 六、集成可复用 widgets 库

> 可复用控件库分多个文件，按需加载（渐进式披露）：
>
> | 文件 | 内容 |
> |------|------|
> | `widgets.md` | 索引 + 设计原则 + 整合示例 + 陷阱速查 |
> | `widgets-statcard.md` | `_StatCard`（多层级统计卡片） |
> | `widgets-charts.md` | `_BarChartWidget` / `_LineChartWidget` / `_ProjectBarWidget` |
> | `widgets-utils.md` | 工具函数（`_format_number` / `_fast_estimate_tokens` / `_short_weekday`） |
> | `widgets-sqlite.md` | SQLite 读取（路径兜底 / N 天窗口 / 字段 fallback） |
> | `widgets-theme.md` | 主题色映射（`ctx` → `QColor` 字典） |
>
> 本节只讲如何与浮动卡片骨架（§一）组合使用。各 widget 的完整代码和设计细节见对应文件。

### 6.1 在浮动卡片中嵌入统计卡片和图表

```python
# plugins/<your-plugin>/ui/cards.py
from .widgets import (
    # 统计卡片 → widgets-statcard.md
    _StatCard,
    # 图表 → widgets-charts.md
    _BarChartWidget,
    _LineChartWidget,
    _ProjectBarWidget,
    # 工具 → widgets-utils.md
    _format_number,
    _format_pct,
    _fast_estimate_tokens,
    _short_weekday,
    # SQLite → widgets-sqlite.md
    _get_db_connection,
    # 主题色 → widgets-theme.md
    _make_chart_colors_from_context,
)


class MyStatsCard(QWidget):
    def _render_content(self, data: dict):
        """渲染所有数据到 _content_layout"""
        # 清空旧内容
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 1. 第一行：3 个 _StatCard（关键指标横排）
        stat_row = QHBoxLayout()
        stat_row.setSpacing(8)

        # 从 data 取数据
        total_sessions = data.get("total_sessions", 0)
        total_messages = data.get("total_messages", 0)
        avg_msgs = data.get("avg_messages_per_session", 0.0)
        avg_daily = round(total_sessions / 14, 1)

        for ic, title, val, sub in [
            (FluentIcon.CHAT, "总会话数", str(total_sessions),
             f"平均 {avg_daily} 次/天"),
            (FluentIcon.MESSAGE, "总消息数", _format_number(total_messages),
             f"平均 {avg_msgs} 条/会话"),
            (FluentIcon.FONT, "总 token 数",
             _format_number(data.get("total_tokens", 0)), "累计消耗"),
        ]:
            card = _StatCard(ic, title, val, sub)
            if self._chart_style:
                card.set_colors(self._chart_style)
            stat_row.addWidget(card)

        stat_widget = QWidget()
        stat_widget.setLayout(stat_row)
        self._content_layout.addWidget(stat_widget)

        # 2. 折线图（趋势）
        daily_tokens = data.get("daily_tokens", [])
        if daily_tokens and any(v for _, v in daily_tokens):
            chart = _LineChartWidget(
                "🔤 估算 Token 用量趋势", daily_tokens, color_key="accent"
            )
            if self._chart_style:
                chart.set_colors(self._chart_style)
            self._content_layout.addWidget(chart)

        # 3. 柱状图（活跃度）
        daily_sessions = data.get("daily_sessions", [])
        if daily_sessions and any(v for _, v in daily_sessions):
            bar = _BarChartWidget("📊 每日会话活跃度", daily_sessions)
            if self._chart_style:
                bar.set_colors(self._chart_style)
            self._content_layout.addWidget(bar)

        # 4. 水平柱状图（分类排行）
        sessions_per_project = data.get("sessions_per_project", {})
        if sessions_per_project:
            sorted_data = sorted(
                sessions_per_project.items(), key=lambda x: -x[1]
            )[:8]
            proj_chart = _ProjectBarWidget(sorted_data)
            if self._chart_style:
                proj_chart.set_colors(self._chart_style)
            self._content_layout.addWidget(proj_chart)
```

### 6.2 主题色注入（拉模型 + chart_style 缓存）

```python
def _apply_latest_theme(self):
    """从 context 拉取最新主题色，缓存到 self._chart_style 供子控件使用"""
    if self._context_provider is None:
        return
    try:
        ctx = self._context_provider()
        self._chart_style = _make_chart_colors_from_context(ctx)
    except Exception:
        self._chart_style = None
    # 注：所有子控件已在 _render_content 中通过 set_colors 注入，无需在这里再调
```

### 6.3 SQLite 异步读取模式

```python
class _DataWorker(QObject):
    """后台线程执行 SQLite 读取"""
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


class MyStatsCard(QWidget):
    def _async_load_data(self):
        self._set_loading(True)
        self._cleanup_worker()

        worker = _DataWorker(self._fetch_data)  # ← 同步函数，在后台线程跑
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_data_loaded)
        worker.error.connect(self._on_load_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._worker, self._worker_thread = worker, thread
        thread.start()

    def _fetch_data(self) -> dict:
        """同步函数：在后台线程执行，从 SQLite 读取数据"""
        conn = _get_db_connection()
        if conn is None:
            return {"error": "无法连接数据库"}
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(message_count), 0) as msgs "
                "FROM sessions WHERE project NOT LIKE '__archived__%'"
            )
            row = cursor.fetchone()
            return {
                "total_sessions": row["cnt"],
                "total_messages": row["msgs"],
                "daily_sessions": [],  # 详见 widgets.md §六.2
            }
        finally:
            conn.close()

    def _on_data_loaded(self, data):
        self._set_loading(False)
        if data.get("error"):
            self._empty_lb.setText(f"加载失败：{data['error'][:60]}")
            self._empty_lb.setVisible(True)
            return
        self._render_content(data)
```

### 6.4 14 天窗口查询模板（最常用 SQL 模式）

> 完整版（含多指标并行查询、错误处理、时区陷阱）见 `widgets-sqlite.md §二`。

```python
from datetime import datetime, timedelta


def _fetch_daily_counts(conn, days: int = 14) -> list:
    """通用 14 天窗口查询：返回 [(日期标签 "MM-DD", 计数值), ...]

    适用：每日会话数、每日消息数、每日 token 数等按日聚合统计。
    """
    today = datetime.now()
    date_labels = [(today - timedelta(days=i)).strftime("%m-%d")
                   for i in range(days - 1, -1, -1)]
    daily_map = {dl: 0 for dl in date_labels}

    cursor = conn.cursor()
    cursor.execute(
        f"SELECT DATE(created_at) as day, COUNT(*) as cnt "
        f"FROM sessions "
        f"WHERE created_at >= date('now', '-{days} days') "
        f"GROUP BY DATE(created_at) ORDER BY day"
    )
    for row in cursor.fetchall():
        try:
            label = datetime.strptime(row["day"], "%Y-%m-%d").strftime("%m-%d")
            daily_map[label] = row["cnt"]
        except (ValueError, TypeError):
            continue

    return [(dl, daily_map[dl]) for dl in date_labels]
```

### 6.5 字段 fallback 模式（新字段缺失时回退估算）

> 完整版（含时间窗口内 fallback、截断长度选择）见 `widgets-sqlite.md §三`。

```python
def _fetch_total_tokens_with_fallback(conn) -> int:
    """context_usage 缺失时回退到 messages 估算（兼容旧数据）

    模式：精确值（context_usage > 0） + 估算值（context_usage = 0，截断 100k 防 OOM）
    """
    cursor = conn.cursor()

    # 1. 精确值
    cursor.execute(
        "SELECT COALESCE(SUM(context_usage), 0) as total "
        "FROM sessions WHERE context_usage > 0"
    )
    total = cursor.fetchone()["total"]

    # 2. 估算值（旧数据）
    cursor.execute(
        "SELECT messages FROM sessions "
        "WHERE (context_usage IS NULL OR context_usage = 0) "
        "AND messages IS NOT NULL AND messages != ''"
    )
    for row in cursor.fetchall():
        msg_data = row["messages"]
        if isinstance(msg_data, (str, bytes)):
            total += _fast_estimate_tokens(str(msg_data)[:100000])

    return total
```

### 6.6 完整项目结构推荐

```
plugins/<your-plugin>/
├── .drifox-plugin/
│   └── plugin.json
└── ui/
    ├── __init__.py       # register_ui 入口
    ├── cards.py          # 主卡片 widget（含 _render_content 用 widgets）
    ├── widgets.py        # 复用 widgets（从 widgets-statcard/charts/utils/sqlite/theme.md 复制）
    └── async_worker.py   # _DataWorker / _async_load_data 模板
```

> `widgets.py` 的内容按需从 `widgets-*.md` 拼装：
> - 想要统计卡片？ → 复制 `widgets-statcard.md`
> - 想要柱状图？ → 复制 `widgets-charts.md §一`
> - 想读 SQLite？ → 复制 `widgets-sqlite.md`
> - ……

或更轻量（无外部依赖时）：

```
plugins/<your-plugin>/
└── ui/
    ├── __init__.py       # register_ui 入口
    └── cards.py          # 主卡片 widget（widgets 直接放这里）
```

### 6.7 常见陷阱

| 陷阱 | 修复 | 详见 |
|------|------|------|
| 忘记 `painter.end()` | 每个提前 `return` 前加 `painter.end()` | `widgets-charts.md §七` |
| 浮动卡片背景偏暗导致黑色字 | `_make_chart_colors_from_context` 中 `text` 固定白色 | `widgets-theme.md §二.2` |
| 标签被顶部裁剪 | 折线图 `top_margin = max_val * 0.3` 留出 30% 空间 | `widgets-charts.md §二.4` |
| 旧数据无新字段 | 模式见 §6.5，截断 100k 防 OOM | `widgets-sqlite.md §三` |
| SQLite 连接阻塞 UI | 用 `_DataWorker` 后台线程跑 `_fetch_data` | `templates.md §一` |
| 主题色不生效 | 确认 `_apply_latest_theme` 在 `show_card` 中调用 | `widgets-theme.md §四` |
| 图表刷新不及时 | `set_data(...)` 后 `self.update()` 触发 `paintEvent` | `widgets-charts.md §五` |
| 数据库被锁 | `timeout=3` + 必要时重试 | `widgets-sqlite.md §四.4.2` |
| 路径层级数错（找不到 DB） | 打印 `_PROJECT_ROOT` 验证 | `widgets-sqlite.md §一.3` |

完整陷阱速查表见 `widgets.md §6`。

---

## 七、通用弹窗模板

> 设计模式见 `patterns.md §7`。本模板提供确认弹窗的完整可复制代码。

### 7.1 确认弹窗类

```python
# ── 统一 MaskDialogBase 风格弹窗 ──
# 放置位置：cards.py 中，_xxxCard 类之前


class _StyledConfirmDialog(MaskDialogBase):
    """统一 MaskDialogBase 风格的确认弹窗 — 参考 ConfirmDialog 设计

    配合 _plugin_styled_dialog() 函数使用，颜色从卡片缓存自动获取。
    """

    def __init__(
        self,
        parent,
        title: str,
        text: str,
        *,
        tc: str,
        ff: str,
        fs: int,
        accent_bg: str,
        card_bg: str,
        border_c: str,
        hover_bg: str,
        yes_text: str = "是",
        no_text: str = "否",
        default_yes: bool = False,
    ):
        super().__init__(parent)
        self._result = False
        self._init_ui(title, text, tc, ff, fs, accent_bg, card_bg, border_c, hover_bg,
                      yes_text, no_text, default_yes)

    def _init_ui(self, title, text, tc, ff, fs, accent_bg, card_bg, border_c, hover_bg,
                 yes_text, no_text, default_yes):
        # ── MaskDialogBase 基础设置 ──
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        # ── 圆角卡片 ──
        self.widget.setObjectName("styledConfirmDialog")
        self.widget.setStyleSheet(f"""
            #styledConfirmDialog {{
                background-color: {card_bg};
                border: 1px solid {border_c};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(28, 28, 28, 20)
        layout.setSpacing(0)

        # ── 标题（粗体，稍大） ──
        title_lb = BodyLabel(title, self.widget)
        title_lb.setWordWrap(True)
        title_lb.setStyleSheet(
            f"color: {tc}; background: transparent; "
            f"{f'font-family: \"{ff}\";' if ff else ''}"
            f"font-size: {max(8, fs + 2)}px; font-weight: bold;"
        )
        layout.addWidget(title_lb)

        layout.addSpacing(12)

        # ── 内容 ──
        content_lb = BodyLabel(text, self.widget)
        content_lb.setWordWrap(True)
        content_lb.setStyleSheet(
            f"color: {tc}; background: transparent; "
            f"{f'font-family: \"{ff}\";' if ff else ''}"
            f"font-size: {max(8, fs - 1)}px; line-height: 1.6;"
        )
        layout.addWidget(content_lb)

        layout.addStretch()

        # ── 按钮行 ──
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        # 取消按钮（有边框，hover 显示 accent 边框）
        cancel_btn = QPushButton(no_text, self.widget)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setFixedHeight(36)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {card_bg};
                color: {tc};
                border: 1px solid {border_c};
                border-radius: 8px;
                padding: 4px 28px;
                {f'font-family: \"{ff}\";' if ff else ''}
                font-size: {max(8, fs - 1)}px;
            }}
            QPushButton:hover {{
                background-color: {hover_bg};
                border-color: {accent_bg};
            }}
        """)
        cancel_btn.clicked.connect(self._on_cancel)

        # 确认按钮（accent 填充，白色粗体）
        confirm_btn = QPushButton(yes_text, self.widget)
        confirm_btn.setCursor(Qt.PointingHandCursor)
        confirm_btn.setFixedHeight(36)
        confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {accent_bg};
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 4px 28px;
                {f'font-family: \"{ff}\";' if ff else ''}
                font-size: {max(8, fs - 1)}px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {accent_bg};
            }}
        """)
        confirm_btn.clicked.connect(self._on_confirm)

        if default_yes:
            confirm_btn.setDefault(True)
            confirm_btn.setFocus()
        else:
            cancel_btn.setDefault(True)
            cancel_btn.setFocus()

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        layout.addLayout(btn_layout)

        self.widget.setFixedSize(400, 200)

    def _on_confirm(self):
        self._result = True
        self.close()

    def _on_cancel(self):
        self._result = False
        self.close()
```

### 7.2 快捷函数

```python
def _styled_confirm(
    parent: QWidget,
    title: str,
    text: str,
    *,
    color_source: Optional[QWidget] = None,
    yes_text: str = "是",
    no_text: str = "否",
    default_yes: bool = False,
) -> bool:
    """快捷确认弹窗 — 从卡片缓存获取主题色

    Args:
        parent: MaskDialogBase 的视觉父窗口（传 self.window()）
        color_source: 颜色查找起点（传 self，即调用的 widget）
    """
    # ── 从 color_source/parent 的父链获取主题色 ──
    tc = "rgba(255,255,255,0.9)"
    ff = ""
    fs = 14
    theme_colors: dict = {}

    p = color_source or parent
    while p is not None:
        cached = getattr(p, "_cached_tc", None)
        if cached is not None:
            tc = cached
            ff = getattr(p, "_cached_font_family", "")
            fs = getattr(p, "_cached_font_size", 14)
            theme_colors = getattr(p, "_cached_theme_colors", {})
            break
        p = p.parent()

    accent_bg = theme_colors.get("accent", "") or ("#62a0ea" if isDarkTheme() else "#2878dc")
    card_bg = theme_colors.get("content_bg", "#2a2a2e" if isDarkTheme() else "#ffffff")
    border_c = theme_colors.get("border", "rgba(128,128,128,0.15)")
    hover_bg = theme_colors.get("hover_bg", "rgba(255,255,255,0.08)" if isDarkTheme() else "rgba(0,0,0,0.06)")

    dialog = _StyledConfirmDialog(
        parent, title, text,
        tc=tc, ff=ff, fs=fs,
        accent_bg=accent_bg, card_bg=card_bg,
        border_c=border_c, hover_bg=hover_bg,
        yes_text=yes_text, no_text=no_text,
        default_yes=default_yes,
    )
    dialog.exec_()
    return dialog._result
```

### 7.3 在卡片中使用

```python
# cards.py 中 _PluginRow._on_uninstall 模式：
def _on_uninstall(self):
    reply = _styled_confirm(
        self.window(),
        "确认卸载",
        f"确定要卸载「{self._plugin.name}」吗？\n此操作不可恢复。",
        color_source=self,
    )
    if reply:
        self._do_uninstall()
```

> **关键**：`parent=self.window()` 保证 `MaskDialogBase` 遮罩覆盖全屏；
> `color_source=self` 保证颜色从调用 widget 的父链向上找到卡片缓存。

### 7.4 补充：卡片 _apply_latest_theme 需缓存 theme_colors

```python
# 在卡片的 _apply_latest_theme 中新增一行：
def _apply_latest_theme(self):
    if self._context_provider is None:
        return
    try:
        ctx = self._context_provider()
    except Exception:
        return

    # ── 缓存上下文值（供弹窗使用） ──
    font_family, font_size = _ctx_font(ctx)
    self._cached_tc = _ctx_text_color(ctx)
    self._cached_tcs = _ctx_text_color(ctx, secondary=True)
    self._cached_font_family = font_family
    self._cached_font_size = font_size
    self._cached_theme_colors = ctx.get("colors", {})  # ← 新增：弹窗颜色源
    ...
```

### 7.5 所需额外 import

```python
from PyQt5.QtGui import QColor
from qfluentwidgets import BodyLabel, MaskDialogBase
```

> `QPushButton`, `QVBoxLayout`, `QHBoxLayout` 在卡片模板中已默认导入。
