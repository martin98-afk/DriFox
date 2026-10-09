# -*- coding: utf-8 -*-
"""
服务商编辑卡片 - 将弹窗改为卡片形式（保留文字标签）
"""

import threading

import requests
from loguru import logger
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
)

from app.constants import (
    get_merged_provider_models,
    provider_default_config,
)
from app.plugins.registries.provider_registry import ProviderRegistry
from app.utils.design_tokens import Colors, font_size_css
from app.utils.provider_icons import get_provider_icon
from app.utils.provider_ui_meta import get_auth_type, get_preset_urls
from app.utils.utils import get_font_family_css
from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget
from app.widgets.model_list_edit_dialog import ModelListEditorWidget
from app.widgets.searchable_editable_combobox import SearchableEditableComboBox


def _is_text_chat_model(model_id: str) -> bool:
    """判断模型是否为文本聊天模型，过滤掉图片、音频、词嵌入等非文本模型"""
    if not model_id:
        return False
    model_lower = model_id.lower()
    non_text_keywords = [
        # 图片生成/视觉模型
        "dall-e",
        "dalle",
        "stable-diffusion",
        "sd-",
        "imagen",
        "flux",
        "image",
        "diffusion",
        "kandinsky",
        "midjourney",
        "wan",
        "vision",
        "vl",
        "llava",
        "seance",
        "cogview",
        "cogvideo",
        "pixart",
        "visual",
        # 音频模型
        "whisper",
        "tts",
        "speech",
        "audio",
        "piper",
        "voice",
        # 词嵌入模型
        "embedding",
        "embed",
        "text-embedding",
        "bge",
        # 其他非聊天模型
        "moderation",
        "rerank",
        "search",
        "retrieval",
    ]
    for keyword in non_text_keywords:
        if keyword in model_lower:
            return False
    return True


def fetch_provider_models(api_url: str, api_key: str, provider_name: str, auth_type: str = "bearer") -> tuple:
    """Fetch model list from provider API.

    Returns (text_chat_models, filtered_out_models)：后者为被关键词规则
    过滤掉的非对话模型，UI 侧会展示给用户、允许点击加回（防误杀）。
    """
    headers = {"Authorization": f"Bearer {api_key}"} if auth_type == "bearer" else {}

    urls_to_try = []
    if provider_name == "DeepSeek":
        urls_to_try = [f"{api_url.rstrip('/')}/models"]
    else:
        urls_to_try = [
            f"{api_url.rstrip('/')}/models",
            f"{api_url.rstrip('/')}/v1/models",
        ]

    last_error = ""
    for url in urls_to_try:
        try:
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()

                if isinstance(data, dict):
                    if "data" in data:
                        all_models = [
                            m.get("id") or m.get("name", "") or m.get("model", "")
                            for m in data["data"]
                            if isinstance(m, dict)
                        ]
                    elif "models" in data:
                        all_models = [
                            m.get("id") or m.get("name", "") or m.get("model", "")
                            for m in data["models"]
                            if isinstance(m, dict)
                        ]
                    else:
                        all_models = []
                elif isinstance(data, list):
                    all_models = data
                else:
                    all_models = []

                filtered = [m for m in all_models if m and _is_text_chat_model(m)]
                removed = [m for m in all_models if m and not _is_text_chat_model(m)]
                return filtered, removed

            last_error = f"HTTP {response.status_code}"
        except Exception as e:
            last_error = str(e)

    logger.warning(f"[ProviderEditCard] All attempts failed. Last error: {last_error}")
    return [], []


class ProviderEditCard(QWidget):
    """服务商编辑卡片 - 紧凑设计"""

    # 信号
    saved = pyqtSignal(str, dict)  # provider_name, provider_info
    closed = pyqtSignal()
    fetchSuccess = pyqtSignal(list)  # 获取成功信号
    fetchFailed = pyqtSignal(str)  # 获取失败信号（失败原因，空串=无内容）
    loginSuccess = pyqtSignal(str, str)  # 登录成功（api_key, info）
    loginFailed = pyqtSignal(str)  # 登录失败（原因）

    def __init__(self, provider_name: str = "", provider_info: dict = None, is_new: bool = True, parent=None):
        super().__init__(parent)
        self.provider_name = provider_name
        self.provider_info = (provider_info or {}).copy()
        self.is_new = is_new
        self._original_info = (provider_info or {}).copy()
        self._fetched_models = []
        self._filtered_out_models = []
        self._auto_config_name = ""
        # 收集 SearchableEditableComboBox 引用用于刷新
        self._searchable_combos = []
        self._init_ui()

        # 连接信号
        self.fetchSuccess.connect(self._on_fetch_success)
        self.fetchFailed.connect(self._on_fetch_failed)
        self.loginSuccess.connect(self._on_login_success)
        self.loginFailed.connect(self._on_login_failed)

    def _init_ui(self):
        self._apply_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 6, 4, 6)
        main_layout.setSpacing(6)

        # 连接配置区域
        # 服务商名称行
        current_provider = self.provider_name if not self.is_new else None
        template_url = ""
        if self.is_new:
            name_row = QHBoxLayout()
            # 服务商名称标签 - 固定宽度右对齐
            name_label = BodyLabel("服务商:")
            name_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            name_row.addWidget(name_label)
            self.nameCombo = SearchableEditableComboBox()
            self._searchable_combos.append(self.nameCombo)
            self.nameCombo.setMaxVisibleItems(10)
            for provider_name in ProviderRegistry.get_instance().names():
                icon = get_provider_icon(provider_name)
                self.nameCombo.addItem(provider_name, icon=icon)
            self.nameCombo.setDisabled(False)
            self.nameCombo.setCurrentIndex(0)
            self.nameCombo.currentTextChanged.connect(self._on_provider_changed)
            name_row.addWidget(self.nameCombo, 1)

            main_layout.addLayout(name_row)
            first_provider = self.nameCombo.currentText()
            template = provider_default_config(first_provider) or {}
            current_provider = first_provider
            template_url = template.get("API_URL", "")
        else:
            if provider_default_config(self.provider_name) is not None:
                template = provider_default_config(self.provider_name) or {}
            else:
                template = self.provider_info
            current_provider = self.provider_name
            template_url = template.get("API_URL", "")
            name_row = QHBoxLayout()
            name_row.addWidget(BodyLabel("服务商:"))
            name_row.addWidget(ProviderIconWidget(self.provider_name, 24))
            name_row.addWidget(BodyLabel(self.provider_name))
            name_row.addStretch(1)
            main_layout.addLayout(name_row)

        # 配置名称行（紧跟服务商名称行）
        config_name_row = QHBoxLayout()
        config_name_label = BodyLabel("配置名称:")
        config_name_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        config_name_row.addWidget(config_name_label)
        self.configNameEdit = LineEdit()
        # 如果是编辑模式，且 provider_info 中有 name 字段，则填充
        if not self.is_new and "name" in self.provider_info:
            self.configNameEdit.setText(self.provider_info["name"])
        else:
            # 新建时，默认使用服务商名称
            if self.is_new:
                self._auto_config_name = self.nameCombo.currentText()
                self.configNameEdit.setText(self._auto_config_name)
            else:
                self.configNameEdit.setText(self.provider_name)
        config_name_row.addWidget(self.configNameEdit, 1)
        main_layout.addLayout(config_name_row)

        # API URL 行
        url_row = QHBoxLayout()
        url_label = BodyLabel("API URL:")
        url_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        url_row.addWidget(url_label)
        self.apiUrlCombo = SearchableEditableComboBox()
        self._searchable_combos.append(self.apiUrlCombo)
        # 传入当前服务商名称和模板URL，加载预设URL列表
        self._load_preset_urls(provider_name=current_provider, template_url=template_url)
        current_url = self.provider_info.get("API_URL", template_url)
        if current_url:
            existing_items = [self.apiUrlCombo.itemText(i) for i in range(self.apiUrlCombo.count())]
            if current_url not in existing_items:
                self.apiUrlCombo.addItem(current_url)
            idx = self.apiUrlCombo.findText(current_url)
            if idx >= 0:
                self.apiUrlCombo.setCurrentIndex(idx)
            else:
                self.apiUrlCombo.setCurrentText(current_url)
        url_row.addWidget(self.apiUrlCombo, 1)
        main_layout.addLayout(url_row)

        # API Key 行
        key_row = QHBoxLayout()
        key_label = BodyLabel("API Key:")
        key_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        key_row.addWidget(key_label)
        self.apiKeyEdit = LineEdit()
        self.apiKeyEdit.setEchoMode(QLineEdit.Password)
        current_key = self.provider_info.get("API_KEY", template.get("API_KEY", ""))
        if current_key:
            self.apiKeyEdit.setText(current_key)
        key_row.addWidget(self.apiKeyEdit, 1)
        # 「获取 API KEY」（打开注册页）与「登录」（OAuth 类）互斥显示，同在 Key 行
        self.getKeyBtn = PrimaryPushButton("获取 API KEY")
        self.getKeyBtn.clicked.connect(self._open_register_url)
        key_row.addWidget(self.getKeyBtn)
        self.loginBtn = PrimaryPushButton("登录")
        self.loginBtn.clicked.connect(self._on_auto_login)
        self.loginBtn.setVisible(False)
        key_row.addWidget(self.loginBtn)
        main_layout.addLayout(key_row)

        # 获取按钮行
        self.fetchBtn = PrimaryPushButton("获取模型列表")
        self.fetchBtn.clicked.connect(self._on_fetch_models)

        model_row = QHBoxLayout()
        model_row.addWidget(BodyLabel("默认模型:"))
        self.modelCombo = SearchableEditableComboBox()
        self._searchable_combos.append(self.modelCombo)
        self.modelCombo.setMaxVisibleItems(10)
        self.modelCombo.setDisabled(False)
        current_model = self.provider_info.get("模型名称", template.get("模型名称", ""))
        saved_models = self.provider_info.get("模型列表", [])

        merged_provider_models = get_merged_provider_models()
        if self.is_new:
            selected_provider = self.nameCombo.currentText()
            if saved_models and isinstance(saved_models, list):
                self.modelCombo.addItems(saved_models)
            elif selected_provider in merged_provider_models:
                self.modelCombo.addItems(merged_provider_models[selected_provider])
            # 无匹配 → 保持空列表（不再硬编码回退到 DeepSeek：那是服务商名硬编码，
            # 且会把 DeepSeek 的模型塞给任意新服务商）
        else:
            has_saved_models = (
                "模型列表" in self.provider_info and isinstance(saved_models, list) and len(saved_models) > 0
            )
            if has_saved_models:
                self.modelCombo.addItems(saved_models)
            elif self.provider_name in merged_provider_models:
                self.modelCombo.addItems(merged_provider_models[self.provider_name])
            elif provider_default_config(self.provider_name) is not None:
                default_model = (provider_default_config(self.provider_name) or {}).get("模型名称", "")
                if default_model:
                    self.modelCombo.addItem(default_model)

        if current_model:
            existing = [self.modelCombo.itemText(i) for i in range(self.modelCombo.count())]
            if current_model not in existing:
                self.modelCombo.addItem(current_model)
            idx = self.modelCombo.findText(current_model)
            if idx >= 0:
                self.modelCombo.setCurrentIndex(idx)

        model_row.addWidget(self.modelCombo, 1)
        model_row.addWidget(self.fetchBtn)

        # 管理模型列表按钮
        self.manageModelsBtn = PrimaryPushButton("编辑列表")
        self.manageModelsBtn.clicked.connect(self._on_manage_models)
        model_row.addWidget(self.manageModelsBtn)

        main_layout.addLayout(model_row)

        # 模型列表内嵌编辑器（点击「编辑列表」展开/收起）
        self.modelListEditor = ModelListEditorWidget(parent=self)
        self.modelListEditor.setVisible(False)
        main_layout.addWidget(self.modelListEditor)

        # 套餐用量查询额外配置（可选）— 由 providers 插件声明（ProviderDef.extra_quota_fields）
        self._extra_config_section = QWidget()
        extra_layout = QVBoxLayout(self._extra_config_section)
        extra_layout.setContentsMargins(4, 2, 0, 4)
        extra_layout.setSpacing(6)

        # 小标题
        section_title = BodyLabel("套餐用量查询（可选）")
        extra_layout.addWidget(section_title)

        # (provider_name, config_key) -> row widget；字段定义全部来自插件
        self._extra_field_rows: dict = {}

        registry = ProviderRegistry.get_instance()
        for provider in registry.all():
            for field in provider.extra_quota_fields:
                config_key = field.key
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                lbl = BodyLabel(field.label)
                lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                row_layout.addWidget(lbl)
                editor = LineEdit()
                editor.setPlaceholderText(field.placeholder)
                existing = self.provider_info.get(config_key, "")
                if existing:
                    editor.setText(existing)
                edit_attr = f"quota_edit_{provider.name}_{config_key}"
                setattr(self, edit_attr, editor)
                row_layout.addWidget(editor, 1)
                extra_layout.addWidget(row_widget)
                self._extra_field_rows[(provider.name, config_key)] = (row_widget, edit_attr)

        main_layout.addWidget(self._extra_config_section)

        # 初始可见性由当前服务商决定
        self._update_extra_config_visibility()

        # 底部弹性空间：将所有内容推到上方
        main_layout.addStretch(1)

        # 保存按钮已移到 BaseSettingsCard 标题栏，信号由外部连接

        # 新建时调用一次初始化
        if self.is_new:
            self._on_provider_changed(self.nameCombo.currentText())
        else:
            # 编辑模式无 provider 切换，直接按当前服务商联动三按钮显隐
            self._sync_login_btn()

    def _apply_style(self):
        """应用主题感知的基础样式"""
        Colors.refresh()
        self.setStyleSheet(f"""
            QWidget {{
                background: transparent;
            }}
            QLineEdit {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                {get_font_family_css()}
                font-size: {font_size_css(12)};
            }}
            QLineEdit:focus {{
                border-color: {Colors.INPUT_FOCUS_BORDER};
            }}
        """)

    def refresh_style(self):
        """主题切换时刷新编辑卡片样式 + 子 SearchableEditableComboBox"""
        self._apply_style()
        for combo in self._searchable_combos:
            try:
                combo.refresh_style()
            except RuntimeError:
                pass
        editor = getattr(self, "modelListEditor", None)
        if editor is not None:
            try:
                editor.refresh_style()
            except RuntimeError:
                pass

    def _load_preset_urls(self, provider_name: str = None, template_url: str = ""):
        """加载预设的 API URL 端点

        候选来源单一化：全部走 app.utils.provider_ui_meta.get_preset_urls
        （插件 preset_urls 声明 → api_url → 默认配置兜底）。原先的 10 个
        elif 硬编码链已删除——它与插件声明双源且已漂移（火山插件声明
        api/coding/v3，硬编码只给 api/v3；阿里云/智谱各 2 个 URL 也只在一处）。
        """
        preset_urls = get_preset_urls(provider_name) if provider_name else []

        all_urls = list(dict.fromkeys(preset_urls + [template_url]))

        self.apiUrlCombo.blockSignals(True)
        self.apiUrlCombo.clear()
        self.apiUrlCombo.addItems(all_urls)
        self.apiUrlCombo.blockSignals(False)

    def _on_provider_changed(self, name: str):
        """服务商变化时更新预设值"""
        # 过滤缓存属于上一个服务商，切换后作废
        self._filtered_out_models = []
        if self.is_new and hasattr(self, "configNameEdit"):
            current_config_name = self.configNameEdit.text()
            if not current_config_name.strip() or current_config_name == self._auto_config_name:
                self.configNameEdit.setText(name)
                self._auto_config_name = name
        if provider_default_config(name) is not None:
            template = provider_default_config(name) or {}
            template_url = template.get("API_URL", "")
            self._load_preset_urls(name, template_url)

            preset_url = template.get("API_URL", "")
            existing_items = [self.apiUrlCombo.itemText(i) for i in range(self.apiUrlCombo.count())]
            if preset_url and preset_url in existing_items:
                idx = self.apiUrlCombo.findText(preset_url)
                self.apiUrlCombo.setCurrentIndex(idx)
            else:
                self.apiUrlCombo.setCurrentText(preset_url)

            merged_provider_models = get_merged_provider_models()
            self.modelCombo.blockSignals(True)
            self.modelCombo.clear()
            if name in merged_provider_models:
                self.modelCombo.addItems(merged_provider_models[name])
            default_model = template.get("模型名称", "")
            if default_model:
                self.modelCombo.addItem(default_model)
            if self.modelCombo.count() > 0:
                self.modelCombo.setCurrentIndex(0)
            self.modelCombo.blockSignals(False)
        # 编辑器可见时同步刷新，保持与下拉一致（防双源漂移）
        if self.modelListEditor.isVisible():
            self.modelListEditor.set_models(self.modelCombo.get_all_models())
            self.modelListEditor.set_filtered_models([])
        self._update_extra_config_visibility()

    def _update_extra_config_visibility(self):
        """根据当前服务商名称显示/隐藏套餐配置区（字段由插件声明）"""
        if not hasattr(self, "_extra_config_section"):
            return
        provider = self.nameCombo.currentText() if self.is_new else self.provider_name

        # 当前服务商的额外字段定义（providers 插件）
        current_fields = {}
        registry = ProviderRegistry.get_instance()
        p = registry.get(provider)
        if p is not None:
            current_fields = {f.key for f in p.extra_quota_fields}

        # 先全部隐藏
        for row_widget, _ in self._extra_field_rows.values():
            row_widget.setVisible(False)

        # 只显示当前服务商声明的字段行
        shown = 0
        for (pname, config_key), (row_widget, _) in self._extra_field_rows.items():
            if pname == provider and config_key in current_fields:
                row_widget.setVisible(True)
                shown += 1

        self._extra_config_section.setVisible(shown > 0)
        self._sync_login_btn()

    def _sync_login_btn(self):
        """按服务商 capabilities 联动三按钮：OAuth 类（有 login_hook）只显「登录」并
        隐藏「获取模型列表」（无 /models REST，用内置模型表）；普通类只显「获取 API KEY」"""
        if not hasattr(self, "loginBtn"):
            return
        provider = self.nameCombo.currentText() if self.is_new else self.provider_name
        p = ProviderRegistry.get_instance().get(provider)
        has_login = bool(p and p.capabilities.get("login_hook"))
        self.loginBtn.setVisible(has_login)
        self.getKeyBtn.setVisible(not has_login)
        # 有专属 models_hook 的 OAuth 类（如 CodeBuddy）仍可获取；纯内置表则隐藏。
        # fetchBtn 在 loginBtn 之后创建，构造期触发本函数时可能尚未存在
        fetch_btn = getattr(self, "fetchBtn", None)
        if fetch_btn is not None:
            has_models_hook = bool(p and p.capabilities.get("models_hook"))
            fetch_btn.setVisible(has_models_hook or not has_login)

    def _open_register_url(self):
        """打开当前服务商的注册/取 Key 页面"""
        name = self.nameCombo.currentText() if self.is_new else self.provider_name
        self._open_help_url(name)

    def _on_auto_login(self):
        """登录：后台执行 capabilities["login_hook"]，成功后回填 API Key 输入框"""
        provider = self.nameCombo.currentText() if self.is_new else self.provider_name
        p = ProviderRegistry.get_instance().get(provider)
        hook = p.capabilities.get("login_hook") if p else None
        if hook is None:
            return
        self.loginBtn.setEnabled(False)
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.info(
            "登录",
            "已打开浏览器等待授权，完成后自动回填（最长等待 5 分钟）",
            parent=parent,
            duration=6000,
            position=InfoBarPosition.BOTTOM,
        )
        threading.Thread(target=self._do_login_thread, args=(hook,), daemon=True).start()

    def _do_login_thread(self, hook):
        """后台执行 login_hook（可长阻塞：轮询浏览器授权结果）"""
        try:
            result = hook()
        except Exception as e:  # noqa: BLE001 —— 失败原因需透传到 UI
            self.loginFailed.emit(str(e))
            return
        if not result or not result.get("api_key"):
            self.loginFailed.emit("登录结果为空")
            return
        self.loginSuccess.emit(result["api_key"], result.get("info", ""))

    def _on_login_success(self, api_key: str, info: str):
        """登录成功（主线程）：回填 API Key 输入框，保存时走原生加密链"""
        self.loginBtn.setEnabled(True)
        self.apiKeyEdit.setText(api_key)
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.success(
            "登录成功",
            info or "API Key 已回填，请保存配置",
            parent=parent,
            duration=4000,
            position=InfoBarPosition.BOTTOM,
        )

    def _on_login_failed(self, reason: str):
        """登录失败（主线程）"""
        self.loginBtn.setEnabled(True)
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.error(
            "登录失败",
            reason[:120],
            parent=parent,
            duration=5000,
            position=InfoBarPosition.BOTTOM,
        )

    def _open_help_url(self, name: str):
        """打开帮助链接"""
        default_cfg = provider_default_config(name)
        if default_cfg:
            import webbrowser

            url = default_cfg.get("获取地址", "")
            if url:
                webbrowser.open(url)

    def _on_fetch_models(self):
        """获取模型列表"""
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        api_url = self.apiUrlCombo.currentText().strip()
        api_key = self.apiKeyEdit.text().strip()
        provider_name = self.nameCombo.currentText() if self.is_new else self.provider_name

        # capabilities["models_hook"]：服务商自定义获取（不依赖输入框 URL，
        # 但需要当前表单值——如 CodeBuddy 按 API_KEY 里的 refresh_token 换 token）
        p = ProviderRegistry.get_instance().get(provider_name)
        hook = p.capabilities.get("models_hook") if p else None
        if hook is not None:
            self.fetchBtn.setEnabled(False)
            InfoBar.info("获取中", "正在获取模型列表...", parent=parent, duration=3000, position=InfoBarPosition.BOTTOM)
            hook_config = {
                "API_URL": api_url,
                "API_KEY": api_key,
                "模型名称": self.modelCombo.currentText().strip(),
                # 认证方式必须问插件声明（百度千帆是 bce），写死 bearer 会让签名走错分支
                "认证方式": get_auth_type(provider_name),
            }
            threading.Thread(target=self._do_fetch_thread, args=(hook, hook_config), daemon=True).start()
            return

        if not api_url or not api_key:
            InfoBar.warning(
                "提示", "请先填写 API URL 和 Key", parent=parent, duration=2000, position=InfoBarPosition.BOTTOM
            )
            return

        self.fetchBtn.setEnabled(False)
        InfoBar.info("获取中", "正在获取模型列表...", parent=parent, duration=3000, position=InfoBarPosition.BOTTOM)

        def do_fetch():
            return fetch_provider_models(api_url, api_key, provider_name)

        thread = threading.Thread(target=self._do_fetch_thread, args=(do_fetch,))
        thread.daemon = True
        thread.start()

    def _do_fetch_thread(self, fetch_func, *args):
        """在后台线程中获取模型。

        异常必须转成 fetchFailed 信号：线程内异常若逃逸会静默杀死线程，
        fetchBtn 停在禁用态且无任何提示（表现为「一直卡住」）。
        """
        import time

        time.sleep(0.1)
        try:
            result = fetch_func(*args)
        except Exception as e:  # noqa: BLE001 —— 失败原因需透传到 UI
            logger.warning(f"[ProviderEditCard] 获取模型列表失败: {e}")
            self.fetchFailed.emit(str(e))
            return
        # 内置 fetch 返回 (models, filtered_out) 元组；插件 models_hook 返回纯列表
        if isinstance(result, tuple):
            models, removed = (list(result[0]), list(result[1]) if len(result) > 1 else [])
        else:
            models, removed = list(result), []
        self._fetched_models = models
        self._filtered_out_models = removed
        if models:
            self.fetchSuccess.emit(models)
        else:
            self.fetchFailed.emit("")

    def _on_fetch_success(self, models: list):
        """获取成功（主线程）。

        现有列表非空时不再静默覆盖（会吞掉手工编辑结果），弹三选：
        合并 / 替换 / 取消；列表为空（首次获取）时直接填入。
        """
        self.fetchBtn.setEnabled(True)
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        existing = self.modelCombo.get_all_models()
        if not existing:
            self._apply_fetched(models, mode="replace")
            return
        from app.widgets.common_dialogs import ChoiceDialog

        dialog = ChoiceDialog(
            title="获取成功",
            content=f"获取到 {len(models)} 个模型，现有列表 {len(existing)} 个，如何处理？",
            options=[("merge", "合并"), ("replace", "替换")],
            parent=parent,
        )
        dialog.chosen.connect(lambda key: self._apply_fetched(models, mode=key))
        dialog.exec_()

    def _apply_fetched(self, models: list, mode: str):
        """把获取结果写入模型下拉与内嵌编辑器。

        合并 = 现有列表在前、新模型去重追加；替换 = 直接覆盖。
        默认模型若不在结果中：提示并切到第一项，不再静默改值。
        """
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        if mode == "merge":
            existing = self.modelCombo.get_all_models()
            new_list = existing + [m for m in models if m not in existing]
            added = len(new_list) - len(existing)
        else:
            new_list = list(models)
            added = len(new_list)

        self.modelCombo.blockSignals(True)
        current = self.modelCombo.currentText()
        self.modelCombo.clear()
        self.modelCombo.addItems(new_list)
        if current and self.modelCombo.findText(current) >= 0:
            self.modelCombo.setCurrentIndex(self.modelCombo.findText(current))
        elif current and self.modelCombo.count() > 0:
            self.modelCombo.setCurrentIndex(0)
            InfoBar.warning(
                "默认模型已失效",
                f"「{current}」不在获取结果中，已切换为「{self.modelCombo.currentText()}」",
                parent=parent,
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )
        self.modelCombo.blockSignals(False)

        # 内嵌编辑器可见时同步刷新，避免收起时旧数据把获取结果写回吞掉
        if self.modelListEditor.isVisible():
            self.modelListEditor.set_models(new_list)
            self.modelListEditor.set_filtered_models(self._filtered_out_models)

        if mode == "merge":
            InfoBar.success(
                "已合并",
                f"新增 {added} 个，保留原有 {len(new_list) - added} 个",
                parent=parent,
                duration=2500,
                position=InfoBarPosition.BOTTOM,
            )
        else:
            InfoBar.success(
                "已替换",
                f"获取到 {len(new_list)} 个模型",
                parent=parent,
                duration=2000,
                position=InfoBarPosition.BOTTOM,
            )

    def _on_fetch_failed(self, reason: str = ""):
        """获取失败（主线程）；reason 为插件抛出的原因，空串走通用提示"""
        self.fetchBtn.setEnabled(True)
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.error(
            "失败",
            reason[:150] if reason else "获取模型列表失败，请检查配置",
            parent=parent,
            duration=5000,
            position=InfoBarPosition.BOTTOM,
        )

    def _on_manage_models(self):
        """展开/收起内嵌模型列表编辑器；收起时把编辑结果写回模型下拉"""
        if self.modelListEditor.isVisible():
            # 编辑器里加回的被过滤项同步回卡片缓存，下次展开不再展示
            self._filtered_out_models = self.modelListEditor.get_filtered_models()
            self._sync_editor_to_combo()
            self.modelListEditor.setVisible(False)
            self.manageModelsBtn.setText("编辑列表")
        else:
            self.modelListEditor.set_models(self.modelCombo.get_all_models())
            self.modelListEditor.set_filtered_models(self._filtered_out_models)
            self.modelListEditor.setVisible(True)
            self.manageModelsBtn.setText("收起列表")

    def _sync_editor_to_combo(self):
        """把内嵌编辑器中的列表写回模型下拉"""
        from qfluentwidgets import InfoBar
        from app.widgets.tab_manager_window import TabManagerWindow

        new_models = self.modelListEditor.get_models()
        self.modelCombo.blockSignals(True)
        current = self.modelCombo.currentText()
        self.modelCombo.clear()
        self.modelCombo.addItems(new_models)
        if current and self.modelCombo.findText(current) >= 0:
            self.modelCombo.setCurrentIndex(self.modelCombo.findText(current))
        elif current and self.modelCombo.count() > 0:
            # 默认模型被用户从列表删除：提示后再切换，不再静默改值
            self.modelCombo.setCurrentIndex(0)
            parent = TabManagerWindow.get_instance() or self.window()
            InfoBar.warning(
                "默认模型已失效",
                f"「{current}」已从列表移除，已切换为「{self.modelCombo.currentText()}」",
                parent=parent,
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )
        self.modelCombo.blockSignals(False)

    def _on_save(self):
        """保存。

        不再手工保留 config_id——config_id 现在由 main_widget 端基于 apikey
        的稳定 hash 计算（见 app.core.modelmeta.provider_profile.apply_provider_save），
        编辑同 apikey 始终命中同一条目，不会再产生重复。

        取值 / 判空 / 组字典 三段已抽到 provider_save_plan（纯函数可单测），
        本方法只负责 UI 交互（确认框）与发信号。
        """
        from app.core.modelmeta.provider_save_plan import build_provider_save_plan, collect_extra_fields

        if self.modelListEditor.isVisible():
            self._filtered_out_models = self.modelListEditor.get_filtered_models()
            self._sync_editor_to_combo()
        provider_name = self.nameCombo.currentText() if self.is_new else self.provider_name
        current_models = self.modelCombo.get_all_models()

        # 套餐用量额外字段：显式管理键恒写入（含空串），详见 collect_extra_fields
        extra_fields = collect_extra_fields(
            provider_name,
            self._extra_field_rows.items(),
            lambda attr: getattr(self, attr, None),
        )

        plan = build_provider_save_plan(
            form_values={
                "api_url": self.apiUrlCombo.currentText(),
                "api_key": self.apiKeyEdit.text(),
                "model": self.modelCombo.currentText(),
                # 认证方式问插件声明（bce/none/anthropic…）；未注册的服务商回落到
                # 存档值，再兜底 bearer——写死 bearer 会覆盖百度千帆的 bce 签名
                "auth_type": get_auth_type(provider_name, self.provider_info.get("认证方式", "")),
                "name": self.configNameEdit.text(),
                "models": current_models,
            },
            old_info=self.provider_info,
            extra_fields=extra_fields,
        )

        if plan["confirm_clear"]:
            # 用户清空了列表但旧列表非空：确认后才允许存空（旧逻辑会静默恢复旧数据）
            from app.widgets.common_dialogs import ConfirmDialog
            from app.widgets.tab_manager_window import TabManagerWindow

            parent = TabManagerWindow.get_instance() or self.window()
            dialog = ConfirmDialog(
                title="清空模型列表",
                content="模型列表已清空。保存后该服务商将没有可选模型，确定继续？",
                confirm_text="清空并保存",
                cancel_text="返回修改",
                parent=parent,
            )
            confirmed = {}
            dialog.confirmed.connect(lambda: confirmed.update(yes=True))
            dialog.exec_()
            if not confirmed.get("yes"):
                return

        self.provider_info = plan["payload"]
        self.saved.emit(provider_name, self.provider_info)

    def _on_cancel(self):
        """取消"""
        self.closed.emit()
