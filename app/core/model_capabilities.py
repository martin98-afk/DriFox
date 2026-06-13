# -*- coding: utf-8 -*-
"""
模型能力字典 - 按模型名精确给 context_limit 和思考相关特性。

数据来源策略（每条都带 source 字段标注）：
    - "models.dev"        : OpenCode 用的真实模型元数据库 https://models.dev/api.json
    - "vendor_official"   : 模型厂商官方文档
    - "user_provided"     : 用户直接提供的资料
    - "industry_default"  : 行业通用认知（已交叉验证）

重要历史教训：
    2026-06 项目曾用 LLM 自动生成的"虚构"模型名（如 deepseek-v4-flash-free、
    nemotron-3-super-free、qwen3.5-plus 等）填入代码，导致预设值完全不靠谱。
    现在本字典只录"已通过真实数据源核实"的条目；找不到的模型一律走
    L3（服务商默认）/ L4（family 兜底），绝不写"看起来合理"的猜测值。

查找优先级（resolve_context_limit）：
    L1: 用户在 llm_config 显式填的最大Token / context_limit / 上下文长度
    L2: MODEL_CAPABILITIES[模型名].context_limit      ← 本模块
    L3: FREE_PROVIDERS[provider_name].最大Token       ← 服务商默认
    L4: PROVIDER_CAPABILITIES[family].context_limit   ← family 兜底

用户自调节值：
    在 model_overrides 字典里按模型名持久化，UI 加载时叠加。
    该机制与本模块的"内置默认"是两个独立层：内置默认只是初值。
"""

from typing import Any, Dict, Optional

from app.constants import FREE_PROVIDERS
from app.core.provider_profile import get_provider_profile


# =============================================================================
# 字段名候选（按这个顺序查 llm_config 里的显式值）
# =============================================================================
_CONTEXT_LIMIT_KEYS = ("最大Token", "context_limit", "上下文长度", "max_context_tokens")

# =============================================================================
# 硬编码兜底默认值
# 当 FREE_PROVIDERS 没有这个服务商、MODEL_CAPABILITIES 也没覆盖该字段时，用这里
# =============================================================================
DEFAULT_MODEL_PARAMS: Dict[str, Any] = {
    "温度": 0.7,
    "top_p": 1.0,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
    "上下文长度": 128000,
    "最大Token": 128000,
    # 注：思考模式/思考等级/启用技能不在兜底里——
    # 这些字段对不支持的模型无意义，仅在 MODEL_CAPABILITIES 标记 supports_thinking=True 时才注入。
}


# =============================================================================
# 模型能力字典
# 格式：模型名（小写精确匹配）-> {
#     context_limit:    int,          # 上下文窗口（tokens）
#     supports_thinking: bool,        # 是否支持思考模式
#     thinking_param:   str|None,    # 思考控制字段名：
#                                    #   "reasoning_effort" -> 请求体 reasoning_effort 字段
#                                    #   "thinking"         -> extra_body.thinking
#                                    #   "thinking_budget"  -> extra_body.thinking_budget
#     thinking_enable_value: str = "enabled"
#                                    # thinking.type 的值。多数模型用 "enabled"，
#                                    # MiniMax 系列用 "adaptive"
#     supports_vision:  bool,         # 是否支持图像（默认 False）
#     source:           str,          # 数据来源（必填）
#     note:             str,          # 备注（可选）
# }
# 找不到的模型走 L3 服务商默认 / L4 family 兜底
# =============================================================================
MODEL_CAPABILITIES: Dict[str, Dict[str, Any]] = {

    # ========== OpenCode Go 真实模型（用户 2026-06-02 给出 + models.dev 验证） ==========
    # OpenCode Go 是付费服务（首月 $5，之后 $10/月），这些是它真实提供的模型。
    "kimi-k2.5": {
        "context_limit": 262144, "supports_thinking": True, "thinking_param": "thinking",
        "source": "models.dev", "note": "Moonshot Kimi K2.5，2026-01-27 发布；API 用 thinking:enabled/disabled",
    },
    "kimi-k2.6": {
        "context_limit": 262144, "supports_thinking": True, "thinking_param": "thinking",
        "source": "models.dev", "note": "Moonshot Kimi K2.6，2026-04-20/21 发布；API 用 thinking:enabled/disabled",
    },
    "glm-5": {
        "context_limit": 204800, "supports_thinking": True, "thinking_param": "thinking",
        "source": "models.dev", "note": "智谱 GLM-5，2026-02-12 发布，200K 上下文，interleaved reasoning_content",
    },
    "glm-5.1": {
        "context_limit": 204800, "supports_thinking": True, "thinking_param": "thinking",
        "source": "models.dev", "note": "智谱 GLM-5.1，2026-03-27/28 发布，200K 上下文",
    },
    "mimo-v2.5-pro": {
        "context_limit": 1000000,
        "source": "vendor_official", "note": "小米 MiMo-V2.5-Pro，2026-04-23 公测，2026-04-27 开源；OpenCode Go 提供。思考控制参数未确认，暂不开放开关",
    },
    "mimo-v2.5": {
        "context_limit": 1000000,
        "source": "vendor_official", "note": "小米 MiMo-V2.5 全模态通用模型，2026-04-23 公测；OpenCode Go 提供。思考控制参数未确认，暂不开放开关",
    },
    "minimax-m2.5": {
        "context_limit": 204800, "supports_thinking": True, "thinking_param": "thinking", "thinking_enable_value": "adaptive",
        "source": "models.dev", "note": "MiniMax M2.5，2026-02-12 发布",
    },
    "minimax-m2.7": {
        "context_limit": 196608, "supports_thinking": True, "thinking_param": "thinking", "thinking_enable_value": "adaptive",
        "source": "models.dev", "note": "MiniMax M2.7，2026-03-18 发布",
    },
    "minimax-m3": {
        "context_limit": 512000, "supports_thinking": True, "thinking_param": "thinking", "thinking_enable_value": "adaptive",
        "source": "models.dev", "note": "MiniMax M3，2026-05-31 发布，全模态",
    },
    "qwen3.6-plus": {
        "context_limit": 128000, "supports_thinking": True, "thinking_param": "thinking",
        "source": "models.dev", "note": "通义 Qwen3.6-Plus，2026-04-02 发布；API 格式同 DashScope 系",
    },
    "qwen3.5-plus": {
        "context_limit": 128000, "supports_thinking": True, "thinking_param": "thinking",
        "source": "inferred", "note": "OpenCode Zen 提供；thinking 控制方式同 qwen3.6-plus",
    },
    "qwen3.7-max": {
        "context_limit": 1000000, "supports_thinking": True, "thinking_param": "thinking",
        "source": "vendor_official", "note": "通义 Qwen3.7-Max，2026-05-20 阿里云峰会发布；API 格式同 DashScope 系",
    },
    "deepseek-v4-pro": {
        "context_limit": 1048576, "supports_thinking": True, "thinking_param": "reasoning_effort",
        "source": "models.dev", "note": "DeepSeek-V4-Pro，2026-04-24 发布，1.6T 总参 / 49B 激活",
    },
    "deepseek-v4-flash": {
        "context_limit": 1048576, "supports_thinking": True, "thinking_param": "reasoning_effort",
        "source": "models.dev", "note": "DeepSeek-V4-Flash，2026-04-24 发布",
    },
    "deepseek-v4-flash-free": {
        "context_limit": 1048576, "supports_thinking": True, "thinking_param": "reasoning_effort",
        "source": "inferred", "note": "OpenCode Zen 免费档转发 DeepSeek-V4-Flash；thinking_param 沿用 deepseek-v4-flash",
    },

    # ========== OpenCode Zen 真实免费模型（来自 opencode/xxx 系列） ==========
    # 注意：big-pickle / gpt-5-nano 的精确 context length 没在 models.dev 公开，
    # 用户确认它们是 OpenCode Zen 的免费档，所以这里不填具体值，让它们走
    # L3 服务商默认（200k）。如未来核实到精确值再加。
    "kimi-k2.5-free": {
        "context_limit": 262144, "supports_thinking": True, "thinking_param": "thinking",
        "source": "user_provided", "note": "OpenCode Zen 免费档，转发到 Kimi K2.5",
    },
    "minimax-m2.5-free": {
        "context_limit": 204800, "supports_thinking": True, "thinking_param": "thinking", "thinking_enable_value": "adaptive",
        "source": "user_provided", "note": "OpenCode Zen 免费档，转发到 MiniMax M2.5",
    },
    # "glm-5-free" - 是 GLM-5 的 OpenCode Zen 代理，context 推测与 glm-5 一致
    "glm-5-free": {
        "context_limit": 202752, "supports_thinking": True, "thinking_param": "thinking",
        "source": "inferred", "note": "OpenCode Zen 免费档转发 GLM-5；context 沿用 glm-5",
    },

    # ========== OpenAI（Anthropic/Gemini/DeepSeek 同下） ==========
    "gpt-4o":          {"context_limit": 128000, "supports_vision": True, "source": "openai_official"},
    "gpt-4o-mini":     {"context_limit": 128000, "supports_vision": True, "source": "openai_official"},
    "gpt-4-turbo":     {"context_limit": 128000, "supports_vision": True, "source": "openai_official"},
    "gpt-4":           {"context_limit": 8192,                          "source": "openai_official"},
    "gpt-3.5-turbo":   {"context_limit": 16385,                         "source": "openai_official"},

    # ========== Anthropic Claude ==========
    "claude-sonnet-4-20250514": {"context_limit": 200000, "supports_vision": True, "supports_thinking": True, "thinking_param": "thinking", "source": "anthropic_official", "note": "Claude Sonnet 4，支持 extended thinking"},
    "claude-3-5-sonnet-latest": {"context_limit": 200000, "supports_vision": True, "supports_thinking": True, "thinking_param": "thinking", "source": "anthropic_official", "note": "Claude 3.5 Sonnet，支持 extended thinking"},
    "claude-3-5-haiku-latest":  {"context_limit": 200000, "supports_vision": True, "supports_thinking": True, "thinking_param": "thinking", "source": "anthropic_official", "note": "Claude 3.5 Haiku，支持 extended thinking"},
    "claude-3-opus-latest":     {"context_limit": 200000, "supports_vision": True, "supports_thinking": True, "thinking_param": "thinking", "source": "anthropic_official", "note": "Claude 3 Opus，支持 extended thinking"},
    "claude-3-haiku-latest":    {"context_limit": 200000, "supports_vision": True, "source": "anthropic_official", "note": "Claude 3 Haiku，**不支持** extended thinking（轻量型号，定位快速低成本）"},

    # ========== Google Gemini ==========
    "gemini-2.5-pro-preview-06-05": {"context_limit": 1000000, "supports_vision": True, "source": "google_official"},
    "gemini-2.0-flash":            {"context_limit": 1000000, "supports_vision": True, "source": "google_official"},
    "gemini-1.5-pro":              {"context_limit": 1000000, "supports_vision": True, "source": "google_official"},
    "gemini-1.5-flash":            {"context_limit": 1000000, "supports_vision": True, "source": "google_official"},
    "gemini-1.5-flash-8b":         {"context_limit": 1000000, "supports_vision": True, "source": "google_official"},

    # ========== DeepSeek（官方 API） ==========
    # 注意：2026-04-24 DeepSeek V4 已经发布（deepseek-v4-flash / deepseek-v4-pro，1M context）。
    # deepseek-chat（原 V3）和 deepseek-reasoner（原 R1）在当前 API 中的映射关系不明确，
    # 不在此录入，让它们走 L3（FREE_PROVIDERS["DeepSeek"]["最大Token"]=40960）或
    # L4（PROVIDER_CAPABILITIES["deepseek"]["context_limit"]=320000）兜底。

    # ========== 智谱 AI GLM-4 系列 ==========
    "glm-4-flash":  {"context_limit": 128000, "supports_thinking": True, "thinking_param": "thinking",
                     "source": "zhipu_official"},
    "glm-4-flashx": {"context_limit": 128000, "supports_thinking": True, "thinking_param": "thinking",
                     "source": "zhipu_official"},
    "glm-4-plus":   {"context_limit": 128000, "supports_thinking": True, "thinking_param": "thinking",
                     "source": "zhipu_official"},
    "glm-4-pro":    {"context_limit": 128000, "supports_thinking": True, "thinking_param": "thinking",
                     "source": "zhipu_official"},
    "glm-4":        {"context_limit": 128000, "supports_thinking": True, "thinking_param": "thinking",
                     "source": "zhipu_official"},
    "glm-5-turbo":  {"context_limit": 128000, "supports_thinking": True, "thinking_param": "thinking",
                     "source": "zhipu_official"},

    # ========== 通义千问 Qwen2.5（开源版） ==========
    "qwen2.5-7b-instruct":   {"context_limit": 32768, "source": "alibaba_official"},
    "qwen2.5-14b-instruct":  {"context_limit": 32768, "source": "alibaba_official"},
    "qwen2.5-72b-instruct":  {"context_limit": 32768, "source": "alibaba_official"},
    "qwen2.5-7b-instruct-awq": {"context_limit": 32768, "source": "alibaba_official"},

    # ========== Meta Llama 3.1/3.3（Groq 等平台代理） ==========
    "llama-3.1-70b-versatile": {"context_limit": 131072, "source": "meta_official"},
    "llama-3.1-8b-versatile":  {"context_limit": 131072, "source": "meta_official"},
    "llama-3.3-70b-versatile": {"context_limit": 131072, "source": "meta_official"},
    "llama-4-scout-17b-16e-instruct": {"context_limit": 131072, "source": "meta_official"},
    "meta-llama/llama-4-scout-17b-16e-instruct": {"context_limit": 131072, "source": "meta_official"},
    "meta-llama/meta-llama-3.1-70b-instruct":     {"context_limit": 131072, "source": "meta_official"},
    "meta-llama/meta-llama-3.1-8b-instruct":      {"context_limit": 131072, "source": "meta_official"},

    # ========== Groq 平台上的特殊模型 ==========
    "openai/gpt-oss-120b":      {"context_limit": 131072, "supports_thinking": True, "source": "models.dev",
                                 "note": "OpenAI gpt-oss-120b 开源模型，2025-08-05 发布"},
    "openai/gpt-oss-20b":       {"context_limit": 131072, "supports_thinking": True, "source": "models.dev"},
    "gpt-oss:120b":             {"context_limit": 131072, "supports_thinking": True, "source": "models.dev"},
    "gpt-oss:20b":              {"context_limit": 131072, "supports_thinking": True, "source": "models.dev"},
    "moonshotai/kimi-k2-instruct-0905": {"context_limit": 131072, "source": "models.dev"},
    "groq/compound":            {"context_limit": 131072, "source": "models.dev"},

    # ========== 百度千帆（文心） ==========
    "ernie-3.5-8k":     {"context_limit": 8192,   "source": "baidu_official"},
    "ernie-3.5-4k":     {"context_limit": 4096,   "source": "baidu_official"},
    "ernie-speed-8k":   {"context_limit": 8192,   "source": "baidu_official"},
    "ernie-speed-128k": {"context_limit": 128000, "source": "baidu_official"},

    # ========== 火山方舟 Doubao ==========
    "doubao-seed-code": {"context_limit": 200000, "source": "vendor_official",
                         "note": "火山方舟编程模型"},
    "doubao-pro-32k":   {"context_limit": 32000,  "source": "vendor_official"},

    # ========== SiliconFlow（硅基流动）上的 Qwen / GLM / DeepSeek ==========
    "deepseek-ai/deepseek-r1":       {"context_limit": 200000, "supports_thinking": True,
                                       "thinking_param": "thinking_budget", "source": "vendor_official"},
    "deepseek-ai/deepseek-v2-chat":  {"context_limit": 32000,  "source": "vendor_official"},
    "thudm/glm4-9b-chat":            {"context_limit": 32768,  "source": "vendor_official"},
    "qwen/qwen2-72b-instruct":       {"context_limit": 32768,  "source": "vendor_official"},

    # ========== Ollama（本地模型） ==========
    "llama3":         {"context_limit": 8192,   "source": "ollama_official", "note": "Meta Llama 3 8B 默认 8k"},
    "llama3.1":       {"context_limit": 131072, "source": "ollama_official", "note": "Meta Llama 3.1"},
    "qwen2.5":        {"context_limit": 32768,  "source": "ollama_official", "note": "Qwen2.5 默认 32k"},
    "qwen2.5-coder":  {"context_limit": 32768,  "source": "ollama_official"},
    "mistral":        {"context_limit": 32768,  "source": "ollama_official"},
    "phi3":           {"context_limit": 4096,   "source": "ollama_official"},
}


def get_model_capabilities(model_name: str) -> Dict[str, Any]:
    """按模型名精确查表，返回能力 dict；查不到返回空 dict。

    匹配规则：先按 strip 后的精确匹配，再按小写精确匹配。两种都查不到返回空。
    不会做任何前缀/子串猜测——找不到就找不到，让调用方走兜底。
    """
    if not model_name:
        return {}
    name = model_name.strip()
    if not name:
        return {}
    if name in MODEL_CAPABILITIES:
        return MODEL_CAPABILITIES[name]
    name_lower = name.lower()
    if name_lower in MODEL_CAPABILITIES:
        return MODEL_CAPABILITIES[name_lower]
    return {}


def resolve_context_limit(llm_config: Dict[str, Any], default: int = 128000) -> int:
    """统一查找上下文窗口（tokens）。

    优先级（高 -> 低）：
        L1: llm_config 显式填的 最大Token / context_limit / 上下文长度 / max_context_tokens
        L2: MODEL_CAPABILITIES[模型名].context_limit
        L3: FREE_PROVIDERS[provider_name].最大Token
        L4: PROVIDER_CAPABILITIES[family].context_limit（由 get_provider_profile 提供）
        兜底: default

    返回值始终 >= 1。
    """
    if not isinstance(llm_config, dict):
        return max(1, int(default))

    # L1: 显式值
    for key in _CONTEXT_LIMIT_KEYS:
        value = llm_config.get(key)
        if value not in (None, ""):
            try:
                return max(1, int(value))
            except (ValueError, TypeError):
                continue

    # L2: 模型名查表
    model = str(llm_config.get("模型名称", "") or "").strip()
    caps = get_model_capabilities(model)
    if caps.get("context_limit"):
        try:
            return max(1, int(caps["context_limit"]))
        except (ValueError, TypeError):
            pass

    # L3: 服务商默认
    provider = str(llm_config.get("provider_name", "") or "").strip()
    if provider and provider in FREE_PROVIDERS:
        v = FREE_PROVIDERS[provider].get("最大Token")
        if v not in (None, ""):
            try:
                return max(1, int(v))
            except (ValueError, TypeError):
                pass

    # L4: family 兜底
    profile = get_provider_profile(llm_config)
    try:
        return max(1, int(profile.get("context_limit", default)))
    except (ValueError, TypeError):
        return max(1, int(default))


def resolve_max_output_tokens(llm_config: Dict[str, Any], default: int = 4096) -> int:
    """统一查找「最大输出 tokens」。

    优先级：
        L1: llm_config 显式填的 最大新Token / max_tokens / max_output_tokens
        L2: provider_profile.max_output_tokens
        兜底: default

    注：api_param="max_tokens" 对应"最大输出 token 数"，schema 上把它 display_name
    写成"上下文长度"是历史遗留问题（见 PARAM_SCHEMA.最大Token）。本函数读取的是
    真实含义——输出上限。
    """
    if not isinstance(llm_config, dict):
        return max(1, int(default))

    for key in ("最大新Token", "max_tokens", "max_output_tokens"):
        value = llm_config.get(key)
        if value not in (None, ""):
            try:
                return max(1, int(value))
            except (ValueError, TypeError):
                continue

    profile = get_provider_profile(llm_config)
    try:
        return max(1, int(profile.get("max_output_tokens", default)))
    except (ValueError, TypeError):
        return max(1, int(default))


def apply_model_defaults(config: Dict[str, Any], model_name: str) -> Dict[str, Any]:
    """对 config 字典叠加上模型默认值，返回新 dict（不修改原对象）。

    合并顺序（低 → 高）：
        L1: DEFAULT_MODEL_PARAMS        （硬编码兜底）
        L2: config 中已有的值           （已保存/saved_providers/FREE_PROVIDERS 默认）
        L3: MODEL_CAPABILITIES[模型名]  （模型固有能力，覆盖 L1/L2 中对应的键）
        ─ 后续在 _load_model_config_to_card 中还有 model_overrides（最高）

    所以最终优先级：model_overrides > 模型能力 > config > 硬编码兜底

    用途：当服务商不在 FREE_PROVIDERS（自定义服务商）时，
    确保 UI 能看到合理的默认值（温度 0.7、top_p 1.0 等）。
    当服务商已知但模型能力更强时，模型能力会覆盖服务商默认（最大Token 等）。
    """
    result = {}
    # L1: 硬编码兜底
    result.update(DEFAULT_MODEL_PARAMS)
    # L2: config 已有值（saved_providers + FREE_PROVIDERS 默认）
    result.update(config)
    # L3: 模型能力（覆盖前两层，之后 model_overrides 还会覆盖回来）
    caps = get_model_capabilities(model_name)
    if caps.get("context_limit"):
        result["最大Token"] = caps["context_limit"]
        result["上下文长度"] = caps["context_limit"]
        if caps.get("supports_thinking"):
            # 仅在 config 还没显式设置时填默认（避免覆盖用户的 model_overrides）
            if "思考模式" not in result:
                result["思考模式"] = True
            if "思考等级" not in result:
                result["思考等级"] = "medium"
        else:
            # 模型不支持思考 → 主动移除思考相关字段
            # （用户如果之前在 model_overrides 里显式开过，会在 _load_model_config_to_card 后续被补回）
            result.pop("思考模式", None)
            result.pop("思考等级", None)
            result.pop("思考预算", None)
    return result
