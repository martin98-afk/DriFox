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

**修法**：线程清理模式详见 patterns.md §3.2；骨架级写法：

```python
def _cleanup_worker(self):
    if self._worker_thread is not None:
        try:
            self._worker_thread.quit()
            self._worker_thread.wait(500)  # 超时防止死锁
        except RuntimeError:
            pass
        self._worker_thread = None
    self._worker = None

def deleteLater(self):
    self._cleanup_worker()  # 必须清理！否则线程泄漏
    super().deleteLater()
```

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

---

> 新坑写回格式：`## N. 标题` + **症状/原因/修法** 三段 + 可运行代码片段。
