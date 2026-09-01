# -*- coding: utf-8 -*-
"""overlays.py — 助手中心浮层（查看记忆 / Dream 版本浏览器 / 人格管理）

统一视觉：半透明遮罩 + 圆角卡片（对齐原版 agent-create-overlay）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QIcon
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import LineEdit, MaskDialogBase

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

from assistant_hub_manager import AssistantManager

from .assistant_avatar import RoundAvatar


def _persona_cls():
    """Persona dataclass（经 manager 的 core 加载器取，与 hook/UI 单例语义一致）。"""
    mod = sys.modules.get("assistant_hub_manager")
    if mod is not None:
        return mod._load_core_module("persona", "persona.py").Persona
    # 兑底：直接按路径加载
    import importlib.util

    path = _PLUGIN_ROOT / "core" / "persona.py"
    spec = importlib.util.spec_from_file_location("assistant_hub_core.persona", str(path))
    mod2 = importlib.util.module_from_spec(spec)
    sys.modules["assistant_hub_core.persona"] = mod2
    spec.loader.exec_module(mod2)
    return mod2.Persona


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _label(text: str, size: int = 11, muted: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(
        f"color: {Colors.TEXT_MUTED if muted else Colors.TEXT_PRIMARY};"
        f"background: transparent; border: none;"
        f"{get_font_family_css()} {font_size_css(size)};" + ("font-weight: 600;" if not muted else "")
    )
    return lbl


def _dialog_btn(text: str, *, primary: bool = False) -> QPushButton:
    """统一弹窗按钮：取消=边框款，确定=accent 填充；系统字体+字号。"""
    btn = QPushButton(text)
    btn.setFixedHeight(32)
    btn.setCursor(Qt.PointingHandCursor)
    if primary:
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background: {Colors.TEXT_ACCENT}; color: #ffffff; border: none;
                border-radius: 8px; padding: 4px 26px;
                {get_font_family_css()} {font_size_css(12)}; font-weight: 600;
            }}
            QPushButton:hover {{ background: {Colors.TEXT_ACCENT}; }}
        """
        )
    else:
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent; color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER}; border-radius: 8px; padding: 4px 26px;
                {get_font_family_css()} {font_size_css(12)};
            }}
            QPushButton:hover {{ background: {Colors.HOVER_BG}; border-color: {Colors.TEXT_ACCENT}; }}
        """
        )
    return btn


class OverlayBase(MaskDialogBase):
    """浮层基类：遮罩 + 圆角卡片 + 标题栏（对齐主程序 MaskDialog 先例）。"""

    def __init__(self, title: str, parent: Optional[QWidget] = None, width: int = 560, height: int = 480):
        super().__init__(parent)
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))
        self.widget.setObjectName("hubOverlayCard")
        self.widget.setStyleSheet(
            f"""
            #hubOverlayCard {{
                background: {Colors.CARD_BG_SOLID};
                border: 1px solid {Colors.BORDER};
                border-radius: 14px;
            }}
        """
        )
        # 卡片本体定尺寸；遮罩层（self）保持全屏。
        # ⚠ 对齐 plugin-marketplace 模式：widget 留在 MaskDialogBase 的 _hBox
        # 布局里自动居中——不要手动 move/resizeEvent 干预，否则与布局互相
        # 覆盖，卡片位置随重排时序漂移（此前弹窗偏位的根因）。
        self.widget.setFixedSize(width, height)
        self._wrap = self.widget
        self._v = QVBoxLayout(self._wrap)
        self._v.setContentsMargins(20, 16, 20, 16)
        self._v.setSpacing(8)
        self._v.addWidget(_label(title, 14))


class TextViewOverlay(OverlayBase):
    """只读/可编辑文本查看浮层（查看当下记忆 / 查看记忆 / 经验分类）。"""

    def __init__(
        self,
        title: str,
        text: str,
        *,
        editable: bool = False,
        on_save: Optional[Callable[[str], None]] = None,
        parent=None,
    ):
        super().__init__(title, parent, width=620, height=520)
        self._on_save = on_save
        self._edit = QPlainTextEdit(text)
        self._edit.setReadOnly(not editable)
        self._edit.setStyleSheet(
            f"""
            QPlainTextEdit {{
                background: {Colors.CARD_BG.format(alpha=120)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(12)};
                padding: 8px;
            }}
        """
        )
        self._v.addWidget(self._edit, 1)
        row = QHBoxLayout()
        row.addStretch()
        close = _dialog_btn("关闭")
        close.clicked.connect(self.accept)
        if editable and on_save is not None:
            save = _dialog_btn("保存", primary=True)
            save.clicked.connect(self._do_save)
            row.addWidget(save)
        row.addWidget(close)
        self._v.addLayout(row)

    def _do_save(self) -> None:
        if self._on_save is not None:
            self._on_save(self._edit.toPlainText())
        self.accept()


class DreamRevisionOverlay(OverlayBase):
    """Dream 版本浏览器：列表 + 预览 + 恢复。"""

    def __init__(
        self,
        revisions: List[Dict[str, Any]],
        preview_fn: Callable[[str], str],
        restore_fn: Callable[[str], Dict[str, Any]],
        parent=None,
    ):
        super().__init__("Dream 版本历史", parent, width=640, height=520)
        self._preview_fn = preview_fn
        self._restore_fn = restore_fn
        self._revisions = revisions
        self._list = QListWidget()
        self._list.setStyleSheet(self._list_style())
        for rev in revisions:
            item = QListWidgetItem(
                f"[{rev.get('createdAt', '')}] {rev.get('kind', 'dream')} · "
                f"{rev.get('trigger', '')} · 长期 {rev.get('longtermChars', 0)} 字"
            )
            item.setData(Qt.UserRole, rev.get("revisionId", ""))
            self._list.addItem(item)
        self._v.addWidget(self._list, 2)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet(self._edit_style())
        self._v.addWidget(self._preview, 1)
        row = QHBoxLayout()
        self._restore_btn = QPushButton("恢复此版本")
        self._restore_btn.setCursor(Qt.PointingHandCursor)
        self._restore_btn.setStyleSheet(
            f"QPushButton {{"
            f"    background: transparent; color: {Colors.TEXT_ACCENT};"
            f"    border: 1px solid {Colors.TEXT_ACCENT}; border-radius: 8px; padding: 4px 18px;"
            f"    {get_font_family_css()} {font_size_css(12)};"
            f"}}"
            f"QPushButton:hover {{ background: rgba(245, 158, 11, 0.12); }}"
        )
        self._restore_btn.clicked.connect(self._do_restore)
        self._restore_btn.setEnabled(False)
        row.addWidget(self._restore_btn)
        row.addStretch()
        close = _dialog_btn("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        self._v.addLayout(row)
        self._list.currentItemChanged.connect(self._on_select)

    def _list_style(self) -> str:
        return f"""
            QListWidget {{ background: transparent; border: none; {get_font_family_css()} {font_size_css(12)} }}
            QListWidget::item {{ padding: 4px 6px; border-radius: 6px; color: {Colors.TEXT_PRIMARY}; }}
            QListWidget::item:hover {{ background: {Colors.HOVER_BG}; }}
            QListWidget::item:selected {{ background: {Colors.SELECTED_BG}; }}
        """

    def _edit_style(self) -> str:
        return f"""
            QPlainTextEdit {{
                background: {Colors.CARD_BG.format(alpha=120)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px; color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(11)}; padding: 6px;
            }}
        """

    def _on_select(self, cur, _prev):
        if cur is None:
            self._restore_btn.setEnabled(False)
            return
        rid = cur.data(Qt.UserRole)
        self._preview.setPlainText(self._preview_fn(rid))
        self._restore_btn.setEnabled(True)

    def _do_restore(self):
        cur = self._list.currentItem()
        if cur is None:
            return
        r = self._restore_fn(cur.data(Qt.UserRole))
        if r.get("ok"):
            self.accept()


class PersonaManageDialog(OverlayBase):
    """人格管理：新建/编辑/删除自定义人格（builtin 字段只读，头像均可自定义）。

    布局：左列表（头像 + 名称 + 内置徽章）｜右侧 QStackedWidget（编辑页 / 选头像页）。
    头像属于人格：换头像写用户级覆盖目录，所有助手自动跟随。
    """

    def __init__(self, registry, current_pid: str, parent=None):
        super().__init__("人格管理", parent, width=720, height=560)
        self._registry = registry
        self._current_pid = current_pid
        self._editing_pid: str = ""

        body = QHBoxLayout()
        body.setSpacing(14)

        # ── 左：人格列表 ──
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(_label("人格", 12))
        self._list = QListWidget()
        self._list.setStyleSheet(self._list_style())
        self._list.currentItemChanged.connect(self._on_select)
        left.addWidget(self._list, 1)
        list_btns = QHBoxLayout()
        list_btns.setSpacing(6)
        new_btn = _dialog_btn("新建")
        new_btn.clicked.connect(self._on_new)
        list_btns.addWidget(new_btn)
        self._del_btn = QPushButton("删除")
        self._del_btn.setFixedHeight(32)
        self._del_btn.setCursor(Qt.PointingHandCursor)
        self._del_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent; color: {Colors.ERROR};
                border: 1px solid {Colors.ERROR}; border-radius: 8px; padding: 4px 18px;
                {get_font_family_css()} {font_size_css(12)};
            }}
            QPushButton:hover {{ background: rgba(220, 38, 38, 0.10); }}
            QPushButton:disabled {{ color: {Colors.TEXT_MUTED}; border-color: {Colors.BORDER}; }}
        """
        )
        self._del_btn.setEnabled(False)
        self._del_btn.clicked.connect(self._on_delete)
        list_btns.addWidget(self._del_btn)
        left.addLayout(list_btns)
        body.addLayout(left, 1)

        # ── 右：编辑页 / 选头像页 ──
        from PyQt5.QtWidgets import QStackedWidget

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_edit_page())
        self._stack.addWidget(self._build_avatar_page())
        body.addWidget(self._stack, 2)
        self._v.addLayout(body, 1)

        row = QHBoxLayout()
        row.addStretch()
        close = _dialog_btn("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        self._v.addLayout(row)
        self._reload()

    # ── 编辑页 ──

    def _build_edit_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # 头像行：预览 + 更换/恢复（头像属于人格，builtin 也可换）
        av_row = QHBoxLayout()
        av_row.setSpacing(10)
        self._av_preview = RoundAvatar(size=64, text="?", color="#7C3AED")
        av_row.addWidget(self._av_preview)
        av_info = QVBoxLayout()
        av_info.setSpacing(2)
        self._av_name = QLabel("—")
        self._av_name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(13)}; font-weight: 600;"
        )
        av_info.addWidget(self._av_name)
        av_info.addWidget(_label("头像属于人格，切换人格自动跟随。", 10, muted=True))
        av_row.addLayout(av_info)
        av_row.addStretch()
        self._av_reset_btn = _dialog_btn("恢复默认")
        self._av_reset_btn.setEnabled(False)
        self._av_reset_btn.clicked.connect(self._on_avatar_reset)
        av_row.addWidget(self._av_reset_btn)
        self._av_change_btn = _dialog_btn("更换头像")
        self._av_change_btn.clicked.connect(self._on_avatar_change)
        av_row.addWidget(self._av_change_btn)
        v.addLayout(av_row)

        # 分组表单
        v.addWidget(_label("名称（id，小写英文/数字）", 10, muted=True))
        self._f_name = LineEdit()
        self._f_name.setPlaceholderText("例如：coder / muse")
        self._f_name.setStyleSheet(self._input_style())
        v.addWidget(self._f_name)
        v.addWidget(_label("副标题", 10, muted=True))
        self._f_desc = LineEdit()
        self._f_desc.setPlaceholderText("如：更懂工程的搭档")
        self._f_desc.setStyleSheet(self._input_style())
        v.addWidget(self._f_desc)
        v.addWidget(_label("思考块标签（留空无）", 10, muted=True))
        self._f_tag = LineEdit()
        self._f_tag.setPlaceholderText("如：推演 / MOOD")
        self._f_tag.setStyleSheet(self._input_style())
        v.addWidget(self._f_tag)
        v.addWidget(_label("人格底座模板（支持 {{userName}} / {{agentName}}）", 10, muted=True))
        self._f_prompt = QPlainTextEdit()
        self._f_prompt.setPlaceholderText("人格基底 prompt，注入 system prompt 的人格段…")
        self._f_prompt.setStyleSheet(self._edit_style())
        v.addWidget(self._f_prompt, 1)

        save_row = QHBoxLayout()
        save_row.addStretch()
        save_btn = _dialog_btn("保存", primary=True)
        save_btn.clicked.connect(self._on_save)
        save_row.addWidget(save_btn)
        v.addLayout(save_row)
        return page

    # ── 选头像页 ──

    def _build_avatar_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        from .avatar_picker import AvatarPicker

        def _saver(data: bytes, ext: str):
            if not self._editing_pid:
                return None
            return self._registry.set_avatar(self._editing_pid, data, ext)

        self._avatar_picker = AvatarPicker(assistant_id="", parent=page, upload_saver=_saver)
        v.addWidget(self._avatar_picker, 1)
        back_row = QHBoxLayout()
        back_row.addStretch()
        back_btn = _dialog_btn("返回编辑")
        back_btn.clicked.connect(self._on_avatar_back)
        back_row.addWidget(back_btn)
        v.addLayout(back_row)
        return page

    def _on_avatar_change(self) -> None:
        if not self._editing_pid:
            return
        ap = self._registry.avatar_path(self._editing_pid)
        p = self._registry.get(self._editing_pid)
        self._avatar_picker.set_assistant(
            aid="",
            color="#7C3AED",
            name=(p.name if p else "") or self._editing_pid,
            image_path=str(ap) if ap else "",
        )
        self._stack.setCurrentIndex(1)

    def _on_avatar_back(self) -> None:
        # 返回编辑时落盘当前选择：上传已在 saver 落盘；预置图此处补写；
        # 纯色（无图）视作放弃本次更换，不改已有头像
        sel = self._avatar_picker.get_selection()
        if self._editing_pid and sel.get("image_path"):
            try:
                data = Path(sel["image_path"]).read_bytes()
                ext = Path(sel["image_path"]).suffix.lstrip(".").lower() or "png"
                self._registry.set_avatar(self._editing_pid, data, ext)
            except Exception as e:
                logger.warning(f"[assistant_hub] 人格头像落盘失败 ({self._editing_pid}): {e}")
        self._sync_avatar_row(self._editing_pid)
        self._stack.setCurrentIndex(0)

    def _on_avatar_reset(self) -> None:
        if self._editing_pid:
            self._registry.clear_avatar(self._editing_pid)
            self._sync_avatar_row(self._editing_pid)

    def _sync_avatar_row(self, pid: str) -> None:
        p = self._registry.get(pid) if pid else None
        ap = self._registry.avatar_path(pid) if pid else None
        self._av_preview.set_text((p.name if p else "") or pid or "?")
        self._av_preview.set_image(str(ap) if ap else None)
        self._av_name.setText((p.name if p else "") or pid or "—")
        self._av_reset_btn.setEnabled(self._registry.has_avatar_override(pid) if pid else False)

    def _input_style(self) -> str:
        return f"""
            LineEdit {{
                background: {Colors.INPUT_BG_START}; border: 1px solid {Colors.INPUT_BORDER};
                color: {Colors.INPUT_TEXT}; border-radius: 6px; padding: 4px 8px;
                {get_font_family_css()} {font_size_css(12)}
            }}
        """

    def _list_style(self) -> str:
        return f"""
            QListWidget {{ background: transparent; border: none; {get_font_family_css()} {font_size_css(12)} }}
            QListWidget::item {{ padding: 4px 6px; border-radius: 6px; color: {Colors.TEXT_PRIMARY}; }}
            QListWidget::item:hover {{ background: {Colors.HOVER_BG}; }}
            QListWidget::item:selected {{ background: {Colors.SELECTED_BG}; }}
        """

    def _edit_style(self) -> str:
        return f"""
            QPlainTextEdit {{
                background: {Colors.CARD_BG.format(alpha=120)};
                border: 1px solid {Colors.BORDER}; border-radius: 8px;
                color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(11)}; padding: 6px;
            }}
            QPlainTextEdit QScrollBar:vertical {{
                background: transparent; width: 6px; margin: 0;
            }}
            QPlainTextEdit QScrollBar::handle:vertical {{
                background: {Colors.SCROLLBAR_HANDLE_BG}; border-radius: 3px; min-height: 30px;
            }}
            QPlainTextEdit QScrollBar::handle:vertical:hover {{
                background: {Colors.SCROLLBAR_HANDLE_HOVER_BG}; width: 8px;
            }}
            QPlainTextEdit QScrollBar::add-line:vertical,
            QPlainTextEdit QScrollBar::sub-line:vertical {{ height: 0; }}
            QPlainTextEdit QScrollBar::add-page:vertical,
            QPlainTextEdit QScrollBar::sub-page:vertical {{ background: none; }}
        """

    def _reload(self, keep_pid: str = "") -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for p in self._registry.list_all():
            if p.id == "none":
                continue
            ap = self._registry.avatar_path(p.id)
            icon = QIcon(str(ap)) if ap and ap.suffix.lower() in (".png", ".jpg", ".webp") else QIcon()
            item = QListWidgetItem(icon, f"{p.name}（{p.id}）" + (" · 内置" if p.builtin else ""))
            item.setData(Qt.UserRole, p.id)
            self._list.addItem(item)
            if p.id == keep_pid:
                self._list.setCurrentItem(item)
        self._list.blockSignals(False)
        if self._list.currentItem() is None and self._list.count():
            self._list.setCurrentRow(0)

    def _on_select(self, cur, _prev):
        if cur is None:
            return
        pid = cur.data(Qt.UserRole)
        p = self._registry.get(pid)
        if p is None:
            return
        self._editing_pid = pid
        self._f_name.setText(p.id)
        self._f_desc.setText(p.description)
        self._f_tag.setText(p.tag)
        self._f_prompt.setPlainText(p.prompt)
        builtin = p.builtin
        for w in (self._f_name, self._f_desc, self._f_tag, self._f_prompt):
            w.setReadOnly(builtin)
        self._del_btn.setEnabled(not builtin)
        self._sync_avatar_row(pid)

    def _on_new(self):
        self._editing_pid = ""
        self._f_name.clear()
        self._f_desc.clear()
        self._f_tag.clear()
        self._f_prompt.clear()
        for w in (self._f_name, self._f_desc, self._f_tag, self._f_prompt):
            w.setReadOnly(False)
        self._del_btn.setEnabled(False)
        self._f_name.setFocus()
        self._sync_avatar_row("")

    def _on_delete(self):
        cur = self._list.currentItem()
        if cur is None:
            return
        if not self._registry.delete(cur.data(Qt.UserRole)):
            return
        self._editing_pid = ""
        self._reload()

    def _on_save(self):
        name = self._f_name.text().strip()
        pid = name.lower()
        if not pid or pid == "none":
            return
        if not all(c.isalnum() or c in "-_" for c in pid):
            return
        existed = self._registry.get(pid)
        if existed is not None and existed.builtin:
            return
        if self._editing_pid and pid != self._editing_pid and existed is not None:
            return  # 改名撞已有 id
        Persona = _persona_cls()
        p = Persona(
            id=pid,
            name=name,
            description=self._f_desc.text().strip(),
            tag=self._f_tag.text().strip(),
            prompt=self._f_prompt.toPlainText(),
            builtin=False,
        )
        self._registry.upsert(p)
        self._editing_pid = pid
        self._reload(keep_pid=pid)
