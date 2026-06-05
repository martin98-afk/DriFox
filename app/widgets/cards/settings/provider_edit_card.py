# -*- coding: utf-8 -*-
"""
服务商编辑卡片 - 将弹窗改为卡片形式（保留文字标签）
"""
import threading

from loguru import logger
import requests
from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLineEdit,
)
from qfluentwidgets import (
    BodyLabel,
    InfoBarPosition,
)
from qfluentwidgets import (
    LineEdit,
    PrimaryPushButton,
)

from app.constants import (
    PROVIDER_ICONS,
    PROVIDER_MODELS,
    FREE_PROVIDERS,
)
from app.utils.utils import get_icon, get_font_family_css
from app.utils.design_tokens import Colors, font_size_css
from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget
from app.widgets.searchable_editable_combobox import SearchableEditableComboBox
from app.widgets.model_list_edit_dialog import ModelListEditDialog


def _is_text_chat_model(model_id: str) -> bool:
    """判断模型是否为文本聊天模型，过滤掉图片、音频、词嵌入等非文本模型"""
    if not model_id:
        return False
    model_lower = model_id.lower()
    non_text_keywords = [
        # 图片生成/视觉模型
        'dall-e', 'dalle', 'stable-diffusion', 'sd-', 'imagen', 'flux',
        'image', 'diffusion', 'kandinsky', 'midjourney', 'wan', 'vision',
        'vl', 'llava', 'seance', 'cogview', 'cogvideo', 'pixart', 'visual',
        # 音频模型
        'whisper', 'tts', 'speech', 'audio', 'piper', 'voice',
        # 词嵌入模型
        'embedding', 'embed', 'text-embedding', 'bge',
        # 其他非聊天模型
        'moderation', 'rerank', 'search', 'retrieval',
    ]
    for keyword in non_text_keywords:
        if keyword in model_lower:
            return False
    return True


def fetch_provider_models(api_url: str, api_key: str, provider_name: str, auth_type: str = "bearer") -> list:
    """Fetch model list from provider API. Returns text chat models only."""
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
                return filtered

            last_error = f"HTTP {response.status_code}"
        except Exception as e:
            last_error = str(e)

    logger.warning(f"[ProviderEditCard] All attempts failed. Last error: {last_error}")
    return []


class ProviderEditCard(QWidget):
    """服务商编辑卡片 - 紧凑设计"""

    # 信号
    saved = pyqtSignal(str, dict)  # provider_name, provider_info
    closed = pyqtSignal()
    fetchSuccess = pyqtSignal(list)  # 获取成功信号
    fetchFailed = pyqtSignal()  # 获取失败信号

    def __init__(self, provider_name: str = "", provider_info: dict = None, is_new: bool = True, parent=None):
        super().__init__(parent)
        self.provider_name = provider_name
        self.provider_info = (provider_info or {}).copy()
        self.is_new = is_new
        self._original_info = (provider_info or {}).copy()
        self._fetched_models = []
        self._init_ui()

        # 连接信号
        self.fetchSuccess.connect(self._on_fetch_success)
        self.fetchFailed.connect(self._on_fetch_failed)

    def _init_ui(self):
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
            self.nameCombo.setMaxVisibleItems(10)
            for provider_name in FREE_PROVIDERS.keys():
                icon_name = PROVIDER_ICONS.get(provider_name, "大模型")
                icon = get_icon(icon_name)
                self.nameCombo.addItem(provider_name, icon=icon)
            self.nameCombo.setDisabled(False)
            self.nameCombo.setCurrentIndex(0)
            self.nameCombo.currentTextChanged.connect(self._on_provider_changed)
            name_row.addWidget(self.nameCombo, 1)

            self.getKeyBtn = PrimaryPushButton("获取 API KEY")
            self.getKeyBtn.clicked.connect(lambda: self._open_help_url(self.nameCombo.currentText()))
            name_row.addWidget(self.getKeyBtn)

            main_layout.addLayout(name_row)
            first_provider = self.nameCombo.currentText()
            template = FREE_PROVIDERS.get(first_provider, {})
            current_provider = first_provider
            template_url = template.get("API_URL", "")
        else:
            if self.provider_name in FREE_PROVIDERS:
                template = FREE_PROVIDERS[self.provider_name]
            else:
                template = self.provider_info
            current_provider = self.provider_name
            template_url = template.get("API_URL", "")
            name_row = QHBoxLayout()
            name_row.addWidget(BodyLabel("服务商:"))
            name_row.addWidget(ProviderIconWidget(self.provider_name, 24))
            name_row.addWidget(BodyLabel(self.provider_name))
            name_row.addStretch(1)
            getKeyBtn = PrimaryPushButton("获取 API KEY")
            getKeyBtn.clicked.connect(lambda: self._open_help_url(self.provider_name))
            name_row.addWidget(getKeyBtn)
            main_layout.addLayout(name_row)

        # API URL 行
        url_row = QHBoxLayout()
        url_label = BodyLabel("API URL:")
        url_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        url_row.addWidget(url_label)
        self.apiUrlCombo = SearchableEditableComboBox()
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
        main_layout.addLayout(key_row)

        # 获取按钮行
        self.fetchBtn = PrimaryPushButton("获取模型列表")
        self.fetchBtn.clicked.connect(self._on_fetch_models)

        model_row = QHBoxLayout()
        model_row.addWidget(BodyLabel("默认模型:"))
        self.modelCombo = SearchableEditableComboBox()
        self.modelCombo.setMaxVisibleItems(10)
        self.modelCombo.setDisabled(False)
        current_model = self.provider_info.get("模型名称", template.get("模型名称", ""))
        saved_models = self.provider_info.get("模型列表", [])

        if self.is_new:
            selected_provider = self.nameCombo.currentText()
            if saved_models and isinstance(saved_models, list):
                self.modelCombo.addItems(saved_models)
            elif selected_provider in PROVIDER_MODELS:
                self.modelCombo.addItems(PROVIDER_MODELS[selected_provider])
            elif "DeepSeek" in PROVIDER_MODELS:
                self.modelCombo.addItems(PROVIDER_MODELS["DeepSeek"])
        else:
            has_saved_models = "模型列表" in self.provider_info and isinstance(saved_models, list) and len(saved_models) > 0
            if has_saved_models:
                self.modelCombo.addItems(saved_models)
            elif self.provider_name in PROVIDER_MODELS:
                self.modelCombo.addItems(PROVIDER_MODELS[self.provider_name])
            elif self.provider_name in FREE_PROVIDERS:
                default_model = FREE_PROVIDERS[self.provider_name].get("模型名称", "")
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

        # 配置名称行
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
                self.configNameEdit.setText(self.nameCombo.currentText())
            else:
                self.configNameEdit.setText(self.provider_name)
        config_name_row.addWidget(self.configNameEdit, 1)
        main_layout.addLayout(config_name_row)

        # ── 套餐用量查询额外配置（可选） ────────────────
        self._extra_config_section = QWidget()
        extra_layout = QVBoxLayout(self._extra_config_section)
        extra_layout.setContentsMargins(4, 2, 0, 4)
        extra_layout.setSpacing(6)

        # 小标题
        section_title = BodyLabel("套餐用量查询（可选）")
        extra_layout.addWidget(section_title)

        # 所有可能的额外字段定义（按服务商显示不同组合）
        # 注意：不同组如果共享同一配置 key（如 "cookie"），内部 key 需加前缀避免冲突
        self._extra_field_defs = {
            "opencode": [
                ("opencode_server_id", "Server ID:", "opencode.ai/_server 请求中的 X-Server-Id"),
                ("opencode_cookie", "Cookie:", "oc_locale=zh; auth=Fe26.2**... （从浏览器复制完整的 Cookie 值）"),
                ("opencode_workspace_id", "Workspace ID:", "wrk_xxxxxxxxxxxx （无需可留空）"),
            ],
            "火山方舟": [
                ("volc_cookie", "Cookie:", "console.volcengine.com 浏览器 Cookie（完整值）"),
                ("volc_csrf_token", "CSRF Token:", "x-csrf-token（从请求头复制）"),
                ("volc_x_web_id", "X-Web-ID:", "x-web-id（可选）"),
            ],
        }

        # 内部 key → 实际存储的配置 key 映射
        self._extra_key_map = {
            "opencode_server_id": "server_id",
            "opencode_cookie": "cookie",
            "opencode_workspace_id": "workspace_id",
            "volc_cookie": "cookie",
            "volc_csrf_token": "csrf_token",
            "volc_x_web_id": "x_web_id",
        }

        # 记录每个字段属于哪个组，以及对应的行 widget
        self._extra_field_rows: dict = {}  # internal_key -> QWidget (row container)
        self._field_to_group: dict = {}    # internal_key -> group name

        for group_name, fields in self._extra_field_defs.items():
            for internal_key, label, placeholder in fields:
                config_key = self._extra_key_map.get(internal_key, internal_key)
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                lbl = BodyLabel(label)
                lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                row_layout.addWidget(lbl)
                editor = LineEdit()
                editor.setPlaceholderText(placeholder)
                existing = self.provider_info.get(config_key, "")
                if existing:
                    editor.setText(existing)
                setattr(self, f"{internal_key}_edit", editor)
                row_layout.addWidget(editor, 1)
                extra_layout.addWidget(row_widget)
                self._extra_field_rows[internal_key] = row_widget
                self._field_to_group[internal_key] = group_name

        main_layout.addWidget(self._extra_config_section)

        # 初始可见性由当前服务商决定
        self._update_extra_config_visibility()

        # 保存按钮已移到 BaseSettingsCard 标题栏，信号由外部连接

        # 新建时调用一次初始化
        if self.is_new:
            self._on_provider_changed(self.nameCombo.currentText())

    def _load_preset_urls(self, provider_name: str = None, template_url: str = ""):
        """加载预设的 API URL 端点"""
        preset_urls = []

        if provider_name:
            if provider_name == "DeepSeek":
                preset_urls = [
                    "https://api.deepseek.com",
                    "https://api.deepseek.com/chat/completions",
                ]
            elif provider_name == "SiliconFlow (硅基流动)":
                preset_urls = [
                    "https://api.siliconflow.cn/v1",
                    "https://api.siliconflow.cn/v1/chat/completions",
                ]
            elif provider_name == "MiniMax" or provider_name == "MiniMax (月之暗面)":
                preset_urls = [
                    "https://api.minimax.chat/v1",
                    "https://api.minimax.chat/v1/chat/completions",
                ]
            elif provider_name == "阿里云 (DashScope)":
                preset_urls = [
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "https://dashscope.aliyuncs.com/api/v1",
                ]
            elif provider_name == "智谱AI":
                preset_urls = [
                    "https://open.bigmodel.cn/api/paas/v4",
                    "https://open.bigmodel.cn/api/coding/paas/v4",
                ]
            elif provider_name == "百度千帆":
                preset_urls = [
                    "https://qianfan.baidubce.com/v2",
                    "https://qianfan.baidubce.com/v2/chat/completions",
                ]
            elif provider_name == "OpenAI":
                preset_urls = [
                    "https://api.openai.com/v1",
                    "https://api.openai.com/v1/chat/completions",
                ]
            elif provider_name == "火山方舟":
                preset_urls = [
                    "https://ark.cn-beijing.volces.com/api/v3",
                ]
            elif provider_name == "OpenCode Zen":
                preset_urls = [
                    "https://opencode.ai/zen/v1",
                    "https://opencode.ai/zen/go/v1",
                ]
            elif provider_name in FREE_PROVIDERS:
                url = FREE_PROVIDERS[provider_name].get("API_URL", "")
                if url:
                    preset_urls.append(url)

        all_urls = list(dict.fromkeys(preset_urls + [template_url]))

        self.apiUrlCombo.blockSignals(True)
        self.apiUrlCombo.clear()
        self.apiUrlCombo.addItems(all_urls)
        self.apiUrlCombo.blockSignals(False)

    def _on_provider_changed(self, name: str):
        """服务商变化时更新预设值"""
        if name in FREE_PROVIDERS:
            template = FREE_PROVIDERS[name]
            template_url = template.get("API_URL", "")
            self._load_preset_urls(name, template_url)

            preset_url = template.get("API_URL", "")
            existing_items = [self.apiUrlCombo.itemText(i) for i in range(self.apiUrlCombo.count())]
            if preset_url and preset_url in existing_items:
                idx = self.apiUrlCombo.findText(preset_url)
                self.apiUrlCombo.setCurrentIndex(idx)
            else:
                self.apiUrlCombo.setCurrentText(preset_url)

            self.modelCombo.blockSignals(True)
            self.modelCombo.clear()
            if name in PROVIDER_MODELS:
                self.modelCombo.addItems(PROVIDER_MODELS[name])
            default_model = template.get("模型名称", "")
            if default_model:
                self.modelCombo.addItem(default_model)
            if self.modelCombo.count() > 0:
                self.modelCombo.setCurrentIndex(0)
            self.modelCombo.blockSignals(False)
        self._update_extra_config_visibility()

    def _update_extra_config_visibility(self):
        """根据当前服务商名称显示/隐藏套餐配置区"""
        if not hasattr(self, "_extra_config_section"):
            return
        provider = self.nameCombo.currentText() if self.is_new else self.provider_name
        is_opencode = "opencode" in provider.lower()
        is_volc = "火山方舟" in provider

        # 先全部隐藏
        for row_widget in self._extra_field_rows.values():
            row_widget.setVisible(False)

        # 确定当前组
        if is_opencode:
            group = "opencode"
        elif is_volc:
            group = "火山方舟"
        else:
            group = None

        if group:
            for internal_key, _, _ in self._extra_field_defs.get(group, []):
                row = self._extra_field_rows.get(internal_key)
                if row:
                    row.setVisible(True)

        self._extra_config_section.setVisible(group is not None)

    def _open_help_url(self, name: str):
        """打开帮助链接"""
        if name in FREE_PROVIDERS:
            import webbrowser
            url = FREE_PROVIDERS[name].get("获取地址", "")
            if url:
                webbrowser.open(url)

    def _on_fetch_models(self):
        """获取模型列表"""
        from qfluentwidgets import InfoBar

        api_url = self.apiUrlCombo.currentText().strip()
        api_key = self.apiKeyEdit.text().strip()
        provider_name = self.nameCombo.currentText() if self.is_new else self.provider_name

        if not api_url or not api_key:
            InfoBar.warning("提示", "请先填写 API URL 和 Key", parent=self.window(), duration=2000, position=InfoBarPosition.BOTTOM)
            return

        self.fetchBtn.setEnabled(False)
        InfoBar.info("获取中", "正在获取模型列表...", parent=self.window(), duration=3000, position=InfoBarPosition.BOTTOM)

        def do_fetch():
            return fetch_provider_models(api_url, api_key, provider_name)

        thread = threading.Thread(target=self._do_fetch_thread, args=(do_fetch,))
        thread.daemon = True
        thread.start()

    def _do_fetch_thread(self, fetch_func):
        """在后台线程中获取模型"""
        import time

        time.sleep(0.1)
        models = fetch_func()
        if models:
            self._fetched_models = models
            self.fetchSuccess.emit(models)
        else:
            self.fetchFailed.emit()

    def _on_fetch_success(self, models: list):
        """获取成功（主线程）"""
        self.fetchBtn.setEnabled(True)
        self.modelCombo.blockSignals(True)
        current = self.modelCombo.currentText()
        self.modelCombo.clear()
        self.modelCombo.addItems(models)
        if current and self.modelCombo.findText(current) >= 0:
            self.modelCombo.setCurrentIndex(self.modelCombo.findText(current))
        self.modelCombo.blockSignals(False)
        from qfluentwidgets import InfoBar
        InfoBar.success("成功", f"获取到 {len(models)} 个模型", parent=self.window(), duration=2000, position=InfoBarPosition.BOTTOM)

    def _on_fetch_failed(self):
        """获取失败（主线程）"""
        self.fetchBtn.setEnabled(True)
        from qfluentwidgets import InfoBar
        InfoBar.error("失败", "获取模型列表失败，请检查配置", parent=self.window(), duration=3000, position=InfoBarPosition.BOTTOM)

    def _on_manage_models(self):
        """打开模型列表管理对话框"""
        current_models = self.modelCombo.get_all_models()
        dialog = ModelListEditDialog(current_models, self.window())
        if dialog.exec_() == dialog.Accepted:
            new_models = dialog.get_models()
            self.modelCombo.blockSignals(True)
            self.modelCombo.clear()
            self.modelCombo.addItems(new_models)
            current = self.modelCombo.currentText()
            if not current or self.modelCombo.findText(current) < 0:
                if new_models:
                    self.modelCombo.setCurrentIndex(0)
            self.modelCombo.blockSignals(False)

    def _on_save(self):
        """保存。

        不再手工保留 config_id——config_id 现在由 main_widget 端基于 apikey
        的稳定 hash 计算（见 app.core.provider_profile.apply_provider_save），
        编辑同 apikey 始终命中同一条目，不会再产生重复。
        """
        provider_name = self.nameCombo.currentText() if self.is_new else self.provider_name
        current_models = self.modelCombo.get_all_models()
        existing_models = self.provider_info.get("模型列表", [])
        # 编辑场景下保留旧 config_id，让 main_widget 能据此判断 apikey 是否被改过
        existing_config_id = self.provider_info.get("config_id", "")
        # 先提取套餐用量额外字段（在覆盖 self.provider_info 之前）
        extra_fields = {}
        for internal_key, config_key in self._extra_key_map.items():
            editor = getattr(self, f"{internal_key}_edit", None)
            if editor is not None:
                val = editor.text().strip()
                if val:
                    extra_fields[config_key] = val
            else:
                old_val = self.provider_info.get(config_key, "")
                if old_val:
                    extra_fields[config_key] = old_val
        self.provider_info = {
            "API_URL": self.apiUrlCombo.currentText().strip(),
            "API_KEY": self.apiKeyEdit.text().strip(),
            "模型名称": self.modelCombo.currentText().strip(),
            "认证方式": "bearer",
            "name": self.configNameEdit.text().strip(),
        }
        if existing_config_id:
            self.provider_info["config_id"] = existing_config_id
        if current_models:
            self.provider_info["模型列表"] = current_models
        elif existing_models:
            self.provider_info["模型列表"] = existing_models
        else:
            self.provider_info["模型列表"] = []

        # 写入套餐用量额外字段
        self.provider_info.update(extra_fields)

        self.saved.emit(provider_name, self.provider_info)

    def _on_cancel(self):
        """取消"""
        self.closed.emit()

    def get_result(self):
        """获取结果"""
        if self.is_new:
            return self.nameCombo.currentText(), self.provider_info
        return self.provider_name, self.provider_info

    def get_save_button(self):
        """获取保存按钮，供父组件移到关闭按钮旁边"""
        return self.save_btn