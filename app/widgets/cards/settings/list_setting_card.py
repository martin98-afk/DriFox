# -*- coding: utf-8 -*-
from pathlib import Path
from typing import Dict, List

from PyQt5.QtCore import QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CardWidget,
    ConfigItem,
    ConfigValidator,
    ExpandSettingCard,
    StrongBodyLabel,
    qconfig,
)

from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css
from app.widgets.cards.settings.expand_height_mixin import DynamicHeightExpandCardMixin
from app.widgets.elided_label import _ElidedLabel

# 「技能」类卡片的分组/分批常量（与插件组件卡同一套节奏）
# 首帧只建分组头，技能行随后分批补齐，展开时不会卡住主线程
_SKILL_FIRST_BATCH = 3
_SKILL_BATCH_SIZE = 12
# 无插件归属的技能归入这两个兜底分组
_BUILTIN_GROUP = "内置技能"
_USER_GROUP = "用户技能"


class ListValidator(ConfigValidator):
    """Folder list validator"""

    def validate(self, value):
        return True

    def correct(self, value: List[str]):
        return value


class SkillItem(CardWidget):
    """Skill item with enable switch — 紧凑卡片风格，参考 MCPServerRow"""

    enabled_changed = pyqtSignal(str, bool)

    def __init__(self, name: str, description: str, is_enabled: bool, parent=None):
        super().__init__(parent=parent)
        self.name = name
        self._description = description
        self._setup_ui(name, description, is_enabled)

    def refresh_style(self):
        """主题变更时刷新描述文字颜色"""
        Colors.refresh()
        self._desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(12)}px;"
        )

    def _setup_ui(self, name: str, description: str, is_enabled: bool):
        from qfluentwidgets import SwitchButton

        from app.utils.design_tokens import SwitchStyles

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)

        # 技能名称
        name_label = StrongBodyLabel(name)
        name_label.setFixedWidth(150)
        layout.addWidget(name_label)

        # 描述（自动省略）
        self._desc_label = _ElidedLabel(description)
        self._desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(12)}px;"
        )
        self._desc_label.setMinimumWidth(40)
        layout.addWidget(self._desc_label, 1)

        # 开关
        self.switch = SwitchButton()
        SwitchStyles.configure(self.switch)
        self.switch.setChecked(is_enabled)
        self.switch.checkedChanged.connect(lambda v: self.enabled_changed.emit(self.name, v))
        layout.addWidget(self.switch)


class SkillGroupSection(QWidget):
    """技能分组小节 — 来源名（插件名或内置/用户）+ 系统/用户标签 + 技能行

    结构对齐「工具启用 / 智能体启用」卡的插件小节：左侧色条 + 名称 + 来源标签，
    下面是该来源下的技能行。行由外部逐批添加（见 SkillListSettingCard）。
    """

    _TAG_STYLE = "background-color: {color}; color: white; {font_size} {font_family} font-weight: bold; padding: 1px 6px; border-radius: 4px;"
    _NAME_STYLE = "color: {color}; background: transparent; border: none; font-weight: 600; {font_size} {font_family}"

    def __init__(self, group_name: str, is_system: bool, parent=None):
        super().__init__(parent)
        self.group_name = group_name
        self.is_system = is_system

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(0)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 6, 8, 2)
        header_layout.setSpacing(8)

        kind_color = "#e74c3c" if is_system else "#2ecc71"
        anchor = QWidget(header)
        anchor.setFixedSize(3, 14)
        anchor.setStyleSheet(f"background: {kind_color}; border: none; border-radius: 1px;")
        header_layout.addWidget(anchor)

        self._name_label = QLabel(group_name)
        self._name_label.setStyleSheet(
            self._NAME_STYLE.format(
                color=Colors.TEXT_PRIMARY,
                font_size=font_size_css(12),
                font_family=get_font_family_css(),
            )
        )
        header_layout.addWidget(self._name_label)

        kind_tag = QLabel("系统" if is_system else "用户")
        kind_tag.setStyleSheet(
            self._TAG_STYLE.format(
                color=kind_color,
                font_size=font_size_css(10),
                font_family=get_font_family_css(),
            )
        )
        header_layout.addWidget(kind_tag)
        header_layout.addStretch(1)
        outer.addWidget(header)

        # 技能行容器（外部调 add_item 逐批填充）
        self._body = QWidget(self)
        self._rows_layout = QVBoxLayout(self._body)
        self._rows_layout.setContentsMargins(0, 0, 0, 2)
        self._rows_layout.setSpacing(0)
        outer.addWidget(self._body)

    def add_item(self, name: str, description: str, is_enabled: bool) -> SkillItem:
        item = SkillItem(name, description, is_enabled, self._body)
        self._rows_layout.addWidget(item)
        item.show()
        return item

    def items(self) -> List[SkillItem]:
        return [w for w in self._body.findChildren(SkillItem)]

    def refresh_style(self):
        Colors.refresh()
        self._name_label.setStyleSheet(
            self._NAME_STYLE.format(
                color=Colors.TEXT_PRIMARY,
                font_size=font_size_css(12),
                font_family=get_font_family_css(),
            )
        )
        for item in self.items():
            item.refresh_style()


class SkillListSettingCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
    """Skill list setting card with enable/disable switches

    按来源（插件 / 内置 / 用户）分组展示，行在展开后分批构建——
    技能数量多时逐个 new SkillItem 是设置面板最大的单项开销。
    """

    skillsChanged = pyqtSignal(list)

    def __init__(
        self,
        icon: QIcon,
        configItem: ConfigItem,
        title: str,
        content: str = None,
        parent=None,
        home=None,
    ):
        self.home = home
        super().__init__(icon, title, content, parent)
        self.title = title
        self.configItem = configItem
        self.enabled_skills = qconfig.get(configItem).copy() if qconfig.get(configItem) else []
        # 技能项延迟到首次展开时构建（见 _ensure_items_built）。
        # all_skills 必须先初始化为空列表——外部在折叠态下也可能读它
        self.all_skills = []
        self._items_built = False
        self._discovered = False  # all_skills 是否已填充（可被外部预热）
        # ── 分批构建状态（按行分批，展开后先出分组骨架再填行）──
        self._pending_rows: List[tuple] = []  # [(section, skill_dict)]
        self._sections: List[SkillGroupSection] = []
        self._built = 0
        self._token = 0  # 重建令牌：作废过期的分批回调
        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.setInterval(0)
        self._batch_timer.timeout.connect(self._build_next_batch)
        # 开关写盘防抖：连续切换只落盘一次（见 _on_skill_enabled_changed）
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_config)
        self.__initWidget()

    def _discover_skills(self):
        """扫描技能目录并解析 SKILL.md（约 90ms，可被外部提前预热）

        预热方式：外部（设置面板 showEvent 的空闲帧）先调一次本方法，
        用户点开卡片时就不必再等扫盘——`_ensure_items_built` 会看到
        `self._discovered` 已为真而跳过重复扫描。
        """
        from pathlib import Path

        from app.utils.utils import get_app_data_dir

        self.all_skills = []
        seen_names = set()  # 按路径优先级去重，保留首次出现的同名技能

        # ---- Phase 1: PluginManager 路径（带插件上下文，最高优先级） ----
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for item in pm.get_skills_with_plugin():
                    skills_base = item["path"]
                    plugin_name = item["plugin_name"]
                    is_system = item["is_system"]
                    if not skills_base.exists():
                        continue
                    for skill_dir in skills_base.iterdir():
                        if not skill_dir.is_dir():
                            continue
                        if skill_dir.name.startswith("_") or skill_dir.name.startswith("."):
                            continue
                        entry = self._parse_skill_dir(skill_dir, plugin_name, is_system)
                        if entry and entry["name"] not in seen_names:
                            seen_names.add(entry["name"])
                            self.all_skills.append(entry)
        except Exception:
            pass

        # ---- Phase 2: 旧路径回退（无插件上下文，按路径判定系统/用户） ----
        skills_dirs = [
            (Path(__file__).parent.parent / "skills", True),  # 内置
            (Path.home() / ".agents" / "skills", False),  # 用户
            (get_app_data_dir() / "skills", False),  # 用户
        ]
        for skills_dir, is_system in skills_dirs:
            if not skills_dir.exists():
                continue
            for skill_dir in skills_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                if skill_dir.name.startswith("_") or skill_dir.name.startswith("."):
                    continue
                entry = self._parse_skill_dir(skill_dir, plugin_name=None, is_system=is_system)
                if entry and entry["name"] not in seen_names:
                    seen_names.add(entry["name"])
                    self.all_skills.append(entry)
        self._discovered = True

    def _build_groups(self) -> List[tuple]:
        """按来源分组：[(group_name, is_system, [skill...]), ...]

        有 plugin_name 的按插件分组；否则归入「内置技能」/「用户技能」。
        排序：系统优先、同名按字母序（与工具/智能体卡的小节顺序一致）。
        """
        groups: Dict[tuple, list] = {}
        for skill in self.all_skills:
            plugin_name = skill.get("plugin_name")
            is_system = bool(skill.get("is_system", True))
            if plugin_name:
                key = (plugin_name, is_system)
            else:
                key = (_BUILTIN_GROUP if is_system else _USER_GROUP, is_system)
            groups.setdefault(key, []).append(skill)

        ordered = sorted(groups.items(), key=lambda kv: (not kv[0][1], kv[0][0].lower()))
        return [(name, is_system, sorted(skills, key=lambda s: s["name"])) for (name, is_system), skills in ordered]

    @staticmethod
    def _parse_skill_dir(skill_dir: Path, plugin_name: str | None = None, is_system: bool = True) -> dict | None:
        """解析技能目录，返回技能信息字典（与 utils._parse_skill_dir 逻辑一致）"""
        import yaml

        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            skill_file = skill_dir / "skill.md"
        if not skill_file.exists():
            return None

        try:
            content = skill_file.read_text(encoding="utf-8")
            name = skill_dir.name
            description = ""

            if content.startswith("---"):
                try:
                    frontmatter = content.split("---", 2)[1]
                    meta = yaml.safe_load(frontmatter)
                    if meta:
                        name = meta.get("name", skill_dir.name)
                        description = meta.get("description", "")
                except Exception:
                    pass

            return {
                "name": name,
                "description": description,
                "plugin_name": plugin_name,
                "is_system": is_system,
            }
        except Exception:
            return None

    def __initWidget(self):
        self.viewLayout.setSpacing(0)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)

        header_widget = QWidget(self.view)
        header_widget.setStyleSheet("background-color: transparent;")
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(12, 6, 12, 6)

        self._header_title = QLabel("技能名称", header_widget)
        self._header_title.setFixedWidth(150)
        self._header_title.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: bold; {get_font_family_css()}"
        )

        self._header_desc = QLabel("描述", header_widget)
        self._header_desc.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: bold; {get_font_family_css()}"
        )

        self._header_state = QLabel("启用", header_widget)
        self._header_state.setFixedWidth(50)
        self._header_state.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: bold; {get_font_family_css()}"
        )
        self._header_state.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        header_layout.addWidget(self._header_title)
        header_layout.addWidget(self._header_desc, 1)
        header_layout.addWidget(self._header_state)

        self.viewLayout.addWidget(header_widget)

        # 分组与技能行延迟到首次展开时构建（见 _ensure_items_built）：
        # 逐个 new SkillItem 是设置面板最大的单项开销，而折叠态下这些
        # 行用户根本看不到。这里只留表头，subtitle 保持构造时传入的静态文案。
        self._adjust_view_size()

    def _clear_sections(self):
        """清空所有分组小节（重建 / 刷新时调用）"""
        for section in self._sections:
            section.setParent(None)
            section.deleteLater()
        self._sections.clear()
        self._pending_rows.clear()
        self._built = 0

    def _ensure_items_built(self, skip_discover: bool = False):
        """首次需要时搭起分组骨架并排队填充技能行

        分两步：先建全部分组头（很轻，用户立刻看到结构），再把技能行排进
        队列分批构建——一次建几十个 SkillItem 会明显卡住主线程。

        Args:
            skip_discover: 调用方已经跑过 _discover_skills() 时传 True，避免重复扫盘
        """
        if self._items_built:
            return
        self._items_built = True
        self._token += 1
        if not skip_discover and not self._discovered:
            self._discover_skills()

        for group_name, is_system, skills in self._build_groups():
            section = SkillGroupSection(group_name, is_system, self.view)
            self.viewLayout.addWidget(section)
            section.show()
            self._sections.append(section)
            for skill in skills:
                self._pending_rows.append((section, skill))

        self._update_skill_token_count()
        self._adjust_view_size()
        self._build_next_batch(first=True)

    def _build_next_batch(self, first: bool = False):
        """构建下一批技能行"""
        if self._built >= len(self._pending_rows):
            self._adjust_view_size()
            return
        # 折叠态不构建：内容不可见，展开时由 on_after_expand 续上
        if not self.isExpand:
            return

        batch = _SKILL_FIRST_BATCH if first else _SKILL_BATCH_SIZE
        target = min(self._built + batch, len(self._pending_rows))
        token = self._token
        for section, skill in self._pending_rows[self._built : target]:
            if token != self._token:
                return  # 期间又发生了重建，本批次作废
            item = section.add_item(skill["name"], skill["description"], skill["name"] in self.enabled_skills)
            item.enabled_changed.connect(self._on_skill_enabled_changed)
        self._built = target
        self._adjust_view_size()
        if self._built < len(self._pending_rows):
            self._batch_timer.start()

    def on_after_expand(self, is_expand: bool) -> None:
        """展开时搭起分组骨架并补齐未完成的技能行"""
        if not is_expand:
            return
        self._ensure_items_built()
        if self._built < len(self._pending_rows):
            self._batch_timer.start()

    def refresh_style(self):
        """主题变更时刷新表头文字颜色与所有分组"""
        Colors.refresh()
        self._header_title.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: bold; {get_font_family_css()}"
        )
        self._header_desc.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: bold; {get_font_family_css()}"
        )
        self._header_state.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: bold; {get_font_family_css()}"
        )
        # 刷新所有 SkillItem 的描述颜色
        for i in range(self.viewLayout.count()):
            w = self.viewLayout.itemAt(i).widget()
            if isinstance(w, SkillItem) and hasattr(w, "refresh_style"):
                w.refresh_style()

    def _sync_skill_states(self):
        """轻量同步：只更新 enabled_skills 和现有开关状态，不重建列表"""
        from qfluentwidgets import qconfig

        self.enabled_skills = qconfig.get(self.configItem).copy() if qconfig.get(self.configItem) else []
        # blockSignals：同步外部变更时 setChecked 会同步触发 checkedChanged →
        # enabled_changed → 又一轮 Settings.set + token 计算，多窗口下 N 倍放大
        for section in self._sections:
            for item in section.items():
                sw = item.switch
                sw.blockSignals(True)
                sw.setChecked(item.name in self.enabled_skills)
                sw.blockSignals(False)
        # 行还没建时 all_skills 是空的，此时更新计数会把 subtitle 写成 0/0
        if self._items_built:
            self._update_skill_token_count()

    def _refresh_skills(self):
        """重建列表：重读启用状态 → 重新发现 → 按分组重排

        折叠态下只刷新 all_skills 与计数，行留给展开时构建；
        已展开则立刻重建分组与行（分批）。
        """
        from qfluentwidgets import qconfig

        self.enabled_skills = qconfig.get(self.configItem).copy() if qconfig.get(self.configItem) else []
        self._items_built = False
        self._discovered = False
        self._clear_sections()
        self._discover_skills()
        self._update_skill_token_count()
        if self.isExpand:
            self._ensure_items_built(skip_discover=True)
        else:
            self._adjust_view_size()

    def _add_skill_item(self, name: str, description: str, adjust: bool = True) -> SkillItem:
        """兼容入口：直接往第一个分组追加一行

        正常路径走 `_build_next_batch`（按来源分组、分批构建），这里只给
        外部/旧调用方保留一个能用的追加接口。
        """
        if not self._sections:
            section = SkillGroupSection(_BUILTIN_GROUP, True, self.view)
            self.viewLayout.addWidget(section)
            section.show()
            self._sections.append(section)
        item = self._sections[0].add_item(name, description, name in self.enabled_skills)
        item.enabled_changed.connect(self._on_skill_enabled_changed)
        if adjust:
            self._adjust_view_size()
        return item

    def _update_skill_token_count(self):
        """更新头部 subtitle：已启用计数 + token 占用估算"""
        from app.core.token_estimator import estimate_tokens
        from app.utils.utils import get_local_skills

        enabled = self.enabled_skills or []
        # 计数依赖 all_skills。外部（多窗口同步）可能在折叠态直接调本方法，
        # 此时若已有启用项却没扫过盘，会把 subtitle 写成「N/0」——补一次发现。
        # 没有任何启用项时不扫，避免为了显示 0/0 付一次磁盘扫描。
        if enabled and not self._discovered:
            self._discover_skills()
        total = len(self.all_skills)

        if not enabled:
            self.setContent(f"0/{total} · ~0 tokens")
            return

        all_skills = get_local_skills()
        parts = [
            "\n\n## 偏好技能\n"
            "以下是部分用户偏好的智能体技能，如果以下技能不能满足用户需求，"
            "可以使用 `list_skills` 技能加载完整技能列表：\n"
        ]
        for skill in all_skills:
            if skill["name"] in enabled:
                display_name = skill.get("qualified_name", skill["name"])
                parts.append(f"\n### {display_name}\n{skill.get('description', '')}\n")

        content = "\n".join(parts) if len(parts) > 1 else ""
        count = estimate_tokens(content)
        self.setContent(f"{len(enabled)}/{total} · ~{count:,} tokens")

    def _save_config(self):
        """防抖落盘（只写文件，配置值已在 Settings.set 时更新）"""
        from app.utils.config import Settings

        Settings.get_instance().save()

    def _on_skill_enabled_changed(self, name: str, enabled: bool):
        if enabled and name not in self.enabled_skills:
            self.enabled_skills.append(name)
        elif not enabled and name in self.enabled_skills:
            self.enabled_skills.remove(name)

        # 值立即更新（valueChanged 同步广播驱动多窗口 UI 同步），落盘防抖：
        # 每次 save 都全量序列化写 config 文件，连续切换开关时只落最后一次
        from app.utils.config import Settings

        Settings.get_instance().set(self.configItem, self.enabled_skills, save=False)
        self._save_timer.start()
        self._update_skill_token_count()
        self.skillsChanged.emit(self.enabled_skills)

    def setContent(self, text: str):
        """更新卡片头部 subtitle（服务器计数 + token 占用）"""
        card = self.card
        if hasattr(card, "contentLabel"):
            card.contentLabel.setText(text)

    def _get_focus_item(self):
        """展开卡片时滚到第一个真正的 SkillItem（跳过表格 header 行）"""
        for section in self._sections:
            items = section.items()
            if items:
                return items[0]
        return None
