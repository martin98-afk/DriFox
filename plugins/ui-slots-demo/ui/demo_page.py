# -*- coding: utf-8 -*-
"""demo_page.py — 独立弹窗演示页（register_window 的 widget_class）

窗口壳（无边框 + 自定义标题栏 + 主题 + 生命周期）由主程序提供，
插件只写客户区内容。内容页可选实现三个约定接口（本页全部实现）：

- ``set_context_provider(provider)``：主程序注入上下文拉模型。每次调用
  provider() 返回最新上下文（主程序浮动卡同款机制）；窗口场景下上下文
  由插件自己在 register_window 时提供的 provider 决定。
- ``show_card()``：弹窗打开/激活时主程序调用（数据加载入口，与浮动卡一致）。
- ``refresh_theme()``：主题切换时主程序调用。

主题取色规范：优先读 ``ctx["colors"]``（text_primary/text_secondary/border/
accent），缺失时回退 ``isDarkTheme()`` 判断的硬编码安全色（对齐 git-panel
插件的 _ctx_text_color 模式）。
"""

from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from qfluentwidgets import isDarkTheme


def _ctx_colors(ctx) -> dict:
    """从上下文取主题色（缺失时按明暗主题回退），返回四色 dict"""
    colors = (ctx or {}).get("colors", {}) or {}
    dark = isDarkTheme()
    return {
        "text_primary": colors.get("text_primary")
        or ("rgba(255,255,255,0.9)" if dark else "rgba(0,0,0,0.85)"),
        "text_secondary": colors.get("text_secondary")
        or ("rgba(255,255,255,0.55)" if dark else "rgba(0,0,0,0.45)"),
        "border": colors.get("border") or ("rgba(255,255,255,0.12)" if dark else "rgba(0,0,0,0.12)"),
        "accent": colors.get("accent") or ("#62a0ea" if dark else "#2878dc"),
    }


class DemoWindowPage(QWidget):
    """演示页：说明 + 计数器 + 消息联动 + 上下文查看"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._provider = None
        self._count = 0
        self._setup_ui()
        self._apply_theme()

    # ── UI ──

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)

        self._title = QLabel("UI 扩展点演示", self)
        lay.addWidget(self._title)

        self._desc = QLabel(
            "本窗口由 register_window 注册，出现在主界面左侧「自定义插件」栏：\n"
            "点击条目开/关切换，右键菜单「弹出」重新打开。\n"
            "窗口壳（标题栏/拖动/最小化/关闭）由主程序提供，插件只写这里的客户区内容。",
            self,
        )
        self._desc.setWordWrap(True)
        lay.addWidget(self._desc)

        # 演示 1：窗口内交互与状态保持
        row = QHBoxLayout()
        self._btn_count = QPushButton("点我 +1", self)
        self._btn_count.clicked.connect(self._on_count)
        self._lbl_count = QLabel("点击次数：0（关窗重开会从 0 开始，实例随窗口销毁）", self)
        row.addWidget(self._btn_count)
        row.addWidget(self._lbl_count)
        row.addStretch(1)
        lay.addLayout(row)

        # 演示 2：与消息卡片按钮联动（user 按钮把消息内容送进来）
        self._lbl_msg = QLabel("最近点击的消息：—（点击用户消息下方的「发送到演示窗」按钮试试）", self)
        self._lbl_msg.setWordWrap(True)
        lay.addWidget(self._lbl_msg)

        # 演示 3：context provider 拉模型
        row2 = QHBoxLayout()
        self._btn_ctx = QPushButton("查看上下文", self)
        self._btn_ctx.clicked.connect(self._refresh_ctx)
        row2.addWidget(self._btn_ctx)
        row2.addStretch(1)
        lay.addLayout(row2)
        self._lbl_ctx = QLabel("", self)
        self._lbl_ctx.setWordWrap(True)
        lay.addWidget(self._lbl_ctx)

        lay.addStretch(1)

    # ── 主程序约定接口 ──

    def set_context_provider(self, provider):
        """主程序注入上下文拉模型（register_window 的 context_provider）"""
        self._provider = provider

    def show_card(self):
        """弹窗打开/激活时主程序调用：刷新主题 + 上下文（数据加载入口）"""
        self._apply_theme()
        self._refresh_ctx()

    def refresh_theme(self):
        """主题切换时主程序调用"""
        self._apply_theme()

    # ── 外部联动 ──

    def set_last_message(self, text: str):
        """消息卡片按钮把消息内容送进来展示（演示按钮↔弹窗联动）"""
        text = (text or "").strip().replace("\n", " ")
        snippet = text[:60] + ("…" if len(text) > 60 else "")
        self._lbl_msg.setText(f"最近点击的消息：{snippet or '（空消息）'}")

    # ── 内部 ──

    def _on_count(self):
        self._count += 1
        self._lbl_count.setText(f"点击次数：{self._count}")

    def _refresh_ctx(self):
        if self._provider is None:
            self._lbl_ctx.setText("上下文：provider 未注入")
            return
        try:
            ctx = self._provider()
        except Exception as e:
            self._lbl_ctx.setText(f"上下文 provider 异常：{e}")
            return
        keys = sorted(ctx.keys()) if isinstance(ctx, dict) else []
        self._lbl_ctx.setText("上下文键：" + (", ".join(keys) if keys else "（空）"))

    def _apply_theme(self):
        ctx = {}
        if self._provider is not None:
            try:
                ctx = self._provider()
            except Exception:
                ctx = {}
        c = _ctx_colors(ctx)
        self._title.setStyleSheet(
            f"color: {c['text_primary']}; font-size: 16px; font-weight: 600; background: transparent;"
        )
        self._desc.setStyleSheet(f"color: {c['text_secondary']}; font-size: 12px; background: transparent;")
        self._lbl_count.setStyleSheet(f"color: {c['text_primary']}; background: transparent;")
        self._lbl_msg.setStyleSheet(f"color: {c['text_primary']}; background: transparent;")
        self._lbl_ctx.setStyleSheet(f"color: {c['text_secondary']}; font-size: 12px; background: transparent;")