# -*- coding: utf-8 -*-
"""
智能重试助手 - SmartRetryHelper

基于 ErrorClassifier 的智能重试机制：
1. 使用结构化错误分类决定恢复策略
2. 支持上下文压缩后重试
3. 支持凭据轮换后重试
4. 支持模型切换后重试

参考: https://github.com/NousResearch/hermes-agent/blob/main/agent/error_classifier.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, TypeVar

from loguru import logger

from app.core.workers.error_handler.error_classifier import (
    ErrorClassifier,
    FailoverReason,
    ClassifiedError,
    get_error_classifier,
)

T = TypeVar("T")


@dataclass
class RetryConfig:
    """重试配置"""
    max_retries: int = 15
    base_delay: float = 1.0
    max_delay: float = 60.0
    backoff_multiplier: float = 2.0
    jitter: bool = True
    
    # 是否启用智能恢复
    enable_context_compression: bool = True
    enable_credential_rotation: bool = False
    enable_model_fallback: bool = False
    
    # 取消检查回调（返回 True 表示应取消重试）
    cancel_check: Optional[Callable[[], bool]] = None


@dataclass
class RetryResult:
    """重试结果"""
    success: bool
    result: Any = None
    error: Optional[Exception] = None
    classified_error: Optional[ClassifiedError] = None
    attempts: int = 0
    used_compression: bool = False
    used_rotation: bool = False
    used_fallback: bool = False


class SmartRetryHelper:
    """
    智能重试助手
    
    使用 ErrorClassifier 分类错误并选择最佳恢复策略：
    - context_overflow: 压缩上下文后重试
    - rate_limit: 退避后重试
    - timeout: 等待后重试
    - auth: 轮换凭据后重试
    - model_not_found: 切换模型后重试
    - 其他: 带退避重试
    
    使用方式：
        helper = SmartRetryHelper(
            error_classifier=classifier,
            config=RetryConfig(max_retries=5),
        )
        
        result = helper.execute_with_retry(
            lambda: api_call(),
            context_provider=lambda: get_current_messages(),
            on_compression_needed=lambda: compress_and_retry(),
            on_rotation_needed=lambda: rotate_credentials(),
            on_fallback_needed=lambda: switch_model(),
        )
    """
    
    def __init__(
        self,
        error_classifier: Optional[ErrorClassifier] = None,
        config: Optional[RetryConfig] = None,
    ):
        self._classifier = error_classifier or get_error_classifier()
        self._config = config or RetryConfig()
        
        # 统计
        self._stats = {
            "total_attempts": 0,
            "successful_retries": 0,
            "compression_used": 0,
            "rotation_used": 0,
            "fallback_used": 0,
            "failures_by_reason": {},
        }
    
    def execute_with_retry(
        self,
        make_request: Callable[[], T],
        *,
        provider: str = "",
        model: str = "",
        approx_tokens: int = 0,
        context_length: int = 128000,
        num_messages: int = 0,
        # 回调函数
        on_compression_needed: Optional[Callable[[], None]] = None,
        on_rotation_needed: Optional[Callable[[], None]] = None,
        on_fallback_needed: Optional[Callable[[], None]] = None,
    ) -> RetryResult:
        """
        执行带智能重试的请求
        
        Args:
            make_request: 执行 API 调用的函数
            provider: 当前 provider
            model: 当前模型
            approx_tokens: 近似 token 数
            context_length: 上下文长度
            num_messages: 消息数量
            on_compression_needed: 需要压缩上下文时的回调
            on_rotation_needed: 需要轮换凭据时的回调
            on_fallback_needed: 需要切换模型时的回调
        
        Returns:
            RetryResult: 包含执行结果和元数据
        """
        last_error: Optional[Exception] = None
        compression_attempted = False
        rotation_attempted = False
        fallback_attempted = False
        compression_applied = False
        
        for attempt in range(self._config.max_retries + 1):
            self._stats["total_attempts"] += 1
            
            # 🛡️ 检查取消
            if self._config.cancel_check and self._config.cancel_check():
                logger.info("[SmartRetry] 检测到取消，放弃重试")
                return RetryResult(
                    success=False,
                    error=last_error or Exception("Cancelled by user"),
                    classified_error=classified if 'classified' in locals() else None,
                    attempts=attempt + 1,
                    used_compression=compression_applied,
                    used_rotation=rotation_attempted,
                    used_fallback=fallback_attempted,
                )
            
            try:
                result = make_request()
                if attempt > 0:
                    self._stats["successful_retries"] += 1
                return RetryResult(
                    success=True,
                    result=result,
                    attempts=attempt + 1,
                    used_compression=compression_applied,
                    used_rotation=rotation_attempted,
                    used_fallback=fallback_attempted,
                )
                
            except Exception as e:
                last_error = e
                
                # 分类错误
                classified = self._classifier.classify(
                    e,
                    provider=provider,
                    model=model,
                    approx_tokens=approx_tokens,
                    context_length=context_length,
                    num_messages=num_messages,
                )
                
                # 记录错误类型统计
                reason_key = classified.reason.value
                self._stats["failures_by_reason"][reason_key] = \
                    self._stats["failures_by_reason"].get(reason_key, 0) + 1
                
                # 不需要重试
                if not classified.should_retry:
                    logger.warning(
                        f"[SmartRetry] 错误 {classified.reason.value} 不需要重试: {str(e)[:100]}"
                    )
                    return RetryResult(
                        success=False,
                        error=e,
                        classified_error=classified,
                        attempts=attempt + 1,
                        used_compression=compression_applied,
                        used_rotation=rotation_attempted,
                        used_fallback=fallback_attempted,
                    )
                
                # 处理智能恢复
                if classified.should_compress and not compression_applied:
                    # 需要压缩上下文
                    compression_applied = True
                    if on_compression_needed and self._config.enable_context_compression:
                        logger.info(
                            f"[SmartRetry] 检测到上下文溢出，执行压缩: {classified.reason.value}"
                        )
                        on_compression_needed()
                        self._stats["compression_used"] += 1
                        compression_attempted = True
                        continue  # 压缩后重试
                
                if classified.should_rotate_credential and not rotation_attempted:
                    # 需要轮换凭据
                    if on_rotation_needed and self._config.enable_credential_rotation:
                        logger.info("[SmartRetry] 需要轮换凭据")
                        on_rotation_needed()
                        self._stats["rotation_used"] += 1
                        rotation_attempted = True
                        continue
                
                if classified.should_fallback and not fallback_attempted:
                    # 需要切换模型
                    if on_fallback_needed and self._config.enable_model_fallback:
                        logger.info("[SmartRetry] 需要切换模型")
                        on_fallback_needed()
                        self._stats["fallback_used"] += 1
                        fallback_attempted = True
                        continue
                
                # 普通的退避重试
                if attempt < self._config.max_retries:
                    delay = self._calculate_delay(attempt, classified)
                    logger.warning(
                        f"[SmartRetry] {classified.reason.value} 错误，"
                        f"等待 {delay:.1f}s 后重试 (attempt {attempt + 1}/{self._config.max_retries})"
                    )
                    if not self._cancelable_sleep(delay):
                        logger.info("[SmartRetry] 等待期间检测到取消，放弃重试")
                        return RetryResult(
                            success=False,
                            error=last_error,
                            classified_error=classified if 'classified' in locals() else None,
                            attempts=attempt + 1,
                            used_compression=compression_applied,
                            used_rotation=rotation_attempted,
                            used_fallback=fallback_attempted,
                        )
                else:
                    logger.error(
                        f"[SmartRetry] 重试次数耗尽，最后错误: {classified.reason.value}"
                    )
        
        return RetryResult(
            success=False,
            error=last_error,
            classified_error=classified if 'classified' in locals() else None,
            attempts=self._config.max_retries + 1,
            used_compression=compression_applied,
            used_rotation=rotation_attempted,
            used_fallback=fallback_attempted,
        )
    
    def _calculate_delay(
        self,
        attempt: int,
        classified: ClassifiedError,
    ) -> float:
        """根据错误类型和重试次数计算延迟"""
        # 根据错误类型调整基础延迟
        base = self._config.base_delay
        
        if classified.reason == FailoverReason.rate_limit:
            # 频率限制：更长的初始延迟
            base = max(base, 5.0)
        elif classified.reason == FailoverReason.overloaded:
            # 服务过载：较长延迟
            base = max(base, 3.0)
        elif classified.reason == FailoverReason.timeout:
            # 超时：较短延迟
            base = min(base, 1.0)
        elif classified.reason == FailoverReason.billing:
            # 计费问题：不重试
            base = float("inf")
        
        # 指数退避
        delay = base * (self._config.backoff_multiplier ** attempt)
        
        # 限制最大延迟
        delay = min(delay, self._config.max_delay)
        
        # 添加抖动
        if self._config.jitter:
            import random
            delay = delay * (0.5 + random.random())
        
        return delay
    
    def _cancelable_sleep(self, seconds: float, step: float = 0.5) -> bool:
        """可取消的 sleep，返回 True 表示正常完成，False 表示被取消"""
        elapsed = 0.0
        while elapsed < seconds:
            if self._config.cancel_check and self._config.cancel_check():
                return False
            time.sleep(min(step, seconds - elapsed))
            elapsed += step
        return True
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return dict(self._stats)
    
    def reset_stats(self) -> None:
        """重置统计"""
        self._stats = {
            "total_attempts": 0,
            "successful_retries": 0,
            "compression_used": 0,
            "rotation_used": 0,
            "fallback_used": 0,
            "failures_by_reason": {},
        }


# ============================================================================
# 便捷函数
# ============================================================================

def smart_retry(
    make_request: Callable[[], T],
    **kwargs,
) -> RetryResult:
    """
    使用全局 SmartRetryHelper 执行智能重试
    
    Args:
        make_request: API 调用函数
        **kwargs: 传递给 execute_with_retry 的参数
    
    Returns:
        RetryResult
    """
    helper = SmartRetryHelper()
    return helper.execute_with_retry(make_request, **kwargs)


# ============================================================================
# 与现有 retry_helper.py 的兼容层
# ============================================================================

def create_smart_api_call_with_retry(
    client,
    create_func: Callable,
    max_retries: int = 15,
    retry_delay: float = 5.0,
    provider: str = "",
    model: str = "",
    classifier: Optional[ErrorClassifier] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    **kwargs,
) -> Any:
    """
    增强版的 create_api_call_with_retry
    
    使用 ErrorClassifier 提供更智能的重试策略。
    
    Args:
        client: OpenAI 客户端
        create_func: 调用函数
        max_retries: 最大重试次数
        retry_delay: 基础延迟
        provider: Provider 名称
        model: 模型名称
        classifier: 错误分类器
        cancel_check: 取消检查回调（返回 True 表示应取消重试）
        **kwargs: 其他参数
    
    Returns:
        API 响应
    """
    classifier = classifier or get_error_classifier()
    
    config = RetryConfig(
        max_retries=max_retries,
        base_delay=retry_delay,
        enable_context_compression=True,
        enable_credential_rotation=False,
        enable_model_fallback=False,
        cancel_check=cancel_check,
    )
    
    helper = SmartRetryHelper(
        error_classifier=classifier,
        config=config,
    )
    
    def make_request():
        return create_func()
    
    result = helper.execute_with_retry(
        make_request,
        provider=provider,
        model=model,
    )
    
    if result.success:
        return result.result
    
    # 重试失败，抛出原始错误
    raise result.error


# 保留旧的函数名作为别名
def create_api_call_with_retry_enhanced(
    client,
    create_func,
    max_retries=15,
    retry_delay=5,
):
    """create_api_call_with_retry 的增强版本"""
    return create_smart_api_call_with_retry(
        client, create_func, max_retries, retry_delay
    )