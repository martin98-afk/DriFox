from typing import Any, Dict, List, Optional

MAX_SESSION_CARD_CACHE_SIZE = 10

# ============================================================
# 可识别的图片扩展名（统一常量，多处复用）
# 注意：gateway/base.py 中额外包含 .svg，用途不同，不纳入此集合
# ============================================================
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

# ============================================================
# 统一参数 schema：定义所有模型参数的 UI 表现与 API 映射
# - ui_type:      checkbox / combobox / slider / spinbox / password / line
# - display_name: 展示名（不传则用 key 本身）
# - api_param:    映射到 API 请求的字段名（不传则需在 worker 中特殊处理）
# - range:        slider/spinbox 的取值范围
# - options:      combobox 的选项列表
# - order:        在 ModelConfigCard 中的展示顺序（越小越靠前；未设则 999）
# - hide_in_card: True 时不渲染到 ModelConfigCard（用于别名/系统字段等）
# ============================================================
PARAM_SCHEMA = {
    "温度": {
        "display_name": "温度",
        "ui_type": "slider",
        "range": {"min": 0.0, "max": 2.0, "step": 0.01, "type": "float"},
        "api_param": "temperature",
        "order": 300,
    },
    "temp": {
        "display_name": "温度",
        "ui_type": "slider",
        "range": {"min": 0.0, "max": 2.0, "step": 0.01, "type": "float"},
        "api_param": "temperature",
        "order": 300,
        "hide_in_card": True,  # "温度" 的别名
    },
    # P1-21：「最大Token」只作**上下文窗口**（不再直发 max_tokens）。
    # api_param 保留 max_tokens 是历史字段名兼容，发送侧已改为只读「最大输出」。
    "最大Token": {
        "display_name": "上下文长度",
        "ui_type": "spinbox",
        "range": {"min": 1, "max": 99999999, "step": 1, "type": "int"},
        "api_param": "max_tokens",
        "order": 100,
    },
    # P1-21 语义拆分：输出上限独立成键（旧「最大Token」一键双语义——既当上下文
    # 窗口又直接当 max_tokens 发出——已被拆开）。api_param 必须为 max_tokens，
    # 这是发送链识别它的唯一依据（subagent 通配分支按 api_param 收集请求参数）。
    "最大输出": {
        "display_name": "最大输出",
        "ui_type": "spinbox",
        "range": {"min": 1, "max": 99999999, "step": 1, "type": "int"},
        "api_param": "max_tokens",
        "order": 101,
    },
    "上下文长度": {
        "display_name": "上下文长度",
        "ui_type": "spinbox",
        "range": {"min": 1, "max": 99999999, "step": 1, "type": "int"},
        "api_param": "max_tokens",
        "order": 100,
        "hide_in_card": True,  # 与 "最大Token" 等价，只显示一个
    },
    "max_new_tokens": {
        "display_name": "最大新Token",
        "ui_type": "spinbox",
        "range": {"min": 1, "max": 18192, "step": 1, "type": "int"},
        "api_param": "max_tokens",
        "order": 340,
    },
    "top_p": {
        "display_name": "核采样 (top_p)",
        "ui_type": "slider",
        "range": {"min": 0.0, "max": 1.0, "step": 0.01, "type": "float"},
        "api_param": "top_p",
        "order": 310,
    },
    "frequency_penalty": {
        "display_name": "频率惩罚",
        "ui_type": "slider",
        "range": {"min": -2.0, "max": 2.0, "step": 0.01, "type": "float"},
        "api_param": "frequency_penalty",
        "order": 320,
        "hide_in_card": True,  # 不常用，不在配置卡显示
    },
    "presence_penalty": {
        "display_name": "存在惩罚",
        "ui_type": "slider",
        "range": {"min": -2.0, "max": 2.0, "step": 0.01, "type": "float"},
        "api_param": "presence_penalty",
        "order": 330,
        "hide_in_card": True,  # 不常用，不在配置卡显示
    },
    "思考模式": {
        "display_name": "思考模式",
        "ui_type": "checkbox",
        "order": 200,
        # 无 api_param，由 chat_worker 特殊处理
    },
    "思考预算": {
        "display_name": "思考预算",
        "ui_type": "spinbox",
        "range": {"min": 256, "max": 65536, "step": 256, "type": "int"},
        "api_param": "thinking_budget",
        "order": 210,
    },
    "思考等级": {
        "display_name": "思考等级",
        "ui_type": "combobox",
        "options": ["low", "medium", "high", "max"],
        "api_param": "reasoning_effort",
        "order": 220,
    },
    # ── P1-9 段三：能力声明六键（model_overrides 持久化；resolve 链 L0 层）──
    # ⚠ 绝不给 api_param：声明键是中文，_VALID_IDENTIFIER_PATTERN 第二挡也过不去，
    #   双挡保证它们永远不会泄漏进 API 请求体。
    # tri_state：ComboBox「自动/开启/关闭」——「自动」= 未声明（get_config 删键）。
    "声明_支持思考": {
        "display_name": "声明: 支持思考",
        "ui_type": "tri_state",
        "order": 500,
    },
    "声明_支持图像": {
        "display_name": "声明: 支持图像",
        "ui_type": "tri_state",
        "order": 501,
    },
    # lineedit_int allow_empty：空串 = 未声明（get_config 删键）
    "声明_上下文长度": {
        "display_name": "声明: 上下文长度",
        "ui_type": "lineedit_int",
        "order": 502,
    },
    "声明_最大输出": {
        "display_name": "声明: 最大输出",
        "ui_type": "lineedit_int",
        "order": 503,
    },
    # lineedit：自由文本（思考参数 thinking/thinking_budget/reasoning_effort；
    # 思考强度逗号分隔等级列表）。空串 = 未声明（resolve 链容忍，不删键）
    "声明_思考参数": {
        "display_name": "声明: 思考参数",
        "ui_type": "lineedit",
        "order": 504,
    },
    "声明_思考强度": {
        "display_name": "声明: 思考强度",
        "ui_type": "lineedit",
        "order": 505,
    },
    "启用技能": {
        "display_name": "启用技能",
        "ui_type": "checkbox",
        "hide_in_card": True,  # 配置卡里不显示，启用技能在别处控制
    },
    "API_KEY": {
        "ui_type": "password",
    },
    "选择模型": {
        "ui_type": "model_selector",
    },
}

# ============================================================
# 模型级参数（按模型名持久化，不按服务商实例）
# ============================================================
# 用户在 UI 上改这些参数时，会存入 `llm_model_overrides[模型名]`，
# 而不是 `saved_providers[config_id]`。
# 连接级参数（API_URL, API_KEY, 认证方式等）仍按服务商实例存。
MODEL_LEVEL_KEYS = frozenset(
    "温度 temp 最大Token 上下文长度 max_new_tokens "
    "top_p frequency_penalty presence_penalty "
    "思考模式 思考预算 思考等级 启用技能 "
    "声明_支持思考 声明_支持图像 声明_上下文长度 声明_最大输出 声明_思考参数 声明_思考强度".split()
)

# ──────────────────────────────────────────────────────────────
# 服务商数据全部移入 providers 插件（万物为插件）：
#   PROVIDER_MODELS / FREE_PROVIDERS / PROVIDER_ICONS /
#   QUOTA_EXCLUDE_KEYS 已从本模块移除，统一由
#   app.plugins.registries.provider_registry.ProviderRegistry 提供。
# 历史常量名被删除；以下仅保留"函数委托"以最小化消费方改动面。
# ──────────────────────────────────────────────────────────────


def get_merged_provider_models() -> Dict[str, List[str]]:
    """PROVIDER_MODELS 与 models.dev 动态数据的合并结果（委托 ProviderRegistry）。

    合并规则参阅 app.plugins.registries.provider_registry.get_merged_provider_models。
    回退时使用插件声明的模型（不再存在硬编码模型表）。
    """
    from app.plugins.registries.provider_registry import ProviderRegistry

    try:
        return ProviderRegistry.get_instance().get_merged_provider_models()
    except Exception:
        # 注册表未初始化（极早期调用）：退回空表，避免拖垮主流程
        return {}


def provider_default_config(name: str) -> Optional[Dict[str, Any]]:
    """服务商默认配置 dict（FREE_PROVIDERS[name] 委托），不存在返回 None"""
    from app.plugins.registries.provider_registry import ProviderRegistry

    try:
        return ProviderRegistry.get_instance().default_config(name)
    except Exception:
        return None


def provider_icon_map() -> Dict[str, str]:
    """服务商 → 图标 key（PROVIDER_ICONS 委托）"""
    from app.plugins.registries.provider_registry import ProviderRegistry

    try:
        return ProviderRegistry.get_instance().icon_map()
    except Exception:
        return {}


def provider_quota_exclude_keys() -> "frozenset[str]":
    """全部服务商用量查询额外字段 key（QUOTA_EXCLUDE_KEYS 委托）。

    该集合与模型参数无关，仅用于配额查询；字段由 providers 插件
    的 extra_quota_fields 声明，不得泄漏到模型参数或 API 请求。
    """
    from app.plugins.registries.provider_registry import ProviderRegistry

    try:
        return ProviderRegistry.get_instance().quota_exclude_keys()
    except Exception:
        # 注册表未初始化：保守返回空集（调用方通常已提前加载插件）
        return frozenset()
