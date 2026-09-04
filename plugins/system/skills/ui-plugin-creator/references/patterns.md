# 核心模式与约定

> 本文是 `SKILL.md §4` 的完整展开。
> 模板见 `references/templates.md`，可复用 widgets 见 `references/widgets.md`。

---

## 📑 目录

> 行号对应本文件当前版本，编辑后请更新。

- **1. 上下文注入（拉模型）** — L50–L203
  - 1.1 原因 — L54–L59
  - 1.2 标准实现 — L60–L84
  - 1.3 ctx 结构约定 — L85–L106
  - 1.4 字体注入（重要 — 易踩坑⚠️） — L107–L203
    - 三层字体策略 — L111–L203
- **2. 比例高度（与系统卡片一致）** — L204–L249
  - 2.1 为什么不能用固定高度 — L208–L212
  - 2.2 标准实现 — L213–L240
  - 2.3 关键点 — L241–L249
- **3. 异步操作** — L250–L340
  - 3.1 标准 Worker 模板 — L254–L278
  - 3.2 完整生命周期管理 — L279–L317
  - 3.3 关键点 — L318–L324
  - 3.4 加载状态 UI 同步 — L325–L340
- **4. 热重载兼容** — L341–L374
  - 4.1 标准实现 — L345–L362
  - 4.2 关于 `__pycache__/` — L363–L368
  - 4.3 含 _vendor/ 的特殊情况 — L369–L374
- **5. 主题色获取** — L375–L403
  - 5.1 推荐：上下文注入 — L377–L389
  - 5.2 不推荐：isDarkTheme() — L390–L403
- **6. 信号链（卡片关闭）** — L404–L427
  - 6.1 标准实现 — L408–L418
  - 6.2 为什么必须发射 — L419–L427
- **7. 弹窗/确认对话框 — 统一 MaskDialogBase 风格** — L428–L490
  - 7.1 为什么不用 QMessageBox — L432–L437
  - 7.2 颜色从哪里来 — L438–L461
  - 7.3 关键设计 — L462–L471
  - 7.4 调用模式 — L472–L490
- **8. 外部依赖管理（_vendor/ 模式）** — L491–L519
  - 8.1 适用场景 — L495–L502
  - 8.2 解决方案 — L503–L506
  - 8.3 何时用 _vendor/ — L507–L519
- **9. 其他约定** — L520–L542
  - 8.1 日志 — L522–L530
  - 8.2 异常处理 — L531–L536
  - 8.3 命名 — L537–L542
- **10. 全屏覆盖窗（选区截图 / 全屏遮罩类动作）** — L551–L630
  - 10.1 冻结底图方案（核心决策） — L556
  - 10.2 窗口属性与生命周期 — L572
  - 10.3 高 DPI 坐标换算 — L600
  - 10.4 测试要点（pytest-qt） — L619
## 1. 上下文注入（拉模型）

**核心原则**：**不要直接推数据**。卡片通过 `set_context_provider(provider)` 注入一个**无参函数**，在需要时自行调用获取最新上下文（主题色、字体、项目信息等）。

### 1.1 原因

- 推送模型在主题色变化时漏更新
- 推送时机不确定，可能在卡片销毁后到达
- 拉模型让卡片完全掌控何时拿数据

### 1.2 标准实现

```python
class MyCard(QWidget):
    def set_context_provider(self, provider: Callable[[], dict]):
        """注入上下文提供函数（由 UIPluginRegistry 调用）"""
        self._context_provider = provider

    def show_card(self):
        """卡片显示时：用最新上下文刷新主题色 + 加载数据"""
        self._apply_latest_theme()  # ← 显示时拉一次
        self._async_load_data()
        self.setVisible(True)

    def _apply_latest_theme(self):
        """从上下文拉取最新主题色并刷新所有子控件样式"""
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
            # 用 ctx.colors / ctx.is_dark / ctx.font_family 等
        except Exception:
            return
```

### 1.3 ctx 结构约定

主程序保证 ctx 包含以下字段（不保证所有插件都用得到）：

```python
ctx = {
    "colors": {
        "accent": "#2878dc",
        "success": "#00a888",
        "border": "#ffffff1e",
        "text_primary": "rgba(255,255,255,0.9)",
        "text_secondary": "rgba(255,255,255,0.55)",
        # ... 更多键
    },
    "is_dark": True,
    "font_family": "Microsoft YaHei",
    "font_size": 14,
}
```

> 永远不要假设某个键一定存在——用 `.get()` + fallback。

### 1.4 字体注入（重要 — 易踩坑⚠️）

ctx 中的 `font_family` 和 `font_size` 必须应用到所有子控件，否则插件字体和主程序不一致。

#### 三层字体策略

单靠 `self.setFont(QFont(family, size))` 是**不够的**，原因有三：

| 层 | 策略 | 解决什么问题 |
|----|------|-------------|
| **第 1 层** | `self.setFont(QFont(family, size))` | 没有显式 QSS 字体的子控件获得级联 |
| **第 2 层** | `re.sub` 替换 QSS 中 `font-size` | QSS 的 `font-size` 优先级高于 QFont 级联，会覆盖第 1 层 |
| **第 3 层** | `FluentLabelBase.setFont()` 直接覆盖 | StrongBodyLabel 等内部有 `self.setFont(self.getFont())`，父级 QFont 无法级联 |

**标准实现（放在 `_apply_latest_theme` 中）：**

```python
def _apply_latest_theme(self):
    ctx = self._context_provider()

    # ── 缓存上下文 ──
    font_family, font_size = _ctx_font(ctx)
    tc = _ctx_text_color(ctx)
    self._cached_tc = tc
    self._cached_font_family = font_family
    self._cached_font_size = font_size

    # ── 第 1 层：QFont 级联 ──
    if font_family:
        self.setFont(QFont(font_family, font_size if font_size else 14))
        # ⚠️ QFont(family, 0) 会使字体极小！必须用真实 font_size

    # ── 第 2 + 3 层 ──
    self._retheme()

def _retheme(self):
    """替换所有 QLabel 的 color + font-size + 覆盖 FluentLabelBase"""
    tc = getattr(self, "_cached_tc", "rgba(255,255,255,0.9)")
    ff = getattr(self, "_cached_font_family", "")
    fs = getattr(self, "_cached_font_size", 14)

    for child in self.findChildren(QLabel):
        try:
            # 第 3 层：StrongBodyLabel 等强制覆盖
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
```

**辅助函数模板（放在 cards.py 文件顶部）：**

```python
def _ctx_font(ctx: dict) -> tuple:
    """从上下文提取 font_family 和 font_size"""
    ff = ctx.get("font_family", "Microsoft YaHei")
    fs = ctx.get("font_size", 14)
    return ff, fs


def _make_style(color: str, font_family: str = "", font_size: int = 0, extra: str = "") -> str:
    """生成带字体的 QSS 样式串"""
    parts = [f"color: {color};"]
    if font_family:
        parts.append(f"font-family: '{font_family}';")  # ← 注意分号！
    if font_size:
        parts.append(f"font-size: {font_size}px;")
    if extra:
        parts.append(extra)
    return " ".join(parts)
```

> **陷阱：**
> 1. ❌ `QFont(family, 0)` → 字号极小； ✅ 必须用 `font_size if font_size else 14`
> 2. ❌ 只改 QFont 不改 QSS → QSS 的 `font-size: 11px` 优先； ✅ 必须 `_retheme()` 也替换
> 3. ❌ 漏掉 FluentLabelBase → StrongBodyLabel 内部 `setFont()` 覆盖父级； ✅ 必须第 3 层直接覆盖
> 4. ❌ 动态创建子控件后不刷新 → 新加的 QLabel 还是旧字号； ✅ 创建完调 `_retheme()`
> 5. ❌ 按钮用 `_make_style` 覆盖 → 按钮的 background/border 丢失； ✅ 用专门的 `_xxx_btn_style(accent)`

详细的主题色映射和卡片背景适配见 `widgets-theme.md`。

---

## 2. 比例高度（与系统卡片一致）

**核心原则**：所有浮动卡片**必须**移除固定 `setMinimumHeight(400)`，改用 `sizeHint()` + 窗口 resize 响应。

### 2.1 为什么不能用固定高度

- 用户窗口大小不一，固定高度会导致小窗口内容被截断、大窗口浪费空间
- 系统配置卡片用比例高度（85%），UI 插件必须一致

### 2.2 标准实现

```python
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
```

### 2.3 关键点

- **`sizeHint()` 返回 85% 窗口高度**——这是与系统卡片一致的比例
- **`showEvent` 安装事件过滤器**——首次显示时绑定
- **`eventFilter` 监听 Resize**——窗口变化时通知容器重算
- **不要在 `__init__` 调用 `win.installEventFilter`**——此时 win 可能还是 None

---

## 3. 异步操作

**核心原则**：所有可能阻塞 UI 的操作（DB 查询、HTTP 请求、文件扫描）必须放到后台线程。

### 3.1 标准 Worker 模板

```python
from PyQt5.QtCore import QObject, QThread, pyqtSignal


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
```

### 3.2 完整生命周期管理

```python
class MyCard(QWidget):
    def _async_load_data(self):
        """在后台线程执行数据加载"""
        self._set_loading(True)
        self._cleanup_worker()  # ← 关键：先清理旧 worker

        w = _Worker(self._fetch_data)  # _fetch_data 是同步函数
        t = QThread(self)
        w.moveToThread(t)
        t.started.connect(w.run)
        w.finished.connect(self._on_data_loaded)
        w.error.connect(self._on_load_error)
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        w.finished.connect(w.deleteLater)  # ← 必须
        w.error.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._worker, self._worker_thread = w, t
        t.start()

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
        self._cleanup_worker()  # ← 删除前清理
        super().deleteLater()
```

### 3.3 关键点

- **每次新任务前先 `_cleanup_worker()`**——避免旧 worker 与新 worker 同时跑
- **`deleteLater` 必须连接**——避免内存泄漏
- **`wait(500)` 给 500ms 超时**——避免主线程无限等待
- **`deleteLater` 重写**——卡片销毁时清理 worker

### 3.4 加载状态 UI 同步

```python
def _set_loading(self, loading: bool):
    if hasattr(self, '_refresh_btn'):
        self._refresh_btn.setEnabled(not loading)
    if loading:
        self._status_lb.setText("读取中…")
        self._empty_lb.setVisible(True)
    else:
        self._status_lb.setText("")
        self._empty_lb.setVisible(False)
```

---

## 4. 热重载兼容

**核心原则**：`ui/__init__.py` 中必须清理旧模块缓存，避免热重载时 Python 用旧 `sys.modules` 缓存导致修改不生效。

### 4.1 标准实现

```python
# ui/__init__.py
import sys

def register_ui(registry):
    # 清理旧子模块缓存（避免热重载时 Python 用旧 sys.modules 缓存）
    safe_name = "<plugin-name>".replace("-", "_").replace(":", "_")
    prefix = f"ui_plugin_{safe_name}."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import MyCard
    registry.register_floating_card(...)
```

### 4.2 关于 `__pycache__/`

> **不要主动删除** `__pycache__/` 目录。
> Python 的 import 系统已通过源文件时间戳自动判断是否需要重新编译 `.pyc`。
> 主动删除只会触发不必要的文件系统变更，导致插件热更新监视器误判为跨插件修改。

### 4.3 含 _vendor/ 的特殊情况

如果插件用了 `_vendor/`，还需额外清理 vendored 包的 `sys.modules` 缓存。详见 `templates.md §五.5.1`。

---

## 5. 主题色获取

### 5.1 推荐：上下文注入

```python
def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    """从上下文 colors 中获取文字颜色，无上下文则回退"""
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    if val:
        return val
    return _text_color(secondary)  # fallback
```

### 5.2 不推荐：isDarkTheme()

| 场景 | `isDarkTheme()` | ctx.colors |
|------|----------------|------------|
| 浮动卡片背景暗 | ❌ 检测不到 | ✅ ctx 是主程序给的真实色 |
| 主题切换响应 | ❌ 需要轮询 | ✅ 拉一次就拿到最新 |
| 自定义主题（用户改色） | ❌ 不感知 | ✅ 跟着主程序走 |

> **结论**：浮动卡片场景**必须用 ctx.colors**，不要依赖 `isDarkTheme()`。

完整主题色适配方案见 `widgets-theme.md`。

---

## 6. 信号链（卡片关闭）

**核心原则**：卡片必须发射 `closed` 信号，让 `UIPluginRegistry` 同步 `CardManager` 状态。

### 6.1 标准实现

```python
class MyCard(QWidget):
    closed = pyqtSignal()

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()  # ← 必须，让 CardManager 知道卡片关了
```

### 6.2 为什么必须发射

如果只 `setVisible(False)` 而不 `emit()`：
- CardManager 还以为卡片是开的，下次调用 `show_card()` 会冲突
- 用户关闭卡片后，下次重新打开可能显示异常
- 状态不一致

---

## 7. 弹窗/确认对话框 — 统一 MaskDialogBase 风格

> 完整代码模板见 `templates.md §七`。

### 7.1 为什么不用 QMessageBox

- 原生 `QMessageBox.question()` 样式与 app 主题不统一（白底/系统默认）
- qfluentwidgets 的 `MessageBox` 在插件闭包约束下不易获取主题色
- **推荐方案**：使用 qfluentwidgets 的 `MaskDialogBase`（主程序 `ConfirmDialog` 也是用它）

### 7.2 颜色从哪里来

与浮动卡片一样，弹窗颜色从 **卡片缓存的上下文主题色** 获取：

```python
# 卡片 _apply_latest_theme 中缓存
self._cached_tc = _ctx_text_color(ctx)          # 文字主色
self._cached_font_family, self._cached_font_size = _ctx_font(ctx)
self._cached_theme_colors = ctx.get("colors", {})  # 完整主题色 dict
```

弹窗通过父链向上查找 `PluginManagerCard` / `YourCard` 上的缓存属性：

```python
p = color_source  # 从调用弹窗的 widget 出发
while p is not None:
    cached = getattr(p, "_cached_tc", None)
    if cached is not None:
        tc = cached
        theme_colors = getattr(p, "_cached_theme_colors", {})
        break
    p = p.parent()
```

### 7.3 关键设计

| 设计点 | 说明 |
|--------|------|
| **parent vs color_source 分离** | `parent=self.window()` 给 `MaskDialogBase`（全屏遮罩），`color_source=self` 从调用 widget 向上找卡片缓存 |
| **缓存 `_cached_theme_colors`** | 在 `_apply_latest_theme` 中保存 `ctx.get("colors", {})`，给弹窗提供 `accent` / `content_bg` / `hover_bg` 等键 |
| **按钮风格** | 取消按钮（有边框，hover accent 边框）；确认按钮（accent 填充白字，粗体） |
| **圆角 8px** | 卡片和按钮统一 8px border-radius |
| **按钮高度 36px** | 与 `ConfirmDialog` 一致，注意用 `padding: 4px 28px` 而非垂直 padding（见 §5.2 陷阱） |

### 7.4 调用模式

```python
# 在 _PluginRow._on_uninstall 等地方：
def _on_uninstall(self):
    reply = _plugin_styled_dialog(
        self.window(),           # MaskDialogBase 父窗口（遮罩用）
        "确认卸载",
        f"确定要卸载「{self._name}」吗？",
        color_source=self,       # 颜色查找起点（_PluginRow → … → YourCard）
    )
    if reply:
        self._do_uninstall()
```

完整实现见 `templates.md §七`。

---

## 8. 外部依赖管理（_vendor/ 模式）

> 详细内容见 `references/templates.md §五`，这里只列核心要点。

### 8.1 适用场景

UI 插件从 PyInstaller exe 解包后下载到 `~/.drifox/plugins/` 使用，**不能再次打包**主程序。

- PyInstaller `--onedir` 打包后，`_internal/` 只包含构建期检测到的第三方包
- 用户从市场下载的插件无法重新打包
- 含 C 扩展的包（`.pyd`/`.so`）不能跨版本/架构复制

### 8.2 解决方案

把**纯 Python**依赖放在 `plugins/<plugin>/ui/_vendor/`，在 `register_ui` 开头加入 `sys.path`。

### 8.3 何时用 _vendor/

| 场景 | 推荐做法 |
|------|---------|
| 纯 Python 包（`requests`, `markdown`, `jsonschema`） | ✅ **用 _vendor/** |
| 含 C 扩展但 PyInstaller 已声明（如 `numpy`, `PIL`） | ❌ 直接 `import` 即可 |
| 含 C 扩展但 PyInstaller 未声明 | ⚠️ **不要用 _vendor/**，跨平台/版本会崩溃 |
| 巨型包（`numpy`, `torch`, `pandas`） | ❌ 不适合 _vendor/（体积太大） |

完整模板见 `templates.md §五.5`，含 sys.modules 缓存陷阱说明。

---

## 9. 其他约定

### 8.1 日志

```python
from loguru import logger

logger.info(f"[<plugin>] UI registered")
logger.error(f"[<plugin>] 数据读取失败: {e}\n{traceback.format_exc()}")
```

### 8.2 异常处理

- **fetch_data 中**：try/except 包住，返回 `{"error": str(e)}`
- **paintEvent 中**：每个提前 return 前 `painter.end()`
- **register_ui 中**：vendored 包加载失败要 log，但不影响其他组件注册

### 8.3 命名

- 文件：`snake_case.py`（如 `my_card.py`）
- 类：`PascalCase`（如 `MyCardWidget`）
- 私有方法：`_underscore_prefix`（如 `_async_load_data`）
- 模块前缀：`ui_plugin_<name>.`（用于热重载清理）

---

## 10. 全屏覆盖窗（选区截图 / 全屏遮罩类动作）

> 参考实现：`plugins/quick-screenshot/ui/overlay.py`（2026-09-04 实战沉淀）。
> 适配场景：点击按钮后接管全屏做选区/遮罩交互（截图、屏幕取色、录屏选区等）。

### 10.1 冻结底图方案（核心决策）

进覆盖窗**之前**先 `QScreen.grabWindow(0)` 抓全屏作底图，覆盖窗铺底图：

```python
screen = QApplication.primaryScreen()
base = screen.grabWindow(0)              # 物理像素尺寸，devicePixelRatio 已标记
overlay = _ScreenshotOverlay(base, screen.geometry())
overlay.show()                            # FramelessWindowHint | WindowStaysOnTopHint | Tool
```

- **为什么不用半透明透窗**：透窗有闪烁；松手到抓屏有时间差，截到的与看到的不一致；
  且做不了「选区外变暗、选区内原图」。冻结底图 = 所见即所截（Snipaste/QQ 截图同原理）。
- paintEvent：先 `drawPixmap(self.rect(), base)` 铺底图（物理尺寸拉伸到逻辑窗口，等效缩小
  DPR，画面正确），再四段 `fillRect` 拼选区外暗遮罩 + 选区边框 + 尺寸角标。

### 10.2 窗口属性与生命周期

```python
super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
self.setAttribute(Qt.WA_DeleteOnClose)   # 用完即毁，防置顶窗残留卡死桌面
self.setCursor(Qt.CrossCursor)
self.setGeometry(screen_rect)            # 不用 showFullScreen()，多屏下几何更可控
```

- 信号出口：`captured(QPixmap)` / `cancelled()`，窗口自身不做剪贴板/提示（保持可测）。
- 交互约定：左键拖框、松手即完成；**Esc / 右键 / 误触（选区 < 4×4 逻辑像素）一律取消**。
- **单实例防护**（放 on_click 侧，模块级引用）：

```python
_active_overlay = None  # 模块级

def _close_stale():
    global _active_overlay
    if _active_overlay is not None:
        try:
            _active_overlay.close()
        except RuntimeError:
            pass  # WA_DeleteOnClose 后 C++ 对象已销毁
        _active_overlay = None
```

- `destroyed.connect(_clear_ref)` 清引用；on_click 全流程 try/except，异常路径必须关窗。

### 10.3 高 DPI 坐标换算（主程序三件套全开：EnableHighDpiScaling + UseHighDpiPixmaps + PassThrough）

- 覆盖窗鼠标 `event.pos()` 是**逻辑像素**；`grabWindow` 底图是**物理像素**（DPR 已标记）。
- 裁剪用物理矩形，结果回写 DPR 保证粘贴出去物理尺寸正确：

```python
def _physical_rect(logical: QRect, dpr: float) -> QRect:
    if dpr <= 1.0:
        return QRect(logical)
    return QRect(round(logical.x() * dpr), round(logical.y() * dpr),
                 round(logical.width() * dpr), round(logical.height() * dpr))

dpr = float(base.devicePixelRatio() or 1.0)
shot = base.copy(_physical_rect(sel, dpr))
shot.setDevicePixelRatio(dpr)
```

- QRect 两点构造**含端点**：拖 (10,10)→(110,60) 得 101×51，与截图工具坐标语义一致，别"修正"它。

### 10.4 测试要点（pytest-qt）

- 插件目录非 Python 包，测试用 `importlib.util.spec_from_file_location` 加载
  （先例：`tests/plugins/test_quick_screenshot.py`、`tests/test_rewrite_inline_script.py`）。
- **`qtbot.mouseMove` 在未 show 的 widget 上不派发事件**：拖拽模拟用手动
  `QMouseEvent + QApplication.sendEvent`（press/move/release 三连）。
- 窗口用 `WA_DeleteOnClose` 时**别交给 `qtbot.addWidget`** 收尾（teardown 二次 close 会
  RuntimeError），fixture 里 yield 后 `try: ov.close() except RuntimeError: pass` + `deleteLater()`。
- 信号验证用 `qtbot.waitSignal(overlay.captured)`；剪贴板断言注意剪贴板是全局资源，
  断言尺寸即可，别依赖具体内容。
- lint 关卡是 **ruff**；`npx pyright` 默认配置在本项目是满屏基线噪音
  （无 pyrightconfig、PyQt5 stub 缺枚举），别当硬关卡，也别为它改代码。