# -*- coding: utf-8 -*-
"""
缓存命中率追踪器

命中率口径（与服务商后台对齐，2026-08-21 重构）：

    hit_rate = cache_read / (cache_read + uncached_input)

两种 usage 语义统一到同一公式：
  - OpenAI 兼容（prompt_tokens 含 cached_tokens，无 write 概念）：
      uncached_input = prompt_tokens - cached_tokens
      → hit_rate = cached / prompt，与 OpenAI/Kimi/GLM/SiliconFlow 等后台一致
  - Anthropic（input_tokens 与缓存三段并列）：
      uncached_input = input_tokens + cache_creation_5m + cache_creation_1h
      → input≈0 时退化为官方公式 read / (read + write)
      参考: https://platform.claude.com/docs/en/build-with-claude/prompt-caching

历史教训：曾用「模型名前缀 + cached≥90%×prompt」判定数据可疑并对 OpenAI 兼容
provider 强制估算（首次 read×30%，其余虚构为 cache_write），导致工具会话中
真实 99% 命中被拉低到 70% 左右。真实高命中恰恰是缓存工作良好的表现，
不应被改写；坏 provider 的数据以其自家后台为准，保持口径一致即可对账。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from loguru import logger

# ============================================================
# Anthropic 官方定价倍率 (v2025+)
# 参考: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
# ============================================================
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_READ_MULTIPLIER = 0.10

MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # model_key: { base_input_per_mtok, output_per_mtok }
    "claude-opus-4.7": {"input": 5.0, "output": 25.0},
    "claude-opus-4.6": {"input": 5.0, "output": 25.0},
    "claude-opus-4.5": {"input": 5.0, "output": 25.0},
    "claude-opus-4.1": {"input": 15.0, "output": 75.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4.6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4.5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-haiku-4.5": {"input": 1.0, "output": 5.0},
    "claude-haiku-3.5": {"input": 0.8, "output": 4.0},
}
DEFAULT_INPUT_PRICE = 3.0
DEFAULT_OUTPUT_PRICE = 15.0


def _get_pricing(model: str = "") -> Dict[str, float]:
    """获取模型定价，降级匹配"""
    key = model.lower().strip()
    # 精确匹配
    if key in MODEL_PRICING:
        return MODEL_PRICING[key]
    # 前缀匹配
    for model_key, prices in MODEL_PRICING.items():
        if key.startswith(model_key):
            return prices
    return {"input": DEFAULT_INPUT_PRICE, "output": DEFAULT_OUTPUT_PRICE}


# ============================================================
# CacheStats - 单次请求的缓存统计
# ============================================================


@dataclass
class CacheStats:
    """单次请求的缓存统计"""

    request_time: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    # True: OpenAI 语义（prompt_tokens 已含 cached_tokens）
    # False: Anthropic 语义（input_tokens 与缓存三段并列，互不重叠）
    input_includes_cache: bool = True

    @property
    def cache_creation_tokens(self) -> int:
        """兼容旧访问方式：5m + 1h 总写入"""
        return self.cache_creation_5m_tokens + self.cache_creation_1h_tokens

    @property
    def cacheable_tokens(self) -> int:
        """可缓存的 token 总数 (read + write)"""
        return self.cache_read_tokens + self.cache_creation_5m_tokens + self.cache_creation_1h_tokens

    @property
    def total_input_tokens(self) -> int:
        """总输入 token = read + 未命中输入（两种语义下均为真实总输入）"""
        return self.cache_read_tokens + self.uncached_input_tokens

    @property
    def uncached_input_tokens(self) -> int:
        """未从缓存命中的输入 token（新写入 + 非缓存部分）

        - OpenAI 语义：prompt 已含 cached，未命中 = prompt - cached
        - Anthropic 语义：input 与缓存并列，未命中 = input + write(5m/1h)
        """
        if self.input_includes_cache:
            return max(0, self.prompt_tokens - self.cache_read_tokens)
        return self.prompt_tokens + self.cache_creation_5m_tokens + self.cache_creation_1h_tokens

    @property
    def is_cache_hit(self) -> bool:
        """本次请求是否命中了缓存 (有任意 cache_read)"""
        return self.cache_read_tokens > 0

    @property
    def hit_rate(self) -> float:
        """缓存命中率 = cache_read / (cache_read + 未命中输入)

        OpenAI 兼容下即 cached/prompt（服务商后台口径）；
        Anthropic 下 input≈0 时退化为官方公式 read/(read+write)。
        """
        denom = self.cache_read_tokens + self.uncached_input_tokens
        if denom == 0:
            return 0.0
        return self.cache_read_tokens / denom

    @property
    def total_input_hit_rate(self) -> float:
        """总输入命中率 = cache_read / 真实总输入（与 hit_rate 同口径）

        旧公式 read/(read+write+prompt) 在 OpenAI 语义下 prompt 含 read
        被双算，导致该指标系统性偏低，已随命中率口径统一修正。
        """
        return self.hit_rate

    def to_dict(self) -> Dict:
        return {
            "request_time": self.request_time,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_5m_tokens": self.cache_creation_5m_tokens,
            "cache_creation_1h_tokens": self.cache_creation_1h_tokens,
            "hit_rate": self.hit_rate,
            "total_input_hit_rate": self.total_input_hit_rate,
            "total_input_tokens": self.total_input_tokens,
            "is_cache_hit": self.is_cache_hit,
        }


# ============================================================
# AggregatedCacheStats - 聚合的会话级缓存统计
# ============================================================


@dataclass
class AggregatedCacheStats:
    """聚合的缓存统计（按 token 计算）"""

    requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    uncached_input_tokens: int = 0

    request_logs: List[CacheStats] = field(default_factory=list)

    def add(self, stats: CacheStats):
        self.requests += 1
        if stats.is_cache_hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        self.prompt_tokens += stats.prompt_tokens
        self.completion_tokens += stats.completion_tokens
        self.cache_read_tokens += stats.cache_read_tokens
        self.cache_creation_5m_tokens += stats.cache_creation_5m_tokens
        self.cache_creation_1h_tokens += stats.cache_creation_1h_tokens
        self.uncached_input_tokens += stats.uncached_input_tokens

        if len(self.request_logs) < 100:
            self.request_logs.append(stats)

    @property
    def cacheable_tokens(self) -> int:
        return self.cache_read_tokens + self.cache_creation_5m_tokens + self.cache_creation_1h_tokens

    @property
    def hit_rate(self) -> float:
        """聚合命中率 = Σcache_read / (Σcache_read + Σ未命中输入)，详见类头注释"""
        denom = self.cache_read_tokens + self.uncached_input_tokens
        if denom == 0:
            return 0.0
        return self.cache_read_tokens / denom

    @property
    def total_input_hit_rate(self) -> float:
        """与 hit_rate 同口径（旧公式在 OpenAI 语义下分母双算 read，已修正）"""
        return self.hit_rate

    @property
    def per_request_hit_rate(self) -> float:
        """按请求次数计算的命中率：多少比例的请求命中了缓存"""
        if self.requests == 0:
            return 0.0
        return self.cache_hits / self.requests

    @property
    def cache_savings_rate(self) -> float:
        """缓存节省率"""
        if self.cache_read_tokens == 0:
            return 0.0
        total_writes = self.cache_creation_5m_tokens + self.cache_creation_1h_tokens
        if total_writes == 0:
            return 0.0
        return self.cache_read_tokens / (self.cache_read_tokens + total_writes * 0.1) * 100

    def cost_usd(self, model: str = "") -> float:
        prices = _get_pricing(model)
        bi = prices["input"]
        ou = prices["output"]
        w5 = self.cache_creation_5m_tokens * bi * CACHE_WRITE_5M_MULTIPLIER
        w1 = self.cache_creation_1h_tokens * bi * CACHE_WRITE_1H_MULTIPLIER
        rd = self.cache_read_tokens * bi * CACHE_READ_MULTIPLIER
        bs = self.prompt_tokens * bi
        ot = self.completion_tokens * ou
        return (w5 + w1 + rd + bs + ot) / 1_000_000

    def cost_without_cache_usd(self, model: str = "") -> float:
        prices = _get_pricing(model)
        bi = prices["input"]
        ou = prices["output"]
        total_input = (
            self.cache_read_tokens + self.prompt_tokens + self.cache_creation_5m_tokens + self.cache_creation_1h_tokens
        )
        return (total_input * bi + self.completion_tokens * ou) / 1_000_000

    def summary(self, model: str = "") -> str:
        savings = self.cost_without_cache_usd(model) - self.cost_usd(model)
        nocache = self.cost_without_cache_usd(model)
        savings_pct = (savings / nocache * 100) if nocache > 0 else 0.0
        return (
            "Cache Statistics\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Requests:        {self.requests}\n"
            f"  ├─ Hits:       {self.cache_hits}\n"
            f"  └─ Misses:     {self.cache_misses}\n"
            f"Token Hit Rate:  {self.hit_rate:.1%}\n"
            f"Per-Request Hit: {self.per_request_hit_rate:.1%}\n"
            f"Total-Input Hit: {self.total_input_hit_rate:.1%}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Tokens:\n"
            f"  ├─ Cache Reads:     {self.cache_read_tokens:>10,}\n"
            f"  ├─ Cache Writes 5m: {self.cache_creation_5m_tokens:>10,}\n"
            f"  ├─ Cache Writes 1h: {self.cache_creation_1h_tokens:>10,}\n"
            f"  ├─ Uncached Input:  {self.uncached_input_tokens:>10,}\n"
            f"  └─ Output:          {self.completion_tokens:>10,}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Cost With Cache:    ${self.cost_usd(model):.4f}\n"
            f"Cost Without Cache: ${nocache:.4f}\n"
            f"Savings:            ${savings:.4f} ({savings_pct:.1f}%)\n"
        )

    def to_dict(self) -> Dict:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_5m_tokens": self.cache_creation_5m_tokens,
            "cache_creation_1h_tokens": self.cache_creation_1h_tokens,
            "hit_rate": self.hit_rate,
            "per_request_hit_rate": self.per_request_hit_rate,
            "total_input_hit_rate": self.total_input_hit_rate,
            "cost_usd": self.cost_usd(),
            "cost_without_cache_usd": self.cost_without_cache_usd(),
        }

    def reset(self):
        self.requests = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_5m_tokens = 0
        self.cache_creation_1h_tokens = 0
        self.uncached_input_tokens = 0
        self.request_logs.clear()


# ============================================================
# CacheHitRateTracker - 缓存命中率追踪器
# ============================================================


class CacheHitRateTracker:
    """
    缓存命中率追踪器

    用法:
        tracker = CacheHitRateTracker()
        tracker.start_session()

        # 每次 API 调用后记录 usage
        tracker.record_usage(response.usage)
        tracker.record_usage(response.usage, model="claude-sonnet-4.6")

        # 获取当前会话的统计
        stats = tracker.get_session_stats()
        print(stats.summary())
    """

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._session_stats = AggregatedCacheStats()
        self._last_stats: Optional[CacheStats] = None
        self._last_model: str = ""
        # 服务商插件注入的缓存 usage 处理钩子（可选，无则走内置解析）
        self._usage_semantics: str = ""  # ""=自动检测；openai/anthropic/prompt_excludes_cache
        self._usage_normalizer = None  # (usage, model) -> 标准 dict | None

    def enable(self):
        self._enabled = True
        logger.info("[CacheTracker] Enabled")

    def disable(self):
        self._enabled = False
        logger.info("[CacheTracker] Disabled")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def start_session(self):
        self._session_stats.reset()
        self._last_stats = None
        self._last_model = ""
        logger.info("[CacheTracker] Session started")

    def end_session(self) -> AggregatedCacheStats:
        stats = self._session_stats
        logger.info(f"[CacheTracker] Session ended - {stats.summary()}")
        return stats

    def set_provider_hooks(self, semantics: str = "", normalizer=None) -> None:
        """注入服务商插件的缓存 usage 处理钩子（ProviderDef.usage_semantics/usage_normalizer）。

        semantics:
          ""                     → 自动检测（有 prompt_tokens 键=OpenAI 语义，否则 Anthropic）
          "openai"               → prompt_tokens 已含 cached_tokens
          "anthropic"            → input_tokens 与缓存三段并列
          "prompt_excludes_cache" → OpenAI 字段名但 prompt_tokens 不含 cached_tokens
        normalizer: (usage: dict|对象, model: str) -> 标准 dict | None
          标准 dict 键：prompt_tokens/completion_tokens/cached_tokens/
          cache_creation_5m/cache_creation_1h/input_includes_cache(bool)
          返回 None → 回退内置解析。优先级高于 semantics。
        """
        self._usage_semantics = semantics or ""
        self._usage_normalizer = normalizer

    def _apply_normalizer(self, usage: any) -> Optional[CacheStats]:
        """调用插件 normalizer，返回 None 表示回退内置解析"""
        if self._usage_normalizer is None:
            return None
        try:
            normalized = self._usage_normalizer(usage, self._last_model)
        except Exception as e:
            logger.warning(f"[CacheTracker] usage_normalizer 异常，回退内置解析: {e}")
            return None
        if not normalized:
            return None
        stats = CacheStats()
        stats.request_time = datetime.now().strftime("%H:%M:%S")
        stats.prompt_tokens = int(normalized.get("prompt_tokens", 0) or 0)
        stats.completion_tokens = int(normalized.get("completion_tokens", 0) or 0)
        stats.cache_read_tokens = int(normalized.get("cached_tokens", 0) or 0)
        stats.cache_creation_5m_tokens = int(normalized.get("cache_creation_5m", 0) or 0)
        stats.cache_creation_1h_tokens = int(normalized.get("cache_creation_1h", 0) or 0)
        stats.input_includes_cache = bool(normalized.get("input_includes_cache", True))
        return stats

    def _apply_semantics(self, stats: CacheStats) -> CacheStats:
        """按插件声明的 usage 语义修正 input_includes_cache（影响 uncached_input 计算）"""
        if self._usage_semantics == "openai":
            stats.input_includes_cache = True
        elif self._usage_semantics in ("anthropic", "prompt_excludes_cache"):
            stats.input_includes_cache = False
        return stats

    def record_usage(self, usage: any, model: str = "") -> Optional[CacheStats]:
        """
        记录一次 API 调用的 usage 数据

        Args:
            usage: API 响应中的 usage 对象
                   - OpenAI: { prompt_tokens, completion_tokens, prompt_tokens_details: { cached_tokens } }
                   - Anthropic: { input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens }
                   - Anthropic 新版: { ..., cache_creation: { ephemeral_5m_input_tokens, ephemeral_1h_input_tokens } }
            model: 模型名称，用于成本计算 (如 "claude-sonnet-4.6")
        """
        if not self._enabled:
            return None

        if model:
            self._last_model = model
        stats = self._apply_normalizer(usage)
        if stats is None:
            stats = self._parse_usage(usage)
            if stats and self._usage_semantics:
                self._apply_semantics(stats)
        if stats:
            stats.model = self._last_model
            self._session_stats.add(stats)
            self._last_stats = stats
        return stats

    def record_usage_dict(self, usage_dict: Dict, model: str = "") -> Optional[CacheStats]:
        """直接传入 usage 字典"""
        if not self._enabled:
            return None

        if model:
            self._last_model = model
        stats = self._apply_normalizer(usage_dict)
        if stats is None:
            stats = self._parse_usage_dict(usage_dict)
            if stats and self._usage_semantics:
                self._apply_semantics(stats)
        if stats:
            stats.model = self._last_model
            self._session_stats.add(stats)
            self._last_stats = stats
        return stats

    def _parse_usage(self, usage: any) -> Optional[CacheStats]:
        """解析 usage 对象"""
        if usage is None:
            return None

        if isinstance(usage, dict):
            return self._parse_usage_dict(usage)

        stats = CacheStats()
        stats.request_time = datetime.now().strftime("%H:%M:%S")

        stats.prompt_tokens = getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
        stats.completion_tokens = getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
        # OpenAI SDK usage 有 prompt_tokens（含 cached）；Anthropic SDK 只有 input_tokens（与缓存并列）
        stats.input_includes_cache = getattr(usage, "prompt_tokens", None) is not None

        # OpenAI: prompt_tokens_details.cached_tokens
        details = getattr(usage, "prompt_tokens_details", None)
        if details:
            stats.cache_read_tokens = getattr(details, "cached_tokens", 0) or 0

        # Anthropic 扁平字段
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        cache_creation_flat = getattr(usage, "cache_creation_input_tokens", None)
        if cache_read is not None:
            stats.cache_read_tokens = cache_read
        if cache_creation_flat is not None:
            stats.cache_creation_5m_tokens = cache_creation_flat

        # Anthropic 新版 SDK (0.42+) 结构: cache_creation = { ephemeral_5m_input_tokens, ephemeral_1h_input_tokens }
        # 优先级高于扁平字段
        cache_creation_obj = getattr(usage, "cache_creation", None)
        if cache_creation_obj is not None:
            e5 = getattr(cache_creation_obj, "ephemeral_5m_input_tokens", None)
            e1 = getattr(cache_creation_obj, "ephemeral_1h_input_tokens", None)
            if e5 is not None:
                stats.cache_creation_5m_tokens = e5
            if e1 is not None:
                stats.cache_creation_1h_tokens = e1

        return stats

    def _parse_usage_dict(self, usage_dict: Dict) -> Optional[CacheStats]:
        """解析 usage 字典"""
        if not usage_dict:
            return None

        stats = CacheStats()
        stats.request_time = datetime.now().strftime("%H:%M:%S")

        # --- OpenAI 格式 ---
        if "prompt_tokens" in usage_dict:
            stats.prompt_tokens = usage_dict.get("prompt_tokens", 0)
            stats.completion_tokens = usage_dict.get("completion_tokens", 0)
            details = usage_dict.get("prompt_tokens_details", {})
            if isinstance(details, dict):
                stats.cache_read_tokens = details.get("cached_tokens", 0)
            stats.input_includes_cache = True

        # --- Anthropic 格式 ---
        elif "input_tokens" in usage_dict:
            stats.prompt_tokens = usage_dict.get("input_tokens", 0)
            stats.completion_tokens = usage_dict.get("output_tokens", 0)
            stats.input_includes_cache = False

            # Anthropic 扁平字段
            stats.cache_read_tokens = usage_dict.get("cache_read_input_tokens", 0) or 0
            stats.cache_creation_5m_tokens = usage_dict.get("cache_creation_input_tokens", 0) or 0

            # Anthropic 新版结构 (优先级高于扁平字段)
            cc = usage_dict.get("cache_creation", {})
            if isinstance(cc, dict):
                e5 = cc.get("ephemeral_5m_input_tokens")
                e1 = cc.get("ephemeral_1h_input_tokens")
                if e5 is not None:
                    stats.cache_creation_5m_tokens = e5
                if e1 is not None:
                    stats.cache_creation_1h_tokens = e1

        # --- 通用格式: 直接尝试所有已知字段 ---
        else:
            stats.prompt_tokens = usage_dict.get("prompt_tokens", usage_dict.get("input_tokens", 0))
            stats.completion_tokens = usage_dict.get("completion_tokens", usage_dict.get("output_tokens", 0))
            stats.cache_read_tokens = usage_dict.get("cache_read_input_tokens", 0) or usage_dict.get("cached_tokens", 0)
            stats.cache_creation_5m_tokens = usage_dict.get("cache_creation_input_tokens", 0)

        return stats

    def get_session_stats(self) -> AggregatedCacheStats:
        return self._session_stats

    def get_last_stats(self) -> Optional[CacheStats]:
        return self._last_stats

    def get_current_hit_rate(self) -> float:
        return self._session_stats.hit_rate

    def get_current_per_request_hit_rate(self) -> float:
        return self._session_stats.per_request_hit_rate

    def get_current_total_input_hit_rate(self) -> float:
        return self._session_stats.total_input_hit_rate

    def get_hit_rate_display(self) -> str:
        """多维度命中率显示"""
        parts = []
        tr = self._session_stats.hit_rate
        pr = self._session_stats.per_request_hit_rate
        if tr > 0:
            parts.append(f"Token: {tr:.1%}")
        if pr > 0:
            parts.append(f"Req: {pr:.1%}")
        return " | ".join(parts) if parts else "N/A"

    def summary(self) -> str:
        return self._session_stats.summary()

    def set_model(self, model: str):
        """设置当前使用的模型"""
        self._last_model = model

    def get_last_model(self) -> str:
        return self._last_model


# 全局单例（可选）
_global_tracker: Optional[CacheHitRateTracker] = None


def get_global_tracker() -> CacheHitRateTracker:
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = CacheHitRateTracker()
    return _global_tracker
