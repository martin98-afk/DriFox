# -*- coding: utf-8 -*-
"""assistant_card.py — 助手中心主页面（full 容器浮动卡 · 单列滚动版）

复刻 openhanako AgentTab 的单列结构（DriFox 化）：

    ┌────────────────────────────────────┐
    │        ArcCardStack 弧形卡片堆叠     │
    │   名称 + ★主助手 + 操作行（重命名等）  │
    │   基本信息（对话模型 / 记忆整理模型）    │
    │   关于 Ta（人格 chips / 身份 / AGENTS.md）│
    │   记忆（传送带 + Dream + 版本）        │
    │   经验（工具型 + 反思）               │
    │   专属技能                          │
    └────────────────────────────────────┘

宿主契约：widget_class=AssistantCardWidget、refresh_style()（主题切换回调）。
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import List

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import InfoBar, InfoBarPosition, MaskDialogBase, SingleDirectionScrollArea

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

from assistant_hub_manager import AssistantManager

from .arc_stack import ArcCardStack
from .assistant_avatar import RoundAvatar
from .overlays import DreamRevisionOverlay, PersonaManageDialog, TextViewOverlay, _dialog_btn
from .rename_dialog import RenameDialog
from .sections import (
    AboutSection,
    ExperienceSection,
    MemorySection,
    ProfileSection,
    SkillsSection,
    _btn_style,
)


def _open_dialog(dlg) -> int:
    """统一弹窗入口：直接 exec_（对齐 plugin-marketplace 模式，零几何干预）。

    ⚠ 不要对 MaskDialog 做 setGeometry / widget.move：MaskDialogBase 的
    _hBox 布局是卡片位置的权威，手动干预会与布局互相覆盖导致卡片漂移。
    """
    return dlg.exec_()


def _host_window():
    """弹窗 parent：完整主窗口（TabManagerWindow 单例，对齐 plugin-marketplace 做法）。

    self.window() 在 full 卡容器内可能返回卡片容器而非主窗口，导致
    MaskDialog 遮罩只盖住容器、定位异常。回退链：TabManagerWindow 单例
    → UIPluginRegistry 主 widget 的 window() → None（调用方自行兜底）。
    """
    try:
        from app.widgets.tab_manager_window import TabManagerWindow

        win = TabManagerWindow.get_instance()
        if win is not None:
            return win
    except Exception:
        pass
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        for mw in (getattr(reg, "_main_widget", None), *getattr(reg, "_window_main_widgets", {}).values()):
            if mw is not None:
                return mw.window()
    except Exception:
        pass
    return None


def _confirm_dialog(parent, title: str, text: str) -> bool:
    """统一 Mask 风格确认弹窗（替代 QMessageBox，系统字体+主题色）。parent 传 _host_window()。"""
    from PyQt5.QtGui import QColor

    class _Dialog(MaskDialogBase):
        def __init__(self):
            super().__init__(parent)
            self._yes = False
            self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
            self.setClosableOnMaskClicked(True)
            self.setDraggable(True)
            self.setMaskColor(QColor(0, 0, 0, 76))
            self.widget.setObjectName("hubConfirm")
            self.widget.setStyleSheet(
                f"#hubConfirm {{ background: {Colors.CARD_BG_SOLID}; border: 1px solid {Colors.BORDER};"
                f"border-radius: 12px; }}"
            )
            self.widget.setFixedSize(640, 420)
            v = QVBoxLayout(self.widget)
            v.setContentsMargins(24, 20, 24, 18)
            v.setSpacing(10)
            t = QLabel(title)
            t.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;"
                f"{get_font_family_css()} {font_size_css(14)}; font-weight: 600;"
            )
            v.addWidget(t)
            body = QLabel(text)
            body.setWordWrap(True)
            body.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
                f"{get_font_family_css()} {font_size_css(12)};"
            )
            v.addWidget(body)
            v.addStretch()
            row = QHBoxLayout()
            row.addStretch()
            cancel = _dialog_btn("取消")
            cancel.clicked.connect(self.reject)
            ok = _dialog_btn("确定", primary=True)
            ok.clicked.connect(self._yes_accept)
            row.addWidget(cancel)
            row.addWidget(ok)
            v.addLayout(row)
            # 对齐 plugin-marketplace 模式：widget 留在 _hBox 布局自动居中，不手动干预

        def _yes_accept(self):
            self._yes = True
            self.accept()

    dlg = _Dialog()
    _open_dialog(dlg)
    return dlg._yes


class AssistantCardWidget(QWidget):
    """助手中心主卡片（单列滚动页）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mgr = AssistantManager.get_instance()
        self._active_aid: str = ""
        self._dream_running = False
        # ── 插件背景完全透明（对齐主程序对话区做法）──
        # 不叠加任何背景层：Tab 内嵌时由 #chatFrame 半透明面板透出。
        # transparentOverlay：声明覆盖层容器（CardContainer）不画面板底。
        self.setProperty("transparentOverlay", True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("")
        self._build_ui()
        self._reload_all()

    # ══════════════════════════════════════════════════
    #  UI 构建
    # ══════════════════════════════════════════════════

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = SingleDirectionScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 1. 弧形卡片堆叠：置顶固定（不随下方分区滚动）
        self._stack = ArcCardStack()
        self._stack.selectionChanged.connect(self._on_select)
        self._stack.createRequested.connect(self._on_create)
        outer.addWidget(self._stack)

        outer.addWidget(scroll, 1)

        # 内容容器透明（对齐主程序对话区 chat_container 做法）
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self._content_v = QVBoxLayout(content)
        self._content_v.setContentsMargins(24, 8, 24, 24)
        self._content_v.setSpacing(14)

        inner = QWidget()
        # 内容最大宽度：主窗口够宽时展开到上限，不够宽则自适应（无横向滚动）
        inner.setMaximumWidth(1000)
        self._inner_v = QVBoxLayout(inner)
        self._inner_v.setContentsMargins(0, 0, 0, 0)
        self._inner_v.setSpacing(14)

        # 2. 名称行：头像/名字（左）· 设为主助手/删除（右）；改名走基本信息输入框
        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        self._avatar = RoundAvatar(size=44, text="?", color="#7C3AED", parent=self)
        self._avatar.setToolTip("头像跟随元人格，在下方「关于 Ta」处更换")
        name_row.addWidget(self._avatar)
        self._name_label = QLabel("(未选择)")
        self._name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(20)}; font-weight: 700;"
        )
        name_row.addWidget(self._name_label)
        # 主助手星标：仅色点+tooltip，不做胶囊徽章
        self._primary_badge = QLabel("★")
        self._primary_badge.setToolTip("主助手")
        self._primary_badge.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(14)};"
        )
        self._primary_badge.hide()
        name_row.addWidget(self._primary_badge)
        name_row.addStretch()
        for text, handler, danger in (
            ("设为主助手", self._on_set_primary, False),
            ("删除此助手", self._on_delete, True),
        ):
            btn = QPushButton(text)
            btn.setFixedHeight(30)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(_btn_style(danger=danger))
            btn.clicked.connect(handler)
            name_row.addWidget(btn)
            if text == "设为主助手":
                self._set_primary_btn = btn  # 已是主助手时隐藏
        self._inner_v.addLayout(name_row)

        # 3-6. 分区
        self._profile = ProfileSection()
        self._profile.saveRequested.connect(self._on_profile_save)
        self._inner_v.addWidget(self._profile)

        self._about = AboutSection(self._persona_items(), "")
        self._about.personaChangeRequested.connect(self._on_persona_change)
        self._about.personaManageRequested.connect(self._on_persona_manage)
        self._about.personaAvatarChangeRequested.connect(self._on_persona_avatar_change)
        self._about.personaAvatarResetRequested.connect(self._on_persona_avatar_reset)
        self._about.saveRequested.connect(self._on_about_save)
        self._inner_v.addWidget(self._about)

        self._memory = MemorySection()
        self._memory.toggleMemory.connect(self._on_memory_toggle)
        self._memory.toggleDreamAuto.connect(self._on_dream_auto_toggle)
        self._memory.viewToday.connect(self._on_view_today)
        self._memory.dreamRun.connect(self._on_dream_run)
        self._memory.dreamRestore.connect(self._on_dream_restore)
        self._memory.viewAll.connect(self._on_view_all)
        self._memory.clearAll.connect(self._on_clear_all)
        self._memory.pinAddRequested.connect(self._on_pin_add)
        self._memory.pinEdited.connect(self._on_pin_edit)
        self._memory.pinDeleteRequested.connect(self._on_pin_delete)
        self._inner_v.addWidget(self._memory)

        self._experience = ExperienceSection()
        self._experience.toggleExperience.connect(self._on_experience_toggle)
        self._experience.viewCategory.connect(self._on_view_experience)
        self._experience.reflectRequested.connect(self._on_reflect)
        self._inner_v.addWidget(self._experience)

        self._skills = SkillsSection()
        self._inner_v.addWidget(self._skills)

        self._inner_v.addStretch(1)

        center = QHBoxLayout()
        # stretch=1：内容区优先吃满剩余宽度；被 maxW(1400) 钳制后
        # 剩余空间由布局两端均分 → 窗口够宽时内容区恰好 1400 且居中。
        # （旧写法 addStretch+addWidget(inner) 无 stretch：inner 只拿 sizeHint
        #   ~412px，maxW 永远够不到 —— 配置区"很窄"的根因）
        center.addWidget(inner, 1)
        self._content_v.addLayout(center, 1)
        scroll.setWidget(content)
        # 透明背景：scroll 与内容容器都显式透明（viewport palette 底是插件内
        # 白色面板的根因，原生 QScrollArea 的后代选择器盖不住它）
        scroll.enableTransparentBackground()

    # ══════════════════════════════════════════════════
    #  数据绑定
    # ══════════════════════════════════════════════════

    def _persona_items(self) -> List[dict]:
        try:
            reg = self._mgr.persona_registry()
            items = []
            for p in reg.list_all():
                ap = reg.avatar_path(p.id)
                if p.id == "none":
                    # 「无」= 纯净助手，作为普通人格卡片参与选择（不再单独横幅）
                    items.append(
                        {
                            "id": "none",
                            "name": "纯净助手",
                            "description": "不附加人格底座",
                            "tag": "",
                            "avatar_path": str(ap or ""),
                        }
                    )
                    continue
                items.append(
                    {
                        "id": p.id,
                        "name": p.name,
                        "description": p.description,
                        "tag": p.tag,
                        "avatar_path": str(ap or ""),
                    }
                )
            return items
        except Exception:
            return []

    def _on_persona_avatar_change(self) -> None:
        a = self._mgr.get(self._active_aid)
        if not a or not a.yuan:
            return
        self._open_persona_avatar_dialog(a.yuan)

    def _on_persona_avatar_reset(self) -> None:
        a = self._mgr.get(self._active_aid)
        if not a or not a.yuan:
            return
        self._mgr.persona_registry().clear_avatar(a.yuan)
        self._refresh_persona_avatar()
        self._notify("已恢复人格默认头像")

    def _open_persona_avatar_dialog(self, pid: str) -> None:
        """人格头像选择器（预置/上传），确定后写入用户级覆盖（Mask 风格）。

        ⚠ 内层类方法里的 self 是 dialog 实例——reg/pid 必须先捕获为局部变量。
        """
        reg = self._mgr.persona_registry()
        p = reg.get(pid)
        if p is None:
            return
        from PyQt5.QtGui import QColor

        from .avatar_picker import AvatarPicker

        cur_ap = reg.avatar_path(pid)

        class _AvatarDialog(MaskDialogBase):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
                self.setClosableOnMaskClicked(True)
                self.setDraggable(True)
                self.setMaskColor(QColor(0, 0, 0, 76))
                self.widget.setObjectName("hubAvatarDialog")
                self.widget.setStyleSheet(
                    f"#hubAvatarDialog {{ background: {Colors.CARD_BG_SOLID};"
                    f"border: 1px solid {Colors.BORDER}; border-radius: 14px; }}"
                )
                self.widget.setFixedSize(780, 620)
                v = QVBoxLayout(self.widget)
                v.setContentsMargins(20, 16, 20, 16)
                picker = AvatarPicker(
                    assistant_id="",
                    parent=self.widget,
                    upload_saver=lambda data, ext: reg.set_avatar(pid, data, ext),
                )
                picker.set_assistant(
                    aid="",
                    color="#7C3AED",
                    name=p.name or pid,
                    image_path=str(cur_ap) if cur_ap else "",
                )
                v.addWidget(picker, 1)
                self._picker = picker  # 确定后外部取选择结果
                row = QHBoxLayout()
                row.addStretch()
                cancel = _dialog_btn("取消")
                cancel.clicked.connect(self.reject)
                ok = _dialog_btn("确定", primary=True)
                ok.clicked.connect(self.accept)
                row.addWidget(cancel)
                row.addWidget(ok)
                v.addLayout(row)
                # 对齐 plugin-marketplace 模式：widget 留在 _hBox 布局自动居中，不手动干预

        dlg = _AvatarDialog(_host_window() or self.window())
        if not _open_dialog(dlg):
            return
        sel = dlg._picker.get_selection()
        try:
            if sel.get("image_path"):
                data = Path(sel["image_path"]).read_bytes()
                ext = Path(sel["image_path"]).suffix.lstrip(".") or "png"
                if not reg.set_avatar(pid, data, ext):
                    self._notify_error("头像保存失败：不支持的图片格式")
                    return
            else:
                # 纯色选择/未选图 → 清除覆盖，回落人格默认头像
                reg.clear_avatar(pid)
        except Exception as e:
            self._notify_error(f"头像保存失败: {e}")
            return
        self._refresh_persona_avatar()
        self._notify("人格头像已更新")

    def _refresh_persona_avatar(self) -> None:
        """人格头像变更/切换人格后刷新三处：About 预览、编辑区头像、弧形卡片。"""
        aid = self._active_aid
        a = self._mgr.get(aid) if aid else None
        if not a:
            return
        reg = self._mgr.persona_registry()
        p = reg.get(a.yuan)
        ap = reg.avatar_path(a.yuan) if a.yuan else None
        self._about.set_persona_avatar(
            str(ap) if ap else "",
            (p.name if p else "") or (a.yuan or ""),
            has_override=reg.has_avatar_override(a.yuan) if a.yuan else False,
        )
        full = self._mgr.assistant_avatar_path(aid)
        self._avatar.set_image(str(full) if full else None)
        self._stack.set_avatar(aid, str(full) if full else "")
        # 人格 chip 头像轻量同步（当前人格）
        self._about.refresh_avatar(a.yuan, str(ap) if ap else "")

    def _reload_all(self, select_aid: str = "") -> None:
        assistants = self._mgr.list_assistants()
        self._stack.set_assistants(
            [
                {
                    "id": a.id,
                    "name": a.name or a.id,
                    "color": a.color,
                    "avatar_path": str(self._mgr.assistant_avatar_path(a.id) or ""),
                }
                for a in assistants
            ]
        )
        primary = next((a.id for a in assistants if a.primary), "")
        self._stack.set_primary(primary)
        self._about.rebuild_chips(self._persona_items())
        if select_aid and self._mgr.has(select_aid):
            self._bind_editor(select_aid)
        elif self._active_aid and self._mgr.has(self._active_aid):
            self._bind_editor(self._active_aid)
        elif assistants:
            self._bind_editor(assistants[0].id)
        else:
            self._show_empty()

    def _show_empty(self) -> None:
        self._active_aid = ""
        self._name_label.setText("(无助手)")
        self._avatar.set_text("?")

    def _bind_editor(self, aid: str) -> None:
        self._active_aid = aid
        a = self._mgr.get(aid)
        if not a:
            return
        # 选中即激活：经验工具 / 记忆注入 / ticker 都以 active_id 为准
        if self._mgr.active_id() != aid:
            self._mgr.set_active(aid)
        ap = self._mgr.assistant_avatar_path(aid)
        self._avatar.set_text(a.name or a.id)
        self._avatar.set_color(a.color)
        self._avatar.set_image(str(ap) if ap else None)
        self._name_label.setText(a.name or a.id)
        self._primary_badge.setVisible(a.primary)
        self._set_primary_btn.setVisible(not a.primary)
        self._stack.set_selected(aid)

        aid_capture = aid

        def _do_bind() -> None:
            mgr = self._mgr
            self._profile.bind(a.name or a.id, a.utility_model or "")
            self._about.set_persona(a.yuan)
            self._refresh_persona_avatar()
            identity, _ = mgr.read_identity_source(aid_capture)
            agents_md, _ = mgr.read_agents_md_source(aid_capture)
            self._about.bind_texts(identity, agents_md)
            self._memory.set_memory_enabled(a.memory_enabled)
            self._memory.set_dream_auto(a.dream_auto_enabled)
            self._memory.reload_pins(mgr.read_pinned(aid_capture))
            self._memory.set_status(self._memory_status(aid_capture))
            self._memory.set_dream_hint("每日自动 Dream 已开启" if a.dream_auto_enabled else "每日自动 Dream 未开启")
            self._experience.set_enabled(a.experience_enabled)
            self._experience.reload_categories(mgr.experience_list(aid_capture))
            self._skills.reload_skills(mgr.list_skills(aid_capture), self._on_view_skill)

        QTimer.singleShot(0, _do_bind)

    def _memory_status(self, aid: str) -> str:
        try:
            text = self._mgr.compiled_memory(aid)
            return f"已编译 {len(text)} 字" if text else "暂无记忆"
        except Exception:
            return "暂无记忆"

    # ══════════════════════════════════════════════════
    #  选择 / 创建 / 删除 / 主助手 / 重命名
    # ══════════════════════════════════════════════════

    def _on_select(self, aid: str) -> None:
        if aid and self._mgr.has(aid):
            self._bind_editor(aid)

    def _on_create(self) -> None:
        dlg = RenameDialog(
            title="新建助手",
            hint="输入助手名称（例如：小助手 / 翻译官 / 代码审查员）：",
            default="",
            parent=_host_window() or self.window(),
        )
        dlg.confirmed.connect(self._do_create)
        _open_dialog(dlg)

    def _do_create(self, name: str) -> None:
        a = self._mgr.create(name=name)
        self._reload_all(select_aid=a.id)
        self._notify(f"助手「{a.name}」已创建")

    def _on_delete(self) -> None:
        if not self._active_aid:
            return
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        ret = _confirm_dialog(
            _host_window() or self.window(),
            "删除助手",
            f"确定删除助手「{a.name}」？\n其身份、提示词、记忆、头像将一并删除。\n（该操作不可撤销）",
        )
        if not ret:
            return
        if self._mgr.delete(a.id):
            self._active_aid = ""
            self._reload_all()
            self._notify(f"助手「{a.name}」已删除")
        else:
            self._notify_error("至少保留一个助手，无法删除")

    def _on_set_primary(self) -> None:
        if not self._active_aid:
            return
        if self._mgr.set_primary(self._active_aid):
            self._reload_all(select_aid=self._active_aid)
            self._notify("已设为主助手")

    # ══════════════════════════════════════════════════
    #  基本信息 / 关于 Ta
    # ══════════════════════════════════════════════════

    def _on_profile_save(self, name: str, utility_model: str) -> None:
        """基本信息实时保存（对话模型跟随系统当前配置，不再持久化覆盖）。"""
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        changed = False
        if name and name != a.name:
            a.name = name
            changed = True
        if a.model:  # 历史遗留的对话模型覆盖一并清除，避免旧值暗中生效
            a.model = ""
            changed = True
        if a.utility_model != utility_model:
            a.utility_model = utility_model
            changed = True
        if not changed:
            return
        self._mgr.update(a)
        self._mgr.invalidate_context(a.id)
        # 轻量更新显示（不整页 reload，避免打断节流保存中的编辑）
        if name:
            self._name_label.setText(name)
            self._avatar.set_text(name)
        if a.primary:
            self._reload_all(select_aid=a.id)

    def _on_persona_change(self, pid: str) -> None:
        a = self._mgr.get(self._active_aid)
        if not a or a.yuan == pid:
            return
        # 身份/AGENTS 回落链依赖人格专属模板：若编辑框未被用户改过（内容 == 落盘/回落值），
        # 切人格时清掉固化副本落盘文件 → 回落新人格模板；真自定义则保留并提示
        identity, _ = self._mgr.read_identity_source(a.id)
        agents_md, _ = self._mgr.read_agents_md_source(a.id)
        cur_identity, cur_agents = self._about.texts()
        identity_custom = cur_identity.strip() != identity.strip()
        agents_custom = cur_agents.strip() != agents_md.strip()
        if not identity_custom:
            self._mgr.clear_identity(a.id)
        if not agents_custom:
            self._mgr.clear_agents_md(a.id)
        a.yuan = pid
        self._mgr.update(a)
        self._mgr.invalidate_context(a.id)
        self._about.set_persona(pid)
        self._refresh_persona_avatar()
        # 重读新人格回落内容刷新编辑框（bind_texts 内部 suspend 自动保存，不会误写盘）
        new_identity, _ = self._mgr.read_identity_source(a.id)
        new_agents_md, _ = self._mgr.read_agents_md_source(a.id)
        self._about.bind_texts(new_identity, new_agents_md)
        if identity_custom or agents_custom:
            self._notify("身份/AGENTS 已自定义，切换人格不覆盖")
        else:
            self._notify(f"人格已切换：{'无（纯净助手）' if pid == 'none' else pid}")

    def _on_persona_manage(self) -> None:
        reg = self._mgr.persona_registry()
        current = self._mgr.get(self._active_aid).yuan if self._active_aid else ""
        dlg = PersonaManageDialog(reg, current, parent=_host_window() or self.window())
        _open_dialog(dlg)
        self._reload_all(select_aid=self._active_aid)

    def _on_about_save(self, identity: str, agents_md: str) -> None:
        """实时保存（节流触发）：静默落盘，不弹提示打断编辑。

        防固化：内容与当前回落值一致（未真正自定义）时不写盘——否则回落态
        模板副本会被固化成自定义文件，切人格永远不跟随。
        """
        aid = self._active_aid
        if not aid:
            return
        cur_identity, _ = self._mgr.read_identity_source(aid)
        if identity.strip() != cur_identity.strip():
            self._mgr.write_identity(aid, identity)
        cur_agents, _ = self._mgr.read_agents_md_source(aid)
        if agents_md.strip() != cur_agents.strip():
            self._mgr.write_agents_md(aid, agents_md)
        self._mgr.invalidate_context(aid)

    # ══════════════════════════════════════════════════
    #  记忆
    # ══════════════════════════════════════════════════

    def _on_memory_toggle(self, on: bool) -> None:
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        a.memory_enabled = bool(on)
        self._mgr.update(a)
        self._mgr.invalidate_context(a.id)
        self._memory.setEnabled_all(bool(on))

    def _on_dream_auto_toggle(self, on: bool) -> None:
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        a.dream_auto_enabled = bool(on)
        self._mgr.update(a)
        self._memory.set_dream_hint("每日自动 Dream 已开启" if on else "每日自动 Dream 未开启")

    def _reload_pinned(self, aid: str) -> None:
        """写盘后回刷置顶列表：UI 永远以盘上数据为准（增删改即时可见）。"""
        self._memory.reload_pins(self._mgr.read_pinned(aid))

    def _on_pin_add(self, text: str) -> None:
        aid = self._active_aid
        if not aid:
            return
        items = self._mgr.read_pinned(aid)
        items.append((f"pin-{uuid.uuid4().hex[:12]}", text))
        self._mgr.write_pinned(aid, items)
        self._mgr.invalidate_context(aid)
        self._reload_pinned(aid)

    def _on_pin_edit(self, pid: str, text: str) -> None:
        aid = self._active_aid
        if not aid or not text:
            return  # 空内容忽略，避免误清
        items = [(p, text if p == pid else c) for p, c in self._mgr.read_pinned(aid)]
        self._mgr.write_pinned(aid, items)
        self._mgr.invalidate_context(aid)

    def _on_pin_delete(self, pid: str) -> None:
        aid = self._active_aid
        if not aid:
            return
        items = [(p, c) for p, c in self._mgr.read_pinned(aid) if p != pid]
        self._mgr.write_pinned(aid, items)
        self._mgr.invalidate_context(aid)
        self._reload_pinned(aid)

    def _on_view_today(self) -> None:
        if not self._active_aid:
            return
        path = self._mgr.memory_dir(self._active_aid) / "today.md"
        text = path.read_text(encoding="utf-8") if Path(path).exists() else ""
        _open_dialog(TextViewOverlay("当下记忆", text or "（暂无当下记忆）", parent=_host_window() or self.window()))

    def _on_view_all(self) -> None:
        if not self._active_aid:
            return
        text = self._mgr.compiled_memory(self._active_aid)
        _open_dialog(TextViewOverlay("所有记忆", text or "（暂无记忆）", parent=_host_window() or self.window()))

    def _on_dream_run(self) -> None:
        if not self._active_aid or self._dream_running:
            return
        aid = self._active_aid
        self._dream_running = True
        self._memory.set_dream_running(True)

        def _worker():
            try:
                result = self._mgr.dream_start(aid, "manual")
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            QTimer.singleShot(0, lambda: self._dream_done(result))

        threading.Thread(target=_worker, daemon=True).start()

    def _dream_done(self, result: dict) -> None:
        self._dream_running = False
        self._memory.set_dream_running(False)
        if result.get("ok"):
            self._memory.set_status(self._memory_status(self._active_aid))
            self._notify("Dream 整理完成" + ("（内容无变化）" if not result.get("changed") else ""))
        else:
            self._notify_error(f"Dream 失败：{result.get('error', '未知错误')}")

    def _on_dream_restore(self) -> None:
        if not self._active_aid:
            return
        aid = self._active_aid
        revisions = self._mgr.dream_revisions(aid)
        if not revisions:
            self._notify_error("暂无 Dream 版本")
            return

        def _preview(rid: str) -> str:
            try:
                p = self._mgr.dream_dir(aid) / "revisions" / f"{rid}.json"
                if not p.exists():
                    return "（无法读取版本内容）"
                before = (json.loads(p.read_text(encoding="utf-8")) or {}).get("before") or {}
                parts = [f"# 事实\n{before.get('facts', '')}", f"# 长期\n{before.get('longterm', '')}"]
                for d in before.get("daily") or []:
                    parts.append(f"# {d.get('date')}\n{d.get('body', '')}")
                return "\n\n".join(parts)
            except Exception:
                return "（无法读取版本内容）"

        dlg = DreamRevisionOverlay(
            revisions,
            preview_fn=_preview,
            restore_fn=lambda rid: self._mgr.dream_restore(aid, rid),
            parent=_host_window() or self.window(),
        )
        _open_dialog(dlg)
        self._memory.set_status(self._memory_status(aid))

    def _on_clear_all(self) -> None:
        if not self._active_aid:
            return
        aid = self._active_aid
        ret = _confirm_dialog(_host_window() or self.window(), "清除记忆", "确定清除该助手的全部记忆（置顶记忆保留）？\n该操作不可撤销。")
        if not ret:
            return
        mem = self._mgr.memory_dir(aid)
        for name in ("today.md", "longterm.md", "facts.md", "memory.md"):
            p = mem / name
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        self._mgr.invalidate_context(aid)
        self._memory.set_status(self._memory_status(aid))
        self._notify("记忆已清除")

    # ══════════════════════════════════════════════════
    #  经验 / 技能
    # ══════════════════════════════════════════════════

    def _on_experience_toggle(self, on: bool) -> None:
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        a.experience_enabled = bool(on)
        self._mgr.update(a)
        self._experience.set_enabled(bool(on))

    def _on_view_experience(self, category: str) -> None:
        if not self._active_aid:
            return
        aid = self._active_aid
        text = self._mgr.experience_read(aid, category)

        def _save(content: str):
            lines = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
            doc_path = self._mgr.experience_dir(aid) / f"{category}.md"
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
            try:
                self._mgr._core_experience().rebuild_index(self._mgr.assistant_dir(aid))
            except Exception:
                pass

        _open_dialog(
            TextViewOverlay(f"经验 · {category}", text, editable=True, on_save=_save, parent=_host_window() or self.window())
        )
        self._experience.reload_categories(self._mgr.experience_list(aid))

    def _on_reflect(self) -> None:
        if not self._active_aid or self._dream_running:
            return
        aid = self._active_aid
        self._notify("反思进行中…")

        def _worker():
            r = self._mgr.experience_reflect(aid)
            msg = (
                f"反思完成：新增 {r.get('added', 0)} 条经验"
                if r.get("added")
                else f"反思完成：暂无新经验 {('(' + r.get('error', '') + ')') if r.get('error') else ''}"
            )
            QTimer.singleShot(0, lambda: self._notify(msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_view_skill(self, name: str) -> None:
        if not self._active_aid:
            return
        text = self._mgr.read_skill(self._active_aid, name)
        _open_dialog(
            TextViewOverlay(
                f"技能 · {name}",
                text,
                editable=True,
                on_save=lambda c: self._mgr.write_skill(self._active_aid, name, c),
                parent=_host_window() or self.window(),
            )
        )

    # ══════════════════════════════════════════════════
    #  通知 / 宿主契约
    # ══════════════════════════════════════════════════

    def _notify(self, text: str) -> None:
        try:
            InfoBar.success(
                title="助手中心",
                content=text,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2500,
                parent=_host_window() or self.window(),
            )
        except Exception:
            pass

    def _notify_error(self, text: str) -> None:
        try:
            InfoBar.error(
                title="助手中心",
                content=text,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=_host_window() or self.window(),
            )
        except Exception:
            pass

    def refresh_style(self) -> None:
        """主题切换后刷新（宿主契约）"""
        Colors.refresh()
        self._reload_all(select_aid=self._active_aid)
