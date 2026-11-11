# -*- coding: utf-8 -*-
"""渲染页只读回显卡：本次进程「实际生效」的渲染参数 + 复制 / 恢复默认。

为什么要回显环境变量而不是重算 app.config
----------------------------------------
本页配置全部重启生效，用户改完最需要的是「我改的到底生效了没」。回显读的是
进程启动那一刻 apply_render_env 写进 os.environ 的值 —— 与当前配置不一致就说明
变更还没生效，无需再翻日志。环境变量缺失（非 Windows 不设 ANGLE、或外部
QTWEBENGINE_CHROMIUM_FLAGS 被清空）也如实显示。

两个操作按钮（icon-only + tooltip，符合项目按钮约定）：
- 复制：把 QT_OPENGL / QT_ANGLE_PLATFORM / QTWEBENGINE_CHROMIUM_FLAGS 完整拷走，
  方便贴给开发者或自己对照改 ExtraChromiumFlags
- 恢复默认：把 Render 组所有配置项写回 defaultValue（仍需重启生效）。
  其中内存治理核心六项同时是「平台推荐档」口径（render_env.platform_recommended_profile）：
  有偏离时在 content 里标注「N 项偏离推荐」，恢复默认的 tooltip 点名偏离项。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from qfluentwidgets import (
    ConfigItem,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    SettingCard,
    TransparentToolButton,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.render_env import describe_applied, platform_recommended_profile
from app.utils.utils import get_font_family_css
from app.widgets.elided_label import _ElidedLabel

# 环境变量 → 后端档位显示文本（刻意简短：这行会进卡片 sizeHint，太长会把设置
# 弹窗整体顶宽；完整参数只在 tooltip / 复制里给）
_BACKEND_TEXT = {
    "hardware": "硬件 D3D11",
    "software": "软件 WARP",
    "software_gl": "软件 GL",
    "vulkan": "Vulkan",
    "d3d9": "D3D9",
    "swiftshader": "SwiftShader",
    "custom": "外部覆盖",
    "": "系统默认",
}

# 推荐档配置键 → 中文短名（偏离提示用；键与 render_env 推荐档对齐）
_ITEM_LABELS = {
    "RenderBackend": "后端档位",
    "WebglEnabled": "WebGL",
    "ShareGLContexts": "GL 上下文共享",
    "JsHeapMb": "JS 堆上限",
    "RendererProcessLimit": "renderer 进程上限",
    "LowEndDeviceMode": "低内存模式",
}


class RenderStatusCard(SettingCard):
    """当前生效参数回显 + 复制 / 恢复默认"""

    def __init__(self, render_items, parent=None):
        """
        Args:
            render_items: Render 组 ConfigItem 列表（「恢复默认」的作用对象）
            parent: 父控件
        """
        super().__init__(FluentIcon.INFO, "当前生效参数", "", parent)
        self._items: list[ConfigItem] = list(render_items)
        self._applied: dict = {}

        self.copyBtn = TransparentToolButton(FluentIcon.COPY, self)
        self.copyBtn.setToolTip("复制完整参数")
        self.copyBtn.setFocusPolicy(Qt.NoFocus)
        self.copyBtn.clicked.connect(self._on_copy)

        self.resetBtn = TransparentToolButton(FluentIcon.SYNC, self)
        self.resetBtn.setToolTip("恢复默认（需重启生效）")
        self.resetBtn.setFocusPolicy(Qt.NoFocus)
        self.resetBtn.clicked.connect(self._on_reset)

        self.hBoxLayout.addWidget(self.copyBtn, 0, Qt.AlignRight)
        self.hBoxLayout.addSpacing(4)
        self.hBoxLayout.addWidget(self.resetBtn, 0, Qt.AlignRight)

        self.refresh_status()

    # ------------------------------------------------------------------
    # 回显
    # ------------------------------------------------------------------
    def refresh_status(self) -> None:
        """重新读取环境变量并刷新文案（切页/主题刷新时调用）"""
        self._applied = describe_applied()
        s = self._applied
        backend = _BACKEND_TEXT.get(s["backend"], s["backend"] or "系统默认")
        deviations = self._recommended_deviations()
        # 文案刻意短：contentLabel 的 sizeHint 会撑大卡片 → 顺着布局链把设置弹窗
        # 顶宽。这里只留后端 + 开关数 + 偏离数，GL 模式在本页有独立开关卡、不必重复，
        # 其余细节全在 tooltip 与「复制」里。
        content = f"{backend} · {s['flag_count']} 开关"
        if deviations:
            content += f" · {len(deviations)} 项偏离推荐"
        self.setContent(content)
        self.contentLabel.setToolTip(self.detail_text())
        if deviations:
            labels = []
            for item, _ in deviations:
                short = item.key.rsplit(".", 1)[-1]
                labels.append(_ITEM_LABELS.get(short) or short)
            self.resetBtn.setToolTip(f"恢复推荐档（偏离：{'、'.join(labels)}）（需重启生效）")
        else:
            self.resetBtn.setToolTip("已与平台推荐档一致")
        self._apply_content_style()

    def detail_text(self) -> str:
        """完整参数文本（tooltip / 复制共用）"""
        s = self._applied or describe_applied()
        lines = []
        if s["opengl"]:
            lines.append(f"QT_OPENGL={s['opengl']}")
        if s["angle"]:
            lines.append(f"QT_ANGLE_PLATFORM={s['angle']}")
        lines.append(f"QTWEBENGINE_CHROMIUM_FLAGS={s['flags']}")
        # 解析后的速览（上面那行原始 flags 太长，这里给人读）
        lines.append(f"Renderer 进程上限={s['renderer_process_limit']} · JS 堆={s['js_heap_mb']}MB")
        # Qt 属性类：不是环境变量，取自 apply_render_env 的留档
        lines.append(f"Qt.AA_UseOpenGLES={'开' if s.get('use_open_gles', True) else '关'}")
        lines.append(f"Qt.AA_ShareOpenGLContexts={'开' if s.get('share_gl_contexts', True) else '关'}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 推荐档对照
    # ------------------------------------------------------------------
    def _recommended_deviations(self) -> list:
        """内存核心六项中与平台推荐档不一致的 (ConfigItem, 推荐值) 列表。

        对照对象是 item 当前值（重启后才会生效），与「当前生效参数」的回显
        口径不同：刚改完还没重启时，偏离数反映的是待生效的目标态。
        """
        profile = platform_recommended_profile()
        out = []
        for item in self._items:
            short_key = item.key.rsplit(".", 1)[-1]
            if short_key in profile and item.value != profile[short_key]:
                out.append((item, profile[short_key]))
        return out

    def _apply_content_style(self) -> None:
        """必须显式写 font-size：会盖掉 apply_font_size_to_widget 的 content 规则"""
        self.contentLabel.setStyleSheet(
            f"QLabel#contentLabel {{ color: {Colors.TEXT_SECONDARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)} }}"
        )

    def refresh_style(self) -> None:
        """主题/字体刷新链：重刷文案配色与回显内容"""
        self.refresh_status()

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self.detail_text())
        self._toast("已复制", "渲染参数已复制到剪贴板")

    def _on_reset(self) -> None:
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        for item in self._items:
            cfg.set(item, item.defaultValue, save=True)
        self._toast("已恢复默认", "重启后生效")

    def _toast(self, title: str, content: str) -> None:
        """轻提示（parent 取顶层窗口，避免被设置卡裁切）"""
        parent = self.window()
        InfoBar.success(
            title=title,
            content=content,
            position=InfoBarPosition.BOTTOM,
            duration=2500,
            parent=parent if parent is not None else self,
        )
