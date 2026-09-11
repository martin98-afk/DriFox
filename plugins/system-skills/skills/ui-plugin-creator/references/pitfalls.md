---
description: UI 插件编码级踩坑记录（症状→原因→修法），来自实战回归
---

# UI 插件踩坑记录（pitfalls）

> 生成代码后的 30 秒自检先行；本文件只收「编码级」踩坑。
> 架构级陷阱速查见 widgets.md §6；新坑按同格式写回本文件。

---

## 生成后 30 秒自检

```
拿到新卡片 → 开程序 → 输 /<card_id>
1. 字体和主界面一致吗？        → 不一致 → 检查 _apply_latest_theme 的字体处理（见下「字体注入」）
2. 按钮文字显示完整吗？         → 不完整 → 检查 button padding 和 fixedSize
3. 主题色跟随主程序变吗？      → 不跟  → 检查 set_context_provider + _apply_latest_theme
4. 刷新/操作不卡 UI 吗？       → 卡UI  → 检查是否用了 QThread（模式见 patterns.md §3.2）
5. 连续快速点击会崩吗？        → 崩溃  → 检查 _is_busy 防重入 + worker cleanup
6. 写剪贴板的图粘贴有反应吗？   → 无反应 → 检查是否用了 setImage（见下）
7. 提示条看得见吗？            → 看不见 → 检查是否用了 InfoBar 而非 QToolTip（见下）
```

---

## 1. Python 3.14 异常语法

**症状**：卡片加载即 SyntaxError，插件整包失效。

**原因**：Python 3.14 不再支持 Python 2 风格 `except Exception, e:`，多异常必须元组包裹。

**修法**：
```python
# ❌ 不可用（Python 2 语法，3.14 报 SyntaxError）
except OSError, PermissionError:
    pass

# ✅ 正确
except (OSError, PermissionError):
    pass

# ✅ 正确（带异常变量）
except (OSError, PermissionError) as e:
    logger.error(f"操作失败: {e}")
```

## 2. 按钮高度 vs padding 陷阱

**症状**：按钮看起来存在，但文字不显示。

**原因**：`setFixedHeight` 小于 padding 上下之和时内容区被压成 0（32 - 16 - 16 = 0）。

**修法**：
```python
# ❌ 按钮文字不显示
btn.setFixedHeight(32)
btn.setStyleSheet("padding: 16px 0;")

# ✅ 正确
btn.setStyleSheet("padding: 0 10px;")   # 左右 padding，不占用高度
```

**规则**：`fixedHeight` < 40px 的按钮用 `padding: 0 Xpx`；大按钮用 `padding: Ypx 0`。

## 3. QTimer.singleShot 按钮自动恢复模式

**症状**：操作完成后临时成功文案不恢复，或恢复时误覆盖新操作文案。

**原因**：恢复回调无脑 set 默认文案，与防重入状态脱节。

**修法**：
```python
self._btn.setText("✅ 清理完成，释放 234 MB")
QTimer.singleShot(3000, self._reset_btn)  # 3 秒后恢复

def _reset_btn(self):
    if not self._is_busy:  # 防止恢复时正在执行新操作
        self._btn.setText("默认文案")
```

## 4. 字体注入容易漏

**症状**：卡片功能正常，但字体与主程序不一致（最常犯 bug）。

**原因**：`_apply_latest_theme` 里只更新了颜色，漏了字体。

**修法**：颜色与字体必须同时注入，模板见 patterns.md §1.4。

```python
# ❌ 只更新颜色
child.setStyleSheet("color: rgba(255,255,255,0.9);")

# ✅ 用 _make_style 同时注入颜色 + 字体
child.setStyleSheet(_make_style(tc, font_family, font_size))
```

## 5. 异步 worker 生命周期

**症状**：关闭卡片后程序崩溃或线程泄漏。

**原因**：销毁时未停 worker 线程；C++ 对象销毁后残留信号触发 RuntimeError。

**修法**：完整线程清理模式见 patterns.md §3.2（quit→wait(500)→置 None，重写 deleteLater）。

## 6. 剪贴板写图必须用 setImage，禁用 setPixmap

**症状**：提示已复制，同进程粘贴却无反应；外部工具粘贴正常。

**原因**：`setPixmap()` 后剪贴板所有权在本进程，同进程粘贴时 `mimeData().imageData()` 返回 QPixmap，主程序输入框的 `isinstance(img, QImage)` 检查静默跳过。跨进程读 CF_DIB 自动转 QImage，故外部工具不受影响。

**修法**：
```python
QApplication.clipboard().setImage(pixmap.toImage())   # ✅
QApplication.clipboard().setPixmap(pixmap)            # ❌ DriFox 内粘贴无反应
```

详见 templates-entries.md §9.3（主程序 2026-09 已双类型兼容，插件侧仍以 QImage 为准）。

## 7. 用户提示统一 InfoBar，QToolTip 不可靠

**症状**：`QToolTip.showText` 调用了但提示不显示。

**原因**：主程序源码明确注释「绕开 QToolTip 样式问题」，DriFox 内 QToolTip 不可靠。

**修法**：提示统一走 `qfluentwidgets.InfoBar`：

```python
from qfluentwidgets import InfoBar, InfoBarPosition
InfoBar.success("标题", "内容", parent=main_widget,
                position=InfoBarPosition.BOTTOM, duration=2500)
```

模板与兜底写法见 templates-entries.md §9.4。

## 8. SQLite 并发读与路径兜底

**症状**：卡片读库报 database is locked，或打包后读不到库文件。

**原因**：直接连主会话库且无超时；硬编码开发机路径。

**修法**：路径兜底 + N 天窗口查询模板见 widgets-sqlite.md §一/§二。

## 9. QPlainTextEdit 高度自适应：document().size() 是行数不是像素

**症状**：多行输入框写了 textChanged → setFixedHeight 自适应，但输入多行后框高永远不变（或高得离谱）。

**原因**：QPlainTextEdit 下 `document().size().height()` 和 `documentLayout().documentSize().height()` 返回的都是**行数**，不是像素。10 行文本 doc_h=10，按像素用永远算出小高度。

**修法**：行数 × lineSpacing + 垂直 chrome（QSS padding×2 + border×2 + documentMargin×2 + 余量）；无换行的长段落按 viewport 宽度估算软折行：

```python
fm = edit.fontMetrics()
avail = max(40, edit.viewport().width() - 16)
lines = 0
blk = edit.document().firstBlock()
while blk.isValid():
    w = fm.horizontalAdvance(blk.text())
    lines += max(1, int(w / avail) + (1 if w % avail else 0))
    blk = blk.next()
target = max(36, min(max(1, lines) * fm.lineSpacing() + 24, 160))
```

验证：用 D:\work\DriFox\.venv 的 python（带完整 PyQt5）写 QTimer 序列脚本离线实测，见 plugin-creator troubleshooting.md「热重载」条。

## 10. ExpandSettingCard 覆写 _adjustViewSize 后展开收不回

**症状**：qfluentwidgets ExpandSettingCard 为消除展开大空白覆写 `_adjustViewSize` 把 `spaceWidget.setFixedHeight(0)`，结果展开后点折叠没反应。

**原因**：原版折叠动画靠 spaceWidget 占位提供滚动余量（scrollbar value 从 0 动画到 maximum）。占位归零后 maximum=0，动画失效，`setFixedHeight` 停在展开高度。

**修法**：连 `setExpand` 一起覆写，绕开滚动动画直接切高度：

```python
def setExpand(self, isExpand: bool):
    if self.isExpand == isExpand:
        return
    self.isExpand = isExpand
    self.setProperty("isExpand", isExpand)
    self.setStyle(QApplication.style())
    self.card.expandButton.setExpand(isExpand)
    self._adjustViewSize()

def _adjustViewSize(self):
    h = self.viewLayout.sizeHint().height()
    self.spaceWidget.setFixedHeight(0)
    self.setFixedHeight(self.card.height() + h if self.isExpand else self.card.height())
```

## 11. retheme 重涂陷阱：选择器类型不匹配静默失效

**症状**：改了控件类型（如 QLineEdit → QPlainTextEdit）后主题色/字体从不生效，报错也没有。

**原因**：`_apply_latest_theme/_retheme` 的 QSS 写死了旧控件类型选择器（`QLineEdit { ... }`），选择器不匹配时整段样式静默忽略。

**修法**：改控件类型时同步改 retheme 里的 QSS 选择器。另注意 retheme 循环规则：

- QLabel 带 `keepColor` 属性 → 整个跳过（语义色/字号自管，适合分支名、图例）
- QPushButton 只有带 styleSheet 才被更新（字号替换为 fs-3，下限 11）
- 带 QSS 的控件写死 `font-size` 会覆盖继承的 widget 字体，需跟随系统字体的控件不要写死字号，显式传 `font-family`/`font-size`

## 12. 需要原生 tooltip 时用事件过滤器

**症状**：`setToolTip("...")` 悬停弹的是主程序自绘气泡，想要系统原生样式做不到。

**原因**：主程序全局 patch 了 `QWidget.setToolTip`，任何 setToolTip 都会被装上自绘 hover filter（QAbstractScrollArea/QLineEdit/QTextEdit 有豁免，但依赖主程序版本）。

**修法**：插件侧装事件过滤器抢占 ToolTip 事件，直接 `QToolTip.showText`（与 #7 不冲突：给用户的操作提示用 InfoBar，控件的悬停说明用本模式）：

```python
class _NativeTooltipFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.ToolTip:
            tip = obj.toolTip()
            if tip:
                QToolTip.showText(event.globalPos(), tip, obj)
            return True
        return False

widget.setToolTip("说明文字")
widget.installEventFilter(_NativeTooltipFilter(widget))
```

## 13. QPlainTextEdit 没有 setText：回调链静默中断

**症状**：提交成功后输入框没清空，且同函数里的后续逻辑（如自动刷新）也没执行，无任何报错。

**原因**：QPlainTextEdit 清空用 `setPlainText("")`；`setText` 只属于 QLineEdit/QLabel。信号回调里抛出的 AttributeError 被吞时，同函数后续语句全部不执行。

**修法**：

```python
edit.setPlainText("")   # ✅ QPlainTextEdit
edit.setText("")        # ❌ QLineEdit/QLabel 的 API，这里 AttributeError
```

**规则**：从单行输入框迁移到多行时全文搜 `.setText(`；信号回调链里的异常要么显式记日志要么别吞。

---

## 14. 工作台插件页拿到的 context 是构造时快照

**症状**：切换项目后插件页仍显示上一个项目的数据（文件树显示旧目录、工作树提示未设置工作目录）；页面显示的路径与 `ctx["project_root"]` 对得上，但就是不变。

**原因**：`WorkbenchPanel._make_page_widget` 只在页面**构造时**推一次 `_build_ui_context()` 的结果。页面把它存成 provider 闭包后一直返回同一份 dict，切项目不会自动更新。

**修法**：

```python
    def __init__(self, parent=None, context=None):
        ...
        # 宿主注入的拉取入口（没有它就拿不到最新 ctx）
        self._host_context_provider = (context or {}).get("context_provider")

    def refresh_data(self) -> None:
        provider = self._host_context_provider
        if callable(provider):
            ctx = provider()          # ✅ 每次拿最新
            self._context = ctx
```

**规则**：插件页的 `context` 只当**初值**用；需要跟随项目 / 工作目录变化时，实现可选协议
`on_project_changed`（宿主切项目时派发），内部走 `refresh_data()` 重取 ctx 并重载数据。
详见 `references/patterns.md §11`。

---

> 新坑写回格式：`## N. 标题` + **症状/原因/修法** 三段 + 可运行代码片段。
