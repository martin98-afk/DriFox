# -*- coding: utf-8 -*-
from typing import List
from pathlib import Path

from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
)
from qfluentwidgets import (
    CardWidget,
    qconfig,
    ExpandSettingCard,
    ConfigItem,
    ConfigValidator,
    StrongBodyLabel,
)
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css


class ListValidator(ConfigValidator):
    """Folder list validator"""

    def validate(self, value):
        return True

    def correct(self, value: List[str]):
        return value


class _ElidedLabel(QLabel):
    """自动根据可用宽度省略文本的 QLabel（中间省略）"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = text
        # 防止布局根据文本内容自动扩展宽度，确保宽度由父布局决定
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str):
        self._full_text = text
        self._update_elided()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self):
        w = self.width()
        if w <= 0:
            # 还没布局完成（width=0），先显示完整文本，等 resizeEvent 时再省略
            if self.text() != self._full_text:
                super().setText(self._full_text)
            return
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full_text, Qt.ElideMiddle, w)
        # 只在文本变化时更新，避免触发不必要的 updateGeometry → 布局重算
        if self.text() != elided:
            super().setText(elided)


class SkillItem(CardWidget):
    """Skill item with enable switch — 紧凑卡片风格，参考 MCPServerRow"""

    enabled_changed = pyqtSignal(str, bool)

    def __init__(self, name: str, description: str, is_enabled: bool, parent=None):
        super().__init__(parent=parent)
        self.name = name
        self._setup_ui(name, description, is_enabled)

    def _setup_ui(self, name: str, description: str, is_enabled: bool):
        from qfluentwidgets import SwitchButton
        from app.utils.design_tokens import SwitchStyles

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)

        # 技能名称
        name_label = StrongBodyLabel(name)
        name_label.setFixedWidth(120)
        layout.addWidget(name_label)

        # 描述（自动省略）
        desc_label = _ElidedLabel(description)
        desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(12)}px;"
        )
        desc_label.setMinimumWidth(40)
        layout.addWidget(desc_label, 1)

        # 开关
        self.switch = SwitchButton()
        SwitchStyles.configure(self.switch)
        self.switch.setChecked(is_enabled)
        self.switch.checkedChanged.connect(
            lambda v: self.enabled_changed.emit(self.name, v)
        )
        layout.addWidget(self.switch)


class SkillListSettingCard(ExpandSettingCard):
    """Skill list setting card with enable/disable switches"""

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
        self.enabled_skills = (
            qconfig.get(configItem).copy() if qconfig.get(configItem) else []
        )
        self._discover_skills()
        self.__initWidget()

    def _discover_skills(self):
        from pathlib import Path
        import yaml

        from app.utils.utils import get_app_data_dir

        self.all_skills = []
        seen_names = set()  # 按路径优先级去重，保留首次出现的同名技能

        # ---- Phase 1: PluginManager 路径（带插件上下文，最高优先级） ----
        try:
            from app.core.plugin_manager import PluginManager
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

        # ---- Phase 2: 旧路径回退（无插件上下文） ----
        skills_dirs = [
            Path(__file__).parent.parent / "skills",
            Path.home() / ".agents" / "skills",
            get_app_data_dir() / "skills",
        ]
        for skills_dir in skills_dirs:
            if not skills_dir.exists():
                continue
            for skill_dir in skills_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                if skill_dir.name.startswith("_") or skill_dir.name.startswith("."):
                    continue
                entry = self._parse_skill_dir(skill_dir, plugin_name=None, is_system=True)
                if entry and entry["name"] not in seen_names:
                    seen_names.add(entry["name"])
                    self.all_skills.append(entry)

    @staticmethod
    def _parse_skill_dir(skill_dir: Path, plugin_name: str | None = None,
                         is_system: bool = True) -> dict | None:
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

            return {"name": name, "description": description, "plugin_name": plugin_name}
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

        header_title = QLabel("技能名称", header_widget)
        header_title.setFixedWidth(120)
        header_title.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: bold; {get_font_family_css()}"
        )

        header_desc = QLabel("描述", header_widget)
        header_desc.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: bold; {get_font_family_css()}")

        header_state = QLabel("启用", header_widget)
        header_state.setFixedWidth(50)
        header_state.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: bold; {get_font_family_css()}"
        )
        header_state.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        header_layout.addWidget(header_title)
        header_layout.addWidget(header_desc, 1)
        header_layout.addWidget(header_state)

        self.viewLayout.addWidget(header_widget)

        for skill in self.all_skills:
            self._add_skill_item(skill["name"], skill["description"])

        self._update_skill_token_count()
        self._adjustViewSize()

    def _sync_skill_states(self):
        """轻量同步：只更新 enabled_skills 和现有开关状态，不重建列表"""
        from qfluentwidgets import qconfig
        self.enabled_skills = qconfig.get(self.configItem).copy() if qconfig.get(self.configItem) else []
        for i in range(self.viewLayout.count()):
            w = self.viewLayout.itemAt(i).widget()
            if isinstance(w, SkillItem):
                w.switch.setChecked(w.name in self.enabled_skills)
        self._update_skill_token_count()

    def _refresh_skills(self):
        # 从配置重新读取启用状态（跨窗口同步需要）
        from qfluentwidgets import qconfig
        self.enabled_skills = qconfig.get(self.configItem).copy() if qconfig.get(self.configItem) else []
        self._discover_skills()
        # 从后往前遍历，只移除 SkillItem 类型的 widgets
        for i in reversed(range(self.viewLayout.count())):
            item = self.viewLayout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, SkillItem):
                self.viewLayout.removeItem(item)
                widget.deleteLater()
        for skill in self.all_skills:
            self._add_skill_item(skill["name"], skill["description"])
        self._update_skill_token_count()
        self._adjustViewSize()

    def _add_skill_item(self, name: str, description: str):
        is_enabled = name in self.enabled_skills
        item = SkillItem(name, description, is_enabled, self.view)
        item.enabled_changed.connect(self._on_skill_enabled_changed)
        self.viewLayout.addWidget(item)
        item.show()
        self._adjustViewSize()

    def _update_skill_token_count(self):
        """更新头部 subtitle：已启用计数 + token 占用估算"""
        from app.core.token_estimator import estimate_tokens
        from app.utils.utils import get_local_skills

        enabled = self.enabled_skills or []
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

    def _on_skill_enabled_changed(self, name: str, enabled: bool):
        if enabled and name not in self.enabled_skills:
            self.enabled_skills.append(name)
        elif not enabled and name in self.enabled_skills:
            self.enabled_skills.remove(name)

        # 使用 Settings.set() 而非 qconfig.set()，确保持久化到正确的配置文件
        from app.utils.config import Settings
        Settings.get_instance().set(self.configItem, self.enabled_skills, save=True)
        self._update_skill_token_count()
        self.skillsChanged.emit(self.enabled_skills)

    def setContent(self, text: str):
        """更新卡片头部 subtitle（服务器计数 + token 占用）"""
        card = self.card
        if hasattr(card, 'contentLabel'):
            card.contentLabel.setText(text)

