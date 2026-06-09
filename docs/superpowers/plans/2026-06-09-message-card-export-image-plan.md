# 消息卡片导出图片修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `CodeWebViewer._export_as_image` 4 个问题：速度慢、背景全黑、超长消息切分不全、短消息被切分

**Architecture:** 把「在 WebEngine page 设半透明背景 + `widget.grab()`」改为「在 QPixmap 上主动填充实心色 + 合成 grab 图像」；颜色取自 MessageCard 父链的 `_theme["bg"]` 并强制实心化；长消息等待从 800ms 降到 200ms + processEvents

**Tech Stack:** PyQt5, QPixmap, QPainter, QWebEngineView, pytest

---

## File Structure

| 文件 | 责任 |
|---|---|
| `app/widgets/message_card.py` | 修改：`CodeWebViewer._get_card_bg_color`, `_capture_full_content`；新增：`_compose_with_solid_bg` |
| `tests/widgets/test_export_image_helpers.py` | 新建：5 个纯逻辑单元测试 |

---

## Task 1: 新建测试文件骨架

**Files:**
- Create: `tests/widgets/__init__.py`（空文件，让 pytest 找到 test 模块）
- Create: `tests/widgets/test_export_image_helpers.py`

- [ ] **Step 1: 创建 `tests/widgets/__init__.py`（空文件）**

```bash
type nul > tests\widgets\__init__.py
```

或 Python：
```python
# tests/widgets/__init__.py
```

- [ ] **Step 2: 创建测试文件，骨架含 PyQt 启动 + 必要 import**

`tests/widgets/test_export_image_helpers.py`：
```python
# -*- coding: utf-8 -*-
"""
消息卡片导出图片相关函数的单元测试

覆盖：
- _get_card_bg_color: rgba 字符串解析、强制实心、找不到父链兜底
- _split_and_stitch: 拼接宽度、列数选择、短图不切分
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pytest


# ─────────────────────────────────────────────
#  PyQt 应用 fixture（测试只创建不弹窗的 widget，需要 QApplication）
# ─────────────────────────────────────────────
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app
    # 不退出 app，会话复用
```

- [ ] **Step 3: 验证测试文件可被发现**

Run: `python -m pytest tests/widgets/test_export_image_helpers.py --collect-only -q`
Expected: 输出 "no tests ran" 或类似（只有 fixture），无 import 错误

- [ ] **Step 4: Commit**

```bash
git add tests/widgets/__init__.py tests/widgets/test_export_image_helpers.py
git commit -m "test: add test scaffolding for message card export image"
```

---

## Task 2: 测试 `_split_and_stitch` 拼接宽度（红→绿）

**Files:**
- Modify: `tests/widgets/test_export_image_helpers.py`
- Modify: `app/widgets/message_card.py`（最终实现）

- [ ] **Step 1: 添加失败的测试（_split_and_stitch 拼接宽度）**

在 `tests/widgets/test_export_image_helpers.py` 末尾添加：

```python
# ─────────────────────────────────────────────
#  _split_and_stitch 测试
# ─────────────────────────────────────────────
class TestSplitAndStitch:
    """_split_and_stitch: 横向拼接多段为横幅"""

    def _make_viewer_stub(self):
        """构造一个避开 QWebEngineView 实例化的 _split_and_stitch 测试桩"""
        from app.widgets.message_card import CodeWebViewer
        # 用 type() 创建不调 __init__ 的实例，绕开 QWebEngineView
        # 但 _split_and_stitch 是普通方法，不依赖 self 任何属性
        # 直接用 unbound 调用即可
        return CodeWebViewer._split_and_stitch

    def test_horizontal_strip_width_equals_sum(self, qapp):
        """3 段等宽 pixmap 拼接后宽度 = 3×单段宽度"""
        from PyQt5.QtGui import QPixmap, QColor
        from app.widgets.message_card import CodeWebViewer

        # 构造 3 段 100x400 的红色 pixmap，模拟超长消息
        # 比例 100:1200 = 1:12，超过 1.5 触发切分
        seg = QPixmap(100, 400)
        seg.fill(QColor(255, 0, 0))

        full = QPixmap(100, 1200)
        full.fill(QColor(255, 0, 0))
        # 在 full 上画 3 个 seg（让 _split_and_stitch 拿到非空像素）
        from PyQt5.QtGui import QPainter
        p = QPainter(full)
        for i in range(3):
            p.drawPixmap(0, i * 400, seg)
        p.end()

        result = CodeWebViewer._split_and_stitch(None, full, max_cols=6)

        # 切分为 3 段后横向拼接，单段宽=100，3 段总宽=300
        assert result.width() == 300, f"expected 300, got {result.width()}"
        # 高度应接近单段高度
        assert result.height() >= 380, f"expected >=380, got {result.height()}"

    def test_near_square_image_not_split(self, qapp):
        """高宽比 1:1 的近方图不切分，返回原图"""
        from PyQt5.QtGui import QPixmap, QColor
        from app.widgets.message_card import CodeWebViewer

        full = QPixmap(400, 400)
        full.fill(QColor(0, 255, 0))

        result = CodeWebViewer._split_and_stitch(None, full, max_cols=6)

        # 不切分时返回原图
        assert result.width() == 400
        assert result.height() == 400

    def test_short_image_not_split(self, qapp):
        """高宽比 1.2:1 的短图不切分（小于 1.5 阈值）"""
        from PyQt5.QtGui import QPixmap, QColor
        from app.widgets.message_card import CodeWebViewer

        # 400x480, 高宽比 1.2 < 1.5, 不切分
        full = QPixmap(400, 480)
        full.fill(QColor(0, 0, 255))

        result = CodeWebViewer._split_and_stitch(None, full, max_cols=6)

        assert result.width() == 400
        assert result.height() == 480
```

- [ ] **Step 2: 跑测试确认通过（_split_and_stitch 已有实现）**

Run: `python -m pytest tests/widgets/test_export_image_helpers.py::TestSplitAndStitch -v`
Expected: 3 个测试全 PASS（`_split_and_stitch` 现有实现已正确）

- [ ] **Step 3: Commit**

```bash
git add tests/widgets/test_export_image_helpers.py
git commit -m "test: cover _split_and_stitch horizontal join logic"
```

---

## Task 3: 测试 `_get_card_bg_color` 强制实心（红→绿）

**Files:**
- Modify: `tests/widgets/test_export_image_helpers.py`
- Modify: `app/widgets/message_card.py`（最终实现）

- [ ] **Step 1: 添加失败的测试（_get_card_bg_color 强制 alpha=255）**

在 `tests/widgets/test_export_image_helpers.py` 末尾追加：

```python
# ─────────────────────────────────────────────
#  _get_card_bg_color 测试
# ─────────────────────────────────────────────
class TestGetCardBgColor:
    """_get_card_bg_color: 沿父链取主题色并强制实心"""

    def _make_self_with_parent_chain(self, theme_dict_or_none):
        """构造一个能让 _get_card_bg_color 找到指定 _theme 的桩"""
        # 构造父链：MessageCard 桩
        mock_card = MagicMock()
        if theme_dict_or_none is None:
            # 无 _theme 属性（删掉默认的 _theme）
            del mock_card._theme
        else:
            mock_card._theme = theme_dict_or_none
        return mock_card

    def test_rgba_string_returns_solid_color(self, qapp):
        """rgba(45, 30, 20, 150) 应解析为 RGB=45,30,20 且 alpha=255"""
        from app.widgets.message_card import CodeWebViewer

        # 构造父链
        mock_card = self._make_self_with_parent_chain(
            {"bg": "rgba(45, 30, 20, 150)"}
        )
        # 构造 viewer 实例（不调 __init__）
        viewer = CodeWebViewer.__new__(CodeWebViewer)
        # 把 mock 当作 viewer.parent()
        viewer_parent = MagicMock()
        viewer_parent.parent = MagicMock(return_value=mock_card)
        # CodeWebViewer.parent() 应该是 viewer 自己的 parent 属性
        # 用 type 绕过
        viewer.__class__ = type("Stub", (CodeWebViewer,), {
            "parent": lambda self: mock_card,
        })
        # 直接 unbound 调用，self 是 viewer
        result = CodeWebViewer._get_card_bg_color(viewer)

        # 强制实心：alpha == 255
        assert result.alpha() == 255, f"expected alpha=255, got {result.alpha()}"
        # RGB 应保持
        assert result.red() == 45
        assert result.green() == 30
        assert result.blue() == 20

    def test_fallback_color_when_no_parent_theme(self, qapp):
        """父链无 _theme 时返回兜底色 #2B2B2B"""
        from app.widgets.message_card import CodeWebViewer
        from PyQt5.QtGui import QColor

        # 父链全是 MagicMock，没有 _theme 属性（hasattr 返回 False）
        mock_parent = MagicMock(spec=[])  # spec=[] 让 hasattr 返回 False
        viewer = CodeWebViewer.__new__(CodeWebViewer)
        viewer.__class__ = type("Stub", (CodeWebViewer,), {
            "parent": lambda self: mock_parent,
        })

        result = CodeWebViewer._get_card_bg_color(viewer)

        # 兜底色 #2B2B2B = (43, 43, 43)
        assert result.red() == 43
        assert result.green() == 43
        assert result.blue() == 43
        assert result.alpha() == 255
```

- [ ] **Step 2: 跑测试，`test_rgba_string_returns_solid_color` 应该 FAIL（当前实现没强制 setAlpha）**

Run: `python -m pytest tests/widgets/test_export_image_helpers.py::TestGetCardBgColor -v`
Expected: `test_rgba_string_returns_solid_color` FAIL（alpha=150 而非 255），`test_fallback_color_when_no_parent_theme` PASS

- [ ] **Step 3: 修复 `_get_card_bg_color` 强制实心**

修改 `app/widgets/message_card.py` L3208-3220：

```python
def _get_card_bg_color(self) -> "QColor":
    """沿父链查找 MessageCard，获取卡片背景色（强制实心化）"""
    from PyQt5.QtGui import QColor
    parent = self.parent()
    while parent:
        if hasattr(parent, '_theme') and isinstance(parent._theme, dict) and 'bg' in parent._theme:
            color = QColor(parent._theme['bg'])
            if color.isValid():
                # ★ 强制实心：保留 RGB，alpha 设为 255
                # （避免半透明 rgba 字符串导致截图后呈现为黑色）
                color.setAlpha(255)
                return color
            # 主题色字符串无效时跳出，用兜底
            break
        parent = parent.parent()
    # 兜底：暗色主题背景
    return QColor("#2B2B2B")
```

- [ ] **Step 4: 重跑测试，全部 PASS**

Run: `python -m pytest tests/widgets/test_export_image_helpers.py -v`
Expected: 5 tests PASS（3 个 _split_and_stitch + 2 个 _get_card_bg_color）

- [ ] **Step 5: Commit**

```bash
git add tests/widgets/test_export_image_helpers.py app/widgets/message_card.py
git commit -m "fix(message-card): force solid alpha in _get_card_bg_color"
```

---

## Task 4: 重写 `_capture_full_content` 主动填充背景

**Files:**
- Modify: `app/widgets/message_card.py`

- [ ] **Step 1: 新增 `_compose_with_solid_bg` 辅助方法**

在 `app/widgets/message_card.py` 中，紧邻 `_capture_full_content` 之前插入：

```python
def _compose_with_solid_bg(self, source: "QPixmap", width: int, height: int) -> "QPixmap":
    """在 QPixmap 上填充实心卡片背景，再合成 source

    Args:
        source:  从 widget.grab() 拿到的 pixmap（可能含透明区）
        width:   目标宽度
        height:  目标高度

    Returns:
        填充实心卡片背景 + 绘制 source 的合成 pixmap
    """
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

- [ ] **Step 2: 重写 `_capture_full_content`（L3234-3310）**

将原方法整体替换为：

```python
def _capture_full_content(self) -> "QPixmap":
    """截取消息的完整内容为一张大图（实心背景 + 内容合成）

    策略：临时解除 body max-height 限制并撑大视图到完整内容高度，
    让全部内容一次性渲染可见，然后通过一次 grab() 截取整张图片，
    最后用 QPixmap 主动填充实心卡片背景 + 合成 grab 结果。

    相比原实现：
    - 主动 QPixmap.fill 卡片色（实心），避免半透明 rgba 在 PNG 中呈现为黑
    - 单次 200ms 等待 + processEvents() 强制布局（替代 400ms×2）
    - grab(QRect) 显式指定区域，避免 setFixedHeight 后未生效导致漏抓
    """
    from PyQt5.QtCore import QEventLoop, QTimer, QRect, QPoint
    from PyQt5.QtGui import QPixmap
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
        # 拿不到高度 → 兜底：直接 grab + 强制实心背景
        return self._compose_with_solid_bg(self.grab(), view_w, cur_h)

    try:
        scroll_h = json_mod.loads(dims_raw).get('sh', 0)
    except Exception:
        scroll_h = 0

    # 2. 短消息：内容不超出 → 不展开
    if scroll_h <= cur_h or scroll_h <= 0:
        grabbed = self.grab()
        return self._compose_with_solid_bg(
            grabbed, view_w, max(cur_h, grabbed.height() if not grabbed.isNull() else cur_h)
        )

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
    # ★ 强制布局：让 setFixedHeight 真的撑大 widget
    QApplication.processEvents()

    self._run_js_sync("window.scrollTo(0, 0);")

    # ★ 单次 200ms 等待（替代 400ms×2）
    stable_loop = QEventLoop()
    QTimer.singleShot(200, stable_loop.quit)
    stable_loop.exec_()

    # 4. 显式 grab 整个目标区域
    full_pix = self.grab(QRect(QPoint(0, 0), self.size()))

    # 5. 合成：实心背景 + grab 内容
    final_w = full_pix.width() if not full_pix.isNull() else view_w
    final_h = max(target_h, full_pix.height() if not full_pix.isNull() else 0)
    result = self._compose_with_solid_bg(full_pix, final_w, final_h)

    # 6. 恢复视图和样式
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
```

**变更要点**：
- 删除 `try/finally` 中 `page.setBackgroundColor(orig_bg)` 切换（不再污染 WebEngine 页面）
- 删除 `card_bg = self._get_card_bg_color()` + `page.setBackgroundColor(card_bg)`（不再依赖 WebEngine 半透明混合）
- 单次 200ms + `QApplication.processEvents()` 替代 400ms×2
- `self.grab(QRect(QPoint(0, 0), self.size()))` 显式 grab 区域
- `_compose_with_solid_bg` 统一处理背景合成（短/长消息都走）

- [ ] **Step 3: 跑全部测试确认不破坏既有**

Run: `python -m pytest tests/widgets/test_export_image_helpers.py -v`
Expected: 5 tests PASS（_split_and_stitch + _get_card_bg_color）

- [ ] **Step 4: 静态语法检查**

Run: `python -c "import app.widgets.message_card; print('ok')"`
Expected: 输出 `ok`（无 ImportError 或 SyntaxError）

- [ ] **Step 5: Commit**

```bash
git add app/widgets/message_card.py
git commit -m "fix(message-card): rewrite _capture_full_content to use QPixmap solid background"
```

---

## Task 5: 验证 `_export_as_image` 触发切分条件

**Files:**
- Verify-only（不改代码）

- [ ] **Step 1: 走查 `_export_as_image` 切分判断**

阅读 `app/widgets/message_card.py` L3368-3383，验证：

```python
def _export_as_image(self, file_path: str):
    full = self._capture_full_content()
    if full.isNull():
        raise RuntimeError("截图生成失败，无法获取渲染内容")

    if full.height() > full.width() * 1.5:
        result = self._split_and_stitch(full)
    else:
        result = full
    result.save(file_path, "PNG")
```

✅ 短消息：`full.height() <= full.width() * 1.5` → 直接保存，不切分（`full` 已是实心背景合成图）
✅ 长消息：触发切分 → `_split_and_stitch` 水平拼接

- [ ] **Step 2: 确认无需改动**

`_export_as_image` 不需修改，逻辑保持。

- [ ] **Step 3: Commit（如有 changelog 等辅助文件）**

无代码改动，无需 commit。

---

## Task 6: 运行全部测试 + 静态检查

**Files:**
- Verify-only

- [ ] **Step 1: 跑全部新单元测试**

Run: `python -m pytest tests/widgets/ -v`
Expected: 5 tests PASS

- [ ] **Step 2: 跑既有的核心测试（确认无回归）**

Run: `python -m pytest tests/ -v --ignore=tests/widgets`
Expected: 既有测试 PASS（无新失败）

- [ ] **Step 3: 静态检查（pyright/mypy）**

Run: `python -m mypy app/widgets/message_card.py 2>&1 | head -30`
Expected: 无新增 type error（既有警告可忽略）

- [ ] **Step 4: 验证模块仍可 import**

Run: `python -c "from app.widgets.message_card import CodeWebViewer, MessageCard; print('import ok')"`
Expected: 输出 `import ok`

---

## Task 7: 手动 GUI 验证清单

**Files:**
- 无（用户手动验证）

- [ ] **Step 1: 启动 DriFox 准备测试**

Run: `python main.py`
Expected: 应用正常启动

- [ ] **Step 2: 验证短消息导出**

操作：
1. 发一条短消息（≤5 行 markdown）
2. 在助手回复卡片上右键 → 导出消息 → 保存为 PNG
3. 打开 PNG 检查

预期：
- 背景色 = 当前主题卡片色（非黑色）
- 内容完整，无空白
- 整图纵横比接近 1:1

- [ ] **Step 3: 验证长消息导出**

操作：
1. 让助手生成一条超长消息（含多个代码块、表格、列表，总高 > 2000px）
2. 导出为 PNG
3. 打开 PNG 检查

预期：
- 背景色 = 当前主题卡片色
- 内容从顶部到底部完整无遗漏
- 长宽比合理（接近 3:2 横幅）

- [ ] **Step 4: 验证切主题后导出**

操作：
1. 切换到另一个主题（如从 midnight 切到 amber）
2. 导出当前消息
3. 打开 PNG 检查

预期：
- 背景色跟随新主题（amber 的暖色卡背景）

- [ ] **Step 5: 验证速度感受**

主观对比：
- 修复前：长消息导出有明显等待（~1s）
- 修复后：等待缩短到 ~250ms

---

## Self-Review Checklist

- [x] **Spec coverage**: 4 个 spec 改动（`_get_card_bg_color`, `_capture_full_content`, `_compose_with_solid_bg`, 测试）都有对应 task
- [x] **No placeholders**: 所有代码块完整，命令有 Expected 输出
- [x] **Type consistency**: 全文用 `CodeWebViewer._split_and_stitch`（unbound 调用），`CodeWebViewer._get_card_bg_color`，`CodeWebViewer._compose_with_solid_bg`，无命名不一致
- [x] **TDD ordering**: Task 2 先测既有 `_split_and_stitch`（红→绿无需实现）→ Task 3 先红测强制实心 → 修复 → Task 4 整合 `_capture_full_content` 重写
- [x] **Frequent commits**: 每个 Task 末尾都有 `git commit`
- [x] **File paths exact**: `app/widgets/message_card.py` L 编号明确；`tests/widgets/test_export_image_helpers.py` 路径明确
