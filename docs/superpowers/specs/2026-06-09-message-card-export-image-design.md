# 消息卡片导出图片：速度/背景/切分修复

> **状态**: 待用户复审
> **日期**: 2026-06-09
> **范围**: `app/widgets/message_card.py` 中 `_export_message` / `_export_as_image` / `_capture_full_content` / `_split_and_stitch` / `_get_card_bg_color`
> **目标**: 修复消息卡片导出 PNG 图片的 4 个体验问题

---

## 1. 背景与目标

### 1.1 用户报告的 4 个问题

| # | 现象 | 触发场景 |
|---|---|---|
| 1 | 速度慢 | 短消息/长消息导出都慢 |
| 2 | 背景全黑 | 截图背景不是当前选择的主题色，是纯黑 |
| 3 | 超长消息切分不全 | 长消息触发切分拼接后，截取的图片漏掉很多内容 |
| 4 | 短消息也被切分 | 短消息（无滚动条）尽量不要切分保存 |

### 1.2 根因诊断

- **问题 1（速度慢）**：`app/widgets/message_card.py:3255-3262` 的 `_capture_full_content` 使用两次 `QEventLoop.exec_()` 各 400ms 强制等待 = **800ms 固定延迟**，且对短消息路径 `if scroll_h <= cur_h` 走 `self.grab()` 之前也有 400ms 等待（实际走的是长消息分支才有，短消息快；但长消息路径的 800ms 不可调）
- **问题 2（背景全黑）**：
  - `_get_card_bg_color` (L3208) 把 `QColor(parent._theme['bg'])` 直接返回
  - 主题 `assistant_card_bg = "rgba(45, 30, 20, 150)"` 是**半透明** rgba 字符串
  - `_capture_full_content` (L3251) `page.setBackgroundColor(card_bg)` 把半透明色设到 WebEngine 页面背景
  - `self.grab()` 截 widget 时，半透明区 + PNG 默认黑色背景 = 截出来是黑色
- **问题 3（超长漏抓）**：
  - `_capture_full_content` (L3275) `self.setFixedHeight(scroll_h + 20)` 后**没有 processEvents 强制布局**
  - 后续 `self.grab()` 截的实际高度可能仍是旧 `self.height()`，小于 `scroll_h + 20`
  - 拿到不完整 `full_pix` 后，`_split_and_stitch` 用 `full.height()` 切分
  - `best_cols` 基于 `target_ratio = 1.5` 计算列数，但对不全的高度切分导致下半段丢失
- **问题 4（短消息切分）**：
  - 实际上 `_export_as_image` 已有 `if full.height() > full.width() * 1.5:` 判断
  - 短消息在 `_capture_full_content` 短消息分支中**不展开**，`full.height()` 等于 viewer 高度
  - 触发切分的条件 `full.height() > full.width() * 1.5` 通常对短消息不成立
  - **但**：如果 `card_bg_solid`/短消息展开后 `full_pix.height()` 比预期大（例如 viewer 内边距+滚动条），可能误触发
  - 需要在修复时验证

### 1.3 修复目标

1. **速度**：长消息导出从 ~800ms 等待降到 ≤ 250ms（自适应）
2. **背景色**：截图背景与当前 MessageCard 主题色一致（强制实心化）
3. **超长切分**：长图抓取时确保抓满 `scroll_h` 全高，水平拼接无遗漏
4. **短消息**：不切分（保持现状，验证不触发切分条件）

### 1.4 决策（已与用户确认）

- ✅ 截图范围：**只截 viewer**（不含 MessageCard 头部/分隔线/按钮）
- ✅ 超长切分：**智能切分拼接为横幅**（保留 `_split_and_stitch` 水平拼接策略）
- ✅ 背景色：**MessageCard 主题色**（`_theme["bg"]` 的 RGB 部分 + `alpha=255` 强制实心）
- ✅ 短消息：不切分

### 1.5 不在范围内

- 截整张 MessageCard（用户已拒绝）
- 切分为多张独立 PNG（用户已拒绝）
- 修改 WebEngine 内部 HTML/CSS
- 修改 viewer 默认主题或滚动条样式
- 添加测试夹具（headless Qt 测试）—— 单元测试只覆盖纯逻辑部分（颜色解析、拼接宽度）

---

## 2. 设计

### 2.1 核心思路

从「在 WebEngine page 上设半透明背景 + `widget.grab()`」改为「在 QPixmap 上**主动填充实心色** + 合成 grab 图像」。

**优势**：
- 背景色完全可控（实心，不依赖 WebEngine 半透明混合）
- 短消息也获得一致背景（不再依赖 viewer 页面背景是否透出）
- grab 失败时仍能输出纯背景图（不会变黑）

### 2.2 改动点

#### 改动 1：`_get_card_bg_color` (L3208-3220) 输出实心色

```python
def _get_card_bg_color(self) -> "QColor":
    """沿父链查找 MessageCard，获取卡片背景色（强制实心化）"""
    from PyQt5.QtGui import QColor
    parent = self.parent()
    while parent:
        if hasattr(parent, '_theme') and isinstance(parent._theme, dict) and 'bg' in parent._theme:
            bg_str = parent._theme['bg']
            color = QColor(bg_str)
            if color.isValid():
                color.setAlpha(255)  # ★ 强制实心
                return color
            # 主题色无效（少见），用兜底
            break
        parent = parent.parent()
    return QColor("#2B2B2B")
```

**变更**：
- 在解析后**强制 `setAlpha(255)`**，消除半透明
- 增加 `isValid()` 校验，无效时落兜底

#### 改动 2：`_capture_full_content` (L3234-3310) 改用 QPixmap 主动填充

```python
def _capture_full_content(self) -> "QPixmap":
    """截取消息的完整内容为一张大图（实心背景 + 内容合成）"""
    from PyQt5.QtCore import QEventLoop, QTimer, QRect, QPoint
    from PyQt5.QtGui import QPixmap, QPainter
    from PyQt5.QtWidgets import QApplication
    import json as json_mod

    page = self.page()
    view_w = self.width()
    cur_h = self.height()

    # 1. 获取完整内容高度
    dims_raw = self._run_js_sync(
        "JSON.stringify({sh: document.body.scrollHeight})"
    )
    if not dims_raw:
        # 拿不到高度 → 直接 grab + 强制实心背景
        return self._compose_with_solid_bg(self.grab(), view_w, cur_h)

    try:
        scroll_h = json_mod.loads(dims_raw).get('sh', 0)
    except Exception:
        scroll_h = 0

    # 2. 短消息：内容不超出 → 不展开，直接合成
    if scroll_h <= cur_h or scroll_h <= 0:
        grabbed = self.grab()
        return self._compose_with_solid_bg(grabbed, view_w, max(cur_h, grabbed.height()))

    # 3. 长消息：临时展开
    old_styles = self._run_js_sync("""
        var s = document.body.style;
        JSON.stringify({maxHeight: s.maxHeight, overflowY: s.overflowY})
    """)
    self._run_js_sync("""
        document.body.style.maxHeight = 'none';
        document.body.style.overflowY = 'hidden';
    """)

    orig_height = self.height()
    target_h = scroll_h + 20
    self.setFixedHeight(target_h)
    self.update()
    QApplication.processEvents()  # ★ 强制布局生效

    self._run_js_sync("window.scrollTo(0, 0);")

    # 自适应等待：单次 200ms（替代 400ms×2）
    stable_loop = QEventLoop()
    QTimer.singleShot(200, stable_loop.quit)
    stable_loop.exec_()

    # 4. 显式 grab 整个目标区域
    full_pix = self.grab(QRect(QPoint(0, 0), self.size()))

    # 5. 合成：实心背景 + grab 内容
    final_w = full_pix.width() if not full_pix.isNull() else view_w
    final_h = max(target_h, full_pix.height() if not full_pix.isNull() else 0)
    result = self._compose_with_solid_bg(full_pix, final_w, final_h)

    # 6. 恢复
    self.setFixedHeight(orig_height)
    if old_styles:
        try:
            prev = json_mod.loads(old_styles)
            js_restore = f"""
                document.body.style.maxHeight = {json_mod.dumps(prev.get('maxHeight', ''))};
                document.body.style.overflowY = {json_mod.dumps(prev.get('overflowY', 'auto'))};
                window.scrollTo(0, 0);
            """
            self._run_js_sync(js_restore)
        except Exception:
            self._run_js_sync("window.scrollTo(0, 0);")

    if result.isNull() or result.width() <= 0 or result.height() <= 0:
        return self.grab()
    return result


def _compose_with_solid_bg(self, source: "QPixmap", width: int, height: int) -> "QPixmap":
    """在 QPixmap 上填充实心卡片背景，再合成 source"""
    from PyQt5.QtGui import QPixmap, QPainter
    if width <= 0 or height <= 0:
        return source
    result = QPixmap(width, height)
    result.fill(self._get_card_bg_color())
    if not source.isNull():
        painter = QPainter(result)
        painter.drawPixmap(0, 0, source)
        painter.end()
    return result
```

**关键变更**：
- **删除** `page.setBackgroundColor(orig_bg)` 切换逻辑（不再污染 WebEngine 页面）
- **新增** `_compose_with_solid_bg` 辅助方法统一处理背景合成
- **替换** 双重 400ms `QEventLoop` 等待 → 单次 200ms + `processEvents()`
- **新增** `QRect(QPoint(0, 0), self.size())` 显式 grab 区域
- **删除** `try/finally` 中恢复 page backgroundColor 的代码（不再需要）

#### 改动 3：`_split_and_stitch` (L3311-3367) 保持不变

`best_cols` 基于 `full.height()` 切分，由于改动 2 修复了 grab 高度问题，此处无需改动。

#### 改动 4：`_export_as_image` (L3368-3383) 保持不变

切分判断 `full.height() > full.width() * 1.5` 保留。

### 2.3 数据流

```
_export_message
  └─ _export_as_image(file_path)
       ├─ _capture_full_content()
       │    ├─ _run_js_sync(scrollHeight)
       │    ├─ 短消息路径：grab() + _compose_with_solid_bg
       │    └─ 长消息路径：
       │         ├─ 解除 body 高度限制
       │         ├─ setFixedHeight(scroll_h+20)
       │         ├─ processEvents() + 200ms 等待
       │         ├─ grab(QRect) + _compose_with_solid_bg
       │         └─ 恢复
       ├─ if full.height > full.width * 1.5:  # 长消息触发
       │    └─ _split_and_stitch(full)
       └─ result.save(file_path, "PNG")
```

### 2.4 错误处理

| 场景 | 行为 |
|---|---|
| `dims_raw` 解析失败 | `_compose_with_solid_bg(self.grab(), view_w, cur_h)` |
| 短消息 grab 失败 | 仍返回纯背景 QPixmap（不会变黑） |
| 长消息 setFixedHeight 失败 | 后续 grab 会拿到旧高度，合成时用 `max(target_h, grab_h)` 保证完整 |
| 旧样式 JSON 解析失败 | 只恢复 `window.scrollTo(0,0)` |
| `result.isNull()` | fallback `self.grab()` |
| `_get_card_bg_color` 父链无 `_theme` | 兜底 `QColor("#2B2B2B")` |
| QColor 解析 rgba 字符串失败 | 兜底色 |

### 2.5 测试

#### 单元测试（纯逻辑）

`tests/widgets/test_export_image_helpers.py`：

1. **`_split_and_stitch` 拼接宽度**：
   - 输入：3 段等宽 pixmap
   - 断言：拼接后宽度 == 3×单段宽度

2. **`_split_and_stitch` 列数选择**：
   - 输入：高宽比 9:1 的超长图
   - 断言：`best_cols >= 2`

3. **`_split_and_stitch` 不切分**：
   - 输入：高宽比 1.0:1 的近方形图
   - 断言：返回原 pixmap

4. **`_get_card_bg_color` 强制实心**：
   - 用 mock 父链返回 `_theme = {"bg": "rgba(45, 30, 20, 150)"}`
   - 断言：返回的 QColor `alpha() == 255`

5. **`_get_card_bg_color` 兜底**：
   - 用 mock 父链无 `_theme`
   - 断言：返回 `QColor("#2B2B2B")`

#### 手动验证（GUI 不可自动化）

需要在真实 DriFox 窗口内手动验证：
- 短消息导出：背景 = 卡片色 + 内容完整
- 长消息导出：抓取完整高度 + 拼接不漏
- 切换主题后导出：背景色跟随

---

## 3. 实施步骤

1. 修改 `app/widgets/message_card.py`：
   - L3208-3220 `_get_card_bg_color` 加 `setAlpha(255)` 和 `isValid` 校验
   - L3234-3310 `_capture_full_content` 重写为 QPixmap 主动填充
   - 新增 `_compose_with_solid_bg` 辅助方法
2. 新建 `tests/widgets/test_export_image_helpers.py`，编写 5 个单元测试
3. 运行 `pytest tests/widgets/test_export_image_helpers.py -v` 确认通过
4. 手动验证（用户提供测试用例）

---

## 4. 风险与权衡

| 风险 | 缓解 |
|---|---|
| `QPainter.drawPixmap` 与 `QWidget.grab` 结果合成边缘对不齐 | 用 `QRect(0, 0, view_w, target_h)` 显式 grab；合成时 `drawPixmap(0, 0, source)` |
| `setFixedHeight` 后 WebEngine 渲染未完成 | 单次 200ms + processEvents 已足够；如失败，合成时 `max(target_h, grab_h)` 兜底 |
| 用户在导出过程中滚动 viewer 造成卡顿 | `_export_message` 由菜单触发，主线程同步执行；现有实现也是同步的，不引入新卡顿 |
| 短消息路径也走 QPixmap 合成（多一次 fill） | QPixmap.fill 是廉价操作，~1ms |
| 单元测试不覆盖 GUI 路径 | 手动验证清单；用户为 DriFox 维护者，可现场确认 |

---

## 5. 不修改的部分

- 主题 YAML（`plugins/system/themes/*/colors` 字段）
- `_build_theme`（MessageCard 初始化）
- `paintEvent`（流光动画等）
- `_convert_md_to_html`（HTML 导出分支）
- Markdown/HTML 导出分支
- 触发入口（右键菜单、按钮绑定）
