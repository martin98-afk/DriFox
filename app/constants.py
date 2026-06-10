MAX_SESSION_CARD_CACHE_SIZE = 10

# ============================================================
# 套餐用量查询字段（与模型参数无关，仅用于配额查询，不得泄漏到模型参数或 API 请求）
# ============================================================
QUOTA_EXCLUDE_KEYS = frozenset({
    "server_id", "cookie", "workspace_id",
    "csrf_token", "x_web_id",
})

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
    "最大Token": {
        "display_name": "上下文长度",
        "ui_type": "spinbox",
        "range": {"min": 1, "max": 99999999, "step": 1, "type": "int"},
        "api_param": "max_tokens",
        "order": 100,
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
    "思考模式 思考预算 思考等级 启用技能".split()
)


PROVIDER_MODELS = {
    "火山方舟": [
        "doubao-seed-code",
        "kimi-k2.6 ",
        "kimi-k2.5",
        "minimax-m2.7",
        "glm-4.7",
        "glm5.1"
    ],
    "MiniMax": [
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5",
        "MiniMax-M2.5-highspeed",
        "MiniMax-M2.1",
        "MiniMax-M2.1-highspeed",
    ],
    "SiliconFlow (硅基流动)": [
        "Qwen/Qwen2.5-7B-Instruct",
        "Qwen/Qwen2.5-14B-Instruct",
        "Qwen/Qwen2.5-72B-Instruct",
        "Qwen/Qwen2.5-7B-Instruct-AWQ",
        "THUDM/glm4-9b-chat",
        "meta-llama/Meta-Llama-3.1-70B-Instruct",
        "meta-llama/Meta-Llama-3.1-8B-Instruct",
        "deepseek-ai/DeepSeek-V2-Chat",
        "Qwen/Qwen2-72B-Instruct",
    ],
    "智谱AI": [
        "glm-5.1",
        "glm-5-turbo",
        "glm-4-pro",
        "glm-4-flash",
        "glm-4-flashx",
        "glm-4-plus",
        "glm-4",
    ],
    "DeepSeek": [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ],
    "Groq": [
        "openai/gpt-oss-120b",
        "qwen/qwen3-32b",
        "groq/compound",
        "llama-3.3-70b-versatile",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "moonshotai/kimi-k2-instruct-0905",
    ],
    "百度千帆": [
        "ernie-3.5-8k",
        "ernie-3.5-4k",
        "ernie-speed-8k",
        "ernie-speed-128k",
    ],
    "Ollama": [
        "llama3",
        "llama3.1",
        "qwen2.5",
        "qwen2.5-coder",
        "mistral",
        "phi3",
    ],
    "阿里云 (DashScope)": [
        "qwen3-max",
        "qwen3-plus",
        "qwen3.5-max",
    ],
    "OpenAI": [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "gpt-4",
        "gpt-3.5-turbo",
    ],
    "Anthropic (Claude)": [
        "claude-sonnet-4-20250514",
        "claude-3-5-sonnet-latest",
        "claude-3-5-haiku-latest",
        "claude-3-opus-latest",
        "claude-3-haiku-latest",
    ],
    "Google Gemini": [
        "gemini-2.5-pro-preview-06-05",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gemini-1.5-flash-8b",
    ],
    "OpenCode Zen": [
        "deepseek-v4-flash-free",
        "nemotron-3-super-free",
        "big-pickle",
        "glm-5.1",
        "glm-5",
        "kimi-k2.6",
        "kimi-k2.5",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "mimo-v2.5-pro",
        "mimo-v2.5",
        "minimax-m2.7",
        "minimax-m2.5",
        "qwen3.6-plus",
        "qwen3.5-plus",
    ],
}

FREE_PROVIDERS = {
    "MiniMax": {
        "API_URL": "https://api.minimax.chat/v1",
        "API_KEY": "",
        "模型名称": "MiniMax-M2.5",
        "温度": 0.7,
        "最大Token": 200000,
        "认证方式": "bearer",
        "获取地址": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
    },
    "OpenCode Zen": {
        "API_URL": "https://opencode.ai/zen/v1",
        "API_KEY": "",
        "模型名称": "deepseek-v4-flash-free",
        "温度": 0.7,
        "最大Token": 200000,
        "认证方式": "bearer",
        "获取地址": "https://opencode.ai/auth",
    },
    "火山方舟": {
        "API_URL": "https://ark.cn-beijing.volces.com/api/coding/v3",
        "API_KEY": "",
        "模型名称": "doubao-pro-32k",
        "温度": 0.7,
        "最大Token": 200000,
        "认证方式": "bearer",
        "获取地址": "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
    },
    "SiliconFlow (硅基流动)": {
        "API_URL": "https://api.siliconflow.cn/v1",
        "API_KEY": "",
        "思考预算": "medium",
        "模型名称": "deepseek-ai/DeepSeek-R1",
        "温度": 0.6,
        "最大Token": 200000,
        "认证方式": "bearer",
        "获取地址": "https://cloud.siliconflow.cn/account/ak",
    },
    "阿里云 (DashScope)": {
        "API_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "API_KEY": "",
        "模型名称": "qwen3.5-plus",
        "温度": 0.7,
        "最大Token": 200000,
        "认证方式": "bearer",
        "获取地址": "https://bailian.console.aliyun.com/cn-beijing?tab=model#/api-key",
    },
    "智谱AI": {
        "API_URL": "https://open.bigmodel.cn/api/paas/v4",
        "API_KEY": "",
        "思考模式": True,
        "模型名称": "glm-4-flash",
        "温度": 0.7,
        "最大Token": 40960,
        "认证方式": "bearer",
        "获取地址": "https://open.bigmodel.cn/apikey/platform",
    },
    "DeepSeek": {
        "API_URL": "https://api.deepseek.com",
        "API_KEY": "",
        "思考模式": False,
        "思考等级": "high",
        "模型名称": "deepseek-chat",
        "温度": 0.7,
        "最大Token": 40960,
        "认证方式": "bearer",
        "获取地址": "https://platform.deepseek.com/api_keys",
    },
    "Groq": {
        "API_URL": "https://api.groq.com/openai/v1",
        "API_KEY": "",
        "模型名称": "llama-3.1-70b-versatile",
        "温度": 0.7,
        "最大Token": 40960,
        "认证方式": "bearer",
        "获取地址": "https://console.groq.com/keys",
    },
    "百度千帆": {
        "API_URL": "https://qianfan.baidubce.com/v2",
        "API_KEY": "",
        "模型名称": "ernie-3.5-8k",
        "温度": 0.7,
        "最大Token": 40960,
        "认证方式": "bce",
        "获取地址": "https://console.bce.baidu.com/qianfan/ais/console/apikey",
    },
    "Ollama": {
        "API_URL": "http://localhost:11434/v1",
        "API_KEY": "not-needed",
        "模型名称": "llama3",
        "温度": 0.7,
        "最大Token": 40960,
        "认证方式": "none",
        "获取地址": "https://ollama.com",
    },
    "OpenAI": {
        "API_URL": "https://api.openai.com/v1",
        "API_KEY": "",
        "模型名称": "gpt-4o-mini",
        "温度": 0.7,
        "最大Token": 40960,
        "认证方式": "bearer",
        "获取地址": "https://platform.openai.com/api-keys",
    },
    "Anthropic (Claude)": {
        "API_URL": "https://api.anthropic.com/v1",
        "API_KEY": "",
        "模型名称": "claude-sonnet-4-20250514",
        "温度": 0.7,
        "最大Token": 40960,
        "认证方式": "anthropic",
        "获取地址": "https://console.anthropic.com/settings/keys",
    },
    "Google Gemini": {
        "API_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "API_KEY": "",
        "模型名称": "gemini-2.0-flash",
        "温度": 0.7,
        "最大Token": 40960,
        "认证方式": "bearer",
        "获取地址": "https://aistudio.google.com/app/apikey",
    },
}

PROVIDER_ICONS = {
    "火山方舟": "火山引擎",
    "MiniMax": "MiniMax",
    "SiliconFlow (硅基流动)": "siliconflow",
    "阿里云 (DashScope)": "qwen",
    "智谱AI": "智谱",
    "DeepSeek": "deepseek",
    "Groq": "groq",
    "百度千帆": "baidu",
    "Ollama": "Ollama",
    "OpenAI": "大模型",
    "Anthropic (Claude)": "Anthropic",
    "Google Gemini": "gemini-ai",
    "OpenCode Zen": "opencode",
}
