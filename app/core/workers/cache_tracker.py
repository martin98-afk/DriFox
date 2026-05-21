# -*- coding: utf-8 -*-
"""
缓存命中率追踪器 - CacheHitRateTracker

用于追踪 LLM API 的缓存命中情况，包括：
- cache_read_input_tokens: 从缓存读取的 token 数
- cache_creation_input_tokens: 写入缓存的 token 数
- input_tokens: 非缓存的输入 token 数
- hit_rate: 缓存命中率

支持 OpenAI 和 Anthropic 格式的 usage 数据。
"""

from dataclasses import dataclass, field
from typing import Dict, Optional, List
from datetime import datetime
from loguru import logger


@dataclass
class CacheStats:
    """单次请求的缓存统计"""
    request_time: str = ""
    prompt_tokens: int = 0           # 非缓存的输入 token
    completion_tokens: int = 0       # 输出 token
    cache_read_tokens: int = 0       # 从缓存读取的 token
    cache_creation_tokens: int = 0   # 写入缓存的 token (5min TTL)
    cache_creation_1h_tokens: int = 0  # 写入缓存的 token (1h TTL)
    
    @property
    def hit_rate(self) -> float:
        """计算命中率（基于 token）"""
        cacheable = self.cache_read_tokens + self.cache_creation_tokens + self.cache_creation_1h_tokens
        if cacheable == 0:
            return 0.0
        return self.cache_read_tokens / cacheable
    
    @property
    def total_input_tokens(self) -> int:
        """总输入 token = 缓存读取 + 非缓存 + 缓存写入"""
        return self.cache_read_tokens + self.prompt_tokens + self.cache_creation_tokens + self.cache_creation_1h_tokens
    
    def to_dict(self) -> Dict:
        return {
            "request_time": self.request_time,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_creation_1h_tokens": self.cache_creation_1h_tokens,
            "hit_rate": self.hit_rate,
            "total_input_tokens": self.total_input_tokens,
        }


@dataclass
class AggregatedCacheStats:
    """聚合的缓存统计"""
    requests: int = 0
    prompt_tokens: int = 0           # 非缓存输入
    completion_tokens: int = 0      # 输出
    cache_read_tokens: int = 0       # 缓存读取
    cache_creation_5m_tokens: int = 0  # 5分钟缓存写入
    cache_creation_1h_tokens: int = 0  # 1小时缓存写入
    
    # 详细日志
    request_logs: List[CacheStats] = field(default_factory=list)
    
    def add(self, stats: CacheStats):
        """添加单次统计"""
        self.requests += 1
        self.prompt_tokens += stats.prompt_tokens
        self.completion_tokens += stats.completion_tokens
        self.cache_read_tokens += stats.cache_read_tokens
        self.cache_creation_5m_tokens += stats.cache_creation_tokens
        self.cache_creation_1h_tokens += stats.cache_creation_1h_tokens
        
        # 保留最近 100 条日志
        if len(self.request_logs) < 100:
            self.request_logs.append(stats)
    
    @property
    def hit_rate(self) -> float:
        """计算总命中率"""
        cacheable = self.cache_read_tokens + self.cache_creation_5m_tokens + self.cache_creation_1h_tokens
        if cacheable == 0:
            return 0.0
        return self.cache_read_tokens / cacheable
    
    @property
    def cache_savings_rate(self) -> float:
        """计算缓存节省率（读取 vs 写入成本）"""
        if self.cache_read_tokens == 0:
            return 0.0
        total_writes = self.cache_creation_5m_tokens + self.cache_creation_1h_tokens
        # 缓存读取成本是写入的约 1/10，节省比例 = (write - read) / write
        if total_writes == 0:
            return 0.0
        # 简化计算：节省 = 读取 / (读取 + 写入 * 0.1) * 100%
        return self.cache_read_tokens / (self.cache_read_tokens + total_writes * 0.1) * 100
    
    def cost_usd(
        self,
        base_input_per_mtok: float = 3.0,
        output_per_mtok: float = 15.0,
    ) -> float:
        """
        计算总成本（美元）
        
        Claude Sonnet 4.6 参考价格:
        - 基础输入: $3.00/MTok
        - 缓存写入(5min): 1.25x = $3.75/MTok
        - 缓存写入(1h): 2.0x = $6.00/MTok
        - 缓存读取: 0.10x = $0.30/MTok
        - 输出: $15.00/MTok
        """
        write_5m = self.cache_creation_5m_tokens * base_input_per_mtok * 1.25
        write_1h = self.cache_creation_1h_tokens * base_input_per_mtok * 2.0
        reads = self.cache_read_tokens * base_input_per_mtok * 0.10
        base = self.prompt_tokens * base_input_per_mtok
        out = self.completion_tokens * output_per_mtok
        
        return (write_5m + write_1h + reads + base + out) / 1_000_000
    
    def cost_without_cache_usd(
        self,
        base_input_per_mtok: float = 3.0,
        output_per_mtok: float = 15.0,
    ) -> float:
        """
        计算无缓存情况下的理论成本
        """
        total_input = self.cache_read_tokens + self.prompt_tokens + self.cache_creation_5m_tokens + self.cache_creation_1h_tokens
        base = total_input * base_input_per_mtok
        out = self.completion_tokens * output_per_mtok
        return (base + out) / 1_000_000
    
    def summary(self) -> str:
        """生成统计摘要"""
        savings = self.cost_without_cache_usd() - self.cost_usd()
        return f"""📊 Cache Statistics
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Requests:        {self.requests}
Hit Rate:        {self.hit_rate:.1%}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tokens:
  ├─ Cache Reads:     {self.cache_read_tokens:>10,} 
  ├─ Cache Writes (5m):{self.cache_creation_5m_tokens:>10,} 
  ├─ Cache Writes (1h):{self.cache_creation_1h_tokens:>10,} 
  ├─ Base Input:      {self.prompt_tokens:>10,} 
  └─ Output:          {self.completion_tokens:>10,} 
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Cost (Claude Sonnet 4.6):
  ├─ With Cache:      ${self.cost_usd():.4f}
  ├─ Without Cache:   ${self.cost_without_cache_usd():.4f}
  └─ Savings:         ${savings:.4f} ({savings/self.cost_without_cache_usd()*100:.1f}%)
"""
    
    def to_dict(self) -> Dict:
        return {
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_5m_tokens": self.cache_creation_5m_tokens,
            "cache_creation_1h_tokens": self.cache_creation_1h_tokens,
            "hit_rate": self.hit_rate,
            "cost_usd": self.cost_usd(),
            "cost_without_cache_usd": self.cost_without_cache_usd(),
        }
    
    def reset(self):
        """重置统计"""
        self.requests = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_5m_tokens = 0
        self.cache_creation_1h_tokens = 0
        self.request_logs.clear()


class CacheHitRateTracker:
    """
    缓存命中率追踪器
    
    用法:
        tracker = CacheHitRateTracker()
        tracker.start_session()
        
        # 每次 API 调用后记录 usage
        tracker.record_usage(response.usage)
        
        # 获取当前会话的统计
        stats = tracker.get_session_stats()
        print(stats.summary())
    """
    
    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._session_stats = AggregatedCacheStats()
        self._last_stats: Optional[CacheStats] = None
    
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
        """开始新的追踪会话"""
        self._session_stats.reset()
        self._last_stats = None
        logger.info("[CacheTracker] Session started")
    
    def end_session(self) -> AggregatedCacheStats:
        """结束当前会话，返回统计"""
        stats = self._session_stats
        logger.info(f"[CacheTracker] Session ended - {stats.summary()}")
        return stats
    
    def record_usage(self, usage: any) -> Optional[CacheStats]:
        """
        记录一次 API 调用的 usage 数据
        
        Args:
            usage: API 响应中的 usage 对象
                   - OpenAI 格式: {"prompt_tokens": ..., "completion_tokens": ..., ...}
                   - Anthropic 格式: {"input_tokens": ..., "output_tokens": ..., ...}
        
        Returns:
            CacheStats: 本次请求的缓存统计
        """
        if not self._enabled:
            return None
        
        stats = self._parse_usage(usage)
        if stats:
            self._session_stats.add(stats)
            self._last_stats = stats
        return stats
    
    def record_usage_dict(self, usage_dict: Dict) -> Optional[CacheStats]:
        """直接传入 usage 字典"""
        if not self._enabled:
            return None
        
        stats = self._parse_usage_dict(usage_dict)
        if stats:
            self._session_stats.add(stats)
            self._last_stats = stats
        
        return stats
    
    def _parse_usage(self, usage: any) -> Optional[CacheStats]:
        """解析 usage 对象（可能是一个对象或字典）"""
        if usage is None:
            return None
        
        # 处理字典格式
        if isinstance(usage, dict):
            return self._parse_usage_dict(usage)
        
        # 处理对象格式（OpenAI SDK）
        stats = CacheStats()
        stats.request_time = datetime.now().strftime("%H:%M:%S")
        
        # 尝试不同的属性名
        stats.prompt_tokens = getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0)
        stats.completion_tokens = getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0)
        
        # Anthropic 特有字段
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        cache_creation = getattr(usage, "cache_creation_input_tokens", None)
        
        # 处理新版 SDK (0.42+) 的结构
        cache_creation_obj = getattr(usage, "cache_creation", None)
        if cache_creation_obj:
            stats.cache_read_tokens = getattr(cache_creation_obj, "ephemeral_5m_input_tokens", 0) or 0
            stats.cache_read_tokens = getattr(cache_creation_obj, "ephemeral_1h_input_tokens", 0) or 0
        
        # 直接字段
        if cache_read is not None:
            stats.cache_read_tokens = cache_read
        if cache_creation is not None:
            stats.cache_creation_tokens = cache_creation
        
        # 旧版 Anthropic 可能只有 cache_creation
        if stats.cache_creation_tokens > 0 and stats.cache_read_tokens == 0:
            # 说明这是第一次写入，cache_read 应该是 0
            pass
        
        return stats
    
    def _parse_usage_dict(self, usage_dict: Dict) -> Optional[CacheStats]:
        """解析 usage 字典"""
        if not usage_dict:
            return None
        
        stats = CacheStats()
        stats.request_time = datetime.now().strftime("%H:%M:%S")
        
        # OpenAI 格式
        if "prompt_tokens" in usage_dict:
            stats.prompt_tokens = usage_dict.get("prompt_tokens", 0)
            stats.completion_tokens = usage_dict.get("completion_tokens", 0)
            
            # OpenAI 可能包含 prompt_tokens_details
            details = usage_dict.get("prompt_tokens_details", {})
            if details:
                # cached_tokens 表示从缓存读取的 token
                stats.cache_read_tokens = details.get("cached_tokens", 0)
        
        # Anthropic 格式
        elif "input_tokens" in usage_dict:
            stats.prompt_tokens = usage_dict.get("input_tokens", 0)
            stats.completion_tokens = usage_dict.get("output_tokens", 0)
            
            stats.cache_read_tokens = usage_dict.get("cache_read_input_tokens", 0) or 0
            stats.cache_creation_tokens = usage_dict.get("cache_creation_input_tokens", 0) or 0
            
            # 处理 cache_creation 对象（新版 SDK）
            cache_creation = usage_dict.get("cache_creation", {})
            if isinstance(cache_creation, dict):
                stats.cache_read_tokens = cache_creation.get("ephemeral_5m_input_tokens", 0) or stats.cache_read_tokens
                stats.cache_creation_1h_tokens = cache_creation.get("ephemeral_1h_input_tokens", 0) or 0
        
        return stats
    
    def get_session_stats(self) -> AggregatedCacheStats:
        """获取当前会话的聚合统计"""
        return self._session_stats
    
    def get_last_stats(self) -> Optional[CacheStats]:
        """获取最后一次请求的统计"""
        return self._last_stats
    
    def get_current_hit_rate(self) -> float:
        """获取当前会话的命中率"""
        return self._session_stats.hit_rate
    
    def get_hit_rate_display(self) -> str:
        """获取格式化的命中率显示字符串"""
        rate = self._session_stats.hit_rate
        if rate == 0:
            return "N/A"
        return f"{rate:.1%}"
    
    def summary(self) -> str:
        """生成当前会话的统计摘要"""
        return self._session_stats.summary()


# 全局单例（可选）
_global_tracker: Optional[CacheHitRateTracker] = None

def get_global_tracker() -> CacheHitRateTracker:
    """获取全局缓存追踪器"""
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = CacheHitRateTracker()
    return _global_tracker