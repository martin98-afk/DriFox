# UI 插件代码模板 — 输入框按钮 / 标题栏常驻 tab

> 何时读：输入框旁加快捷动作按钮、标题栏加常驻入口时。
> 前置依赖：patterns.md §10（全屏窗口模式）；剪贴板用 setImage、提示用 InfoBar（坑见 §9.3/§9.4）。
> 产出：register_input_button / register_titlebar_tab 注册骨架。

## 九、输入框按钮模板（输入框工具栏快捷动作）

> 参考实现：`plugins/quick-screenshot/`（选区截图按钮，2026-09-04 实战沉淀）。
> 适配场景：输入框加个按钮触发即时动作（截图、增强提示词、快捷插入等）。

### 9.1 注册 API 与 position 锚定

```python
registry.register_input_button(
    plugin_name,          # 插件名
    button_id,            # 按钮唯一 id
    icon_path=...,        # 深色主题图标（浅色线条 SVG）
    icon_light_path=...,  # 浅色主题图标（深色线条 SVG），主题切换主程序自动刷新
    tooltip="...",
    on_click=_on_clicked, # callback(context)
    on_right_click=None,  # 可选；注册后按钮右键派发 callback(context)，不弹系统菜单
    position="before:new_session",  # 可选；缺省 "end"
)
```

- `position` 取值：`"start"` / `"end"` / `"before:<id>"` / `"after:<id>"`；
  **锚点缺失时降级末尾追加，不报错**（安全兜底）。
- 系统锚点（objectName）：`new_session`（新建会话按钮）；
  `memory` / `history` 已迁工作台，仅剩零尺寸锚点（兼容旧插件，勿依赖视觉位置）。
- 图标为 **24×24 线性 SVG**，线条 `stroke`：深色主题用 `#d0d0d0`，浅色主题用 `#3a3a3a`。
- `on_click(context)` 的 context 含：`main_widget` / `window_id` / `item_id` / `tab_index`。
- `on_right_click` 仅新版主程序支持：旧主程序上多传该 kwarg 会 TypeError，
  插件侧用 `inspect.signature` 检测降级（参考 quick-screenshot `register_ui`）。

### 9.2 ui/__init__.py 完整骨架

```python
# -*- coding: utf-8 -*-
"""<功能一句话说明>。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

from loguru import logger

PLUGIN_NAME = "quick-screenshot"
_BUTTON_ID = "quick-screenshot"
_TOOLTIP = "..."

# 存活临时窗口/任务引用（模块级，单实例防护；按需）
_active_overlay = None


def _icons_dir() -> Path:
    # ui/__init__.py -> ui/icons/
    return Path(__file__).resolve().parent / "icons"


def register_ui(registry) -> None:
    """热重载兼容：清理旧子模块缓存，再注册按钮。"""
    prefix = f"ui_plugin_{PLUGIN_NAME}."
    for k in [k for k in sys.modules if k.startswith(prefix)]:
        del sys.modules[k]

    icons = _icons_dir()
    registry.register_input_button(
        PLUGIN_NAME,
        _BUTTON_ID,
        icon_path=str(icons / "screenshot.svg"),
        icon_light_path=str(icons / "screenshot_light.svg"),
        tooltip=_TOOLTIP,
        on_click=_on_clicked,
        position="before:new_session",
    )


def _on_clicked(context: Dict[str, Any]) -> None:
    global _active_overlay
    main_widget = context.get("main_widget")
    _close_stale()  # 单实例防护：旧窗口存活先关（见 §9.4）
    try:
        # 延迟 import PyQt5/qfluentwidgets/子模块 —— 注册期不加载 UI 依赖
        ...
    except Exception as e:  # noqa: BLE001 — 全流程兜底，不留残留窗口
        logger.error(f"[{PLUGIN_NAME}] 动作失败: {e}")
```

### 9.3 剪贴板写图规范（坑 ⚠️）

**往剪贴板写图片一律 `setImage(img.toImage())`，禁用 `setPixmap`：**

```python
QApplication.clipboard().setImage(pixmap.toImage())   # ✅
QApplication.clipboard().setPixmap(pixmap)            # ❌ 粘贴链路会静默失败
```

原因：`setPixmap` 后剪贴板所有权在本进程，**同进程粘贴时 Qt 直接返回内部对象**，
`mimeData().imageData()` 拿到的是 **QPixmap**；主程序输入框
`insertFromMimeData`（bottom_input_area.py）对图片的 `isinstance(img, QImage)` 检查
会静默跳过 → 表现为「复制成功提示有，粘贴却无反应」。
外部工具（微信/Snipaste）截图不受影响：剪贴板所有权在外部，Qt 读 CF_DIB 自动转 QImage。
（主程序 2026-09 已兼容 QPixmap 双类型，但插件侧直接存 QImage 语义最稳。）

### 9.4 提示通道规范（坑 ⚠️）

**用户反馈统一走 InfoBar（qfluentwidgets），QToolTip 在 DriFox 内不可靠：**
主程序源码明确注释「绕开 QToolTip 样式问题」（bottom_toolbar_module 自绘 hover tooltip），
QToolTip.showText 可能不显示。InfoBar 需要 parent：

```python
def _notify(main_widget, title: str, msg: str) -> None:
    try:
        from qfluentwidgets import InfoBar, InfoBarPosition

        InfoBar.success(title, msg, parent=main_widget,
                        position=InfoBarPosition.BOTTOM, duration=2500)
    except Exception:  # noqa: BLE001 — InfoBar 失败才降级 QToolTip
        from PyQt5.QtGui import QCursor
        from PyQt5.QtWidgets import QToolTip

        QToolTip.showText(QCursor.pos(), msg)
```

### 9.5 临时全屏窗口的生命周期模式

选区截图/全屏遮罩类动作窗口（完整模式见 `patterns.md §10`）：

- **每次点击新建实例**，窗口 `WA_DeleteOnClose`，用完即毁；
- **模块级引用 `_active_overlay` 做单实例防护**：重复点击先 `close()` 旧窗
  （`RuntimeError` 兜底 C++ 已销毁竞态）；
- `destroyed` 信号连接模块级清理函数，置空引用；
- 兜底原则：on_click 全流程 try/except，异常路径必须关闭已创建的全屏窗，
  **不允许残留置顶窗卡死桌面**。

### 9.6 验证清单

```
1. 按钮出现在锚定位置（如新建会话左侧）？      → position 锚点
2. 深浅主题切换图标都可见？                    → icon_path + icon_light_path 两套
3. 动作触发正常？热重载后仍正常？              → sys.modules 前缀清理
4. 剪贴板内容在 DriFox 输入框 Ctrl+V 有反应？  → setImage 而非 setPixmap（§9.3）
5. 成功/失败提示都可见？                       → InfoBar（§9.4），别用 QToolTip
6. 重复点击/热重载/异常路径无残留窗口？        → §9.5 单实例防护 + 兜底
```

> 完整验证清单见 `checklist.md §13`。

---

## 十、标题栏常驻 tab 模板（tab + full 卡组合）

> 参考实现：`plugins/agent_trace/ui/__init__.py`（轨迹）、`plugins/assistant_hub/ui/__init__.py`（助手）。
> 适配场景：插件有一个全屏级主界面，入口是窗口顶部常驻 tab（无 ×，点击切换）。

### 10.1 组合模式：titlebar_tab + full 容器浮动卡

标题栏 tab 只是**入口**（无内容区），点击后显示插件自己的 full 容器浮动卡：

```python
CARD_ID = "my_feature"  # tab_id 与 card_id 共用同一命名空间


def _on_tab_clicked() -> None:
    """tab 点击 → 切换显示 full 卡。"""
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        UIPluginRegistry.get_instance().toggle_floating_card(CARD_ID)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[my_feature] toggle 失败: {e}")


def register_ui(registry) -> None:
    # 热重载兼容：清理 ui_plugin_my_feature.* 旧子模块（略，见 §4.2）

    # ── full 容器浮动卡（内容本体）──
    registry.register_floating_card(
        plugin_name=PLUGIN_NAME,
        card_id=CARD_ID,
        widget_class=MyCardWidget,      # QWidget 子类，占满内容区
        container="full",
        title="我的功能",
        default_visible=False,
        metadata={
            "icon_dark": str(icons_dir / "icon.svg"),
            "icon_light": str(icons_dir / "icon_light.svg"),
            "full_card": True,     # 可被标题栏 full tab 接管（同设置卡）
            "hide_sidebar": True,  # 入口只有标题栏 tab，侧边栏不重复列出
        },
    )

    # ── 常驻标题栏 tab（入口）──
    registry.register_titlebar_tab(
        plugin_name=PLUGIN_NAME,
        tab_id=CARD_ID,
        label="我的功能",
        on_click=_on_tab_clicked,
        priority=0,          # 同 tab_id 高优先级覆盖
    )
```

### 10.2 踩坑记录（来自 agent_trace 实战）

- **tab 点击必须走 `UIPluginRegistry.toggle_floating_card(CARD_ID)`**，不能直接用
  `card_manager.show_card`：插件卡实例是懒创建的（首次点击时才 `_show_floating_card`
  创建），`show_card` 在 `card_widget is None` 处**静默 return**，表现就是"点了没反应"。
- `register_titlebar_tab` 的 `icon_path` **无主题感知**（CustomTabButton 单一路径渲染，
  不像 register_input_button 有 icon_light_path），深色主题下深色描边图标会不可见。
  **纯文字 label 更安全**；确要图标则给浅色线条 SVG 并实测深浅两主题。
- Tab 模式宿主是 TabManagerWindow（不是 MainWidget）：直接用
  `UIPluginRegistry.toggle_floating_card`，内部已做宿主解析；自己遍历 main_widget
  的 card_manager 在 Tab 模式下会找不到卡片。
- 语义细节：tab 已可见时再点不做动作（toggle 会关掉它，而点 tab 语义是"切到该 tab"），
  先 `card_manager.is_card_visible(CARD_ID, window_id)` 判断。

### 10.3 验证清单

```
1. tab 出现在标题栏，label 正确？              → register_titlebar_tab
2. 点击能打开 full 卡？再点 × 能关闭？         → toggle_floating_card（非 show_card！）
3. Tab 模式（多窗口）下点击仍正常？            → 宿主解析走 registry
4. 深浅主题下 tab 文字/图标都可见？            → 纯文字最稳
5. 已可见时点 tab 不闪退？                     → is_card_visible 前置判断
6. 热重载后 tab 与卡片均正常？                 → sys.modules 前缀清理
```

> 完整验证清单见 `checklist.md §14.1`。

---

