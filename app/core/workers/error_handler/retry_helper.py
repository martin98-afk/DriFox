# -*- coding: utf-8 -*-
"""
API 调用重试辅助工具

提供可重试的错误检测和退避重试装饰器。

现在集成 ErrorClassifier 提供更智能的重试策略：
- 支持上下文压缩后重试
- 支持凭据轮换后重试
- 支持模型切换后重试
- 支持更精细的错误分类
"""

import time
import random
from functools import wraps
from typing import Callable, Any, Optional

from loguru import logger

# 尝试导入新的错误分类器
try:
    from app.core.workers.error_handler.error_classifier import (
        ErrorClassifier,
        ClassifiedError,
        FailoverReason,
        get_error_classifier,
    )

    _HAS_ERROR_CLASSIFIER = True
except ImportError:
    _HAS_ERROR_CLASSIFIER = False
    logger.warning("[RetryHelper] ErrorClassifier 未找到，使用基础重试逻辑")

# ============================================================================
# 错误类型定义（向后兼容）
# ============================================================================

_RETRIABLE_ERROR_TYPES = (
    "RateLimitError",  # 请求频率限制
    "InternalServerError",  # 服务器错误 (5xx)
    "APIConnectionError",  # 连接错误
    "APITimeoutError",  # 超时错误
)


def is_retriable_error(e: Exception) -> bool:
    """
    检查错误是否应该重试

    如果 ErrorClassifier 可用，使用更智能的判断。
    """
    if _HAS_ERROR_CLASSIFIER:
        try:
            classifier = get_error_classifier()
            classified = classifier.classify(e)
            return classified.should_retry and classified.retryable
        except Exception:
            pass

    # 回退到基础检查
    from openai import (
        RateLimitError,
        APIError,
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
    )

    is_rate_limit = isinstance(e, RateLimitError)
    is_server_overload = isinstance(e, APIError) and "2064" in str(e)
    is_server_error = isinstance(e, InternalServerError)
    is_connection_error = isinstance(e, APIConnectionError)
    is_timeout = isinstance(e, APITimeoutError)

    return (
            is_rate_limit
            or is_server_overload
            or is_server_error
            or is_connection_error
            or is_timeout
    )


def classify_error(e: Exception, **kwargs) -> Optional[ClassifiedError]:
    """
    对错误进行分类

    Returns:
        ClassifiedError 或 None（如果 ErrorClassifier 不可用）
    """
    if not _HAS_ERROR_CLASSIFIER:
        return None

    try:
        classifier = get_error_classifier()
        return classifier.classify(e, **kwargs)
    except Exception:
        return None


def get_error_type_name(e: Exception) -> str:
    """获取错误类型名称"""
    if _HAS_ERROR_CLASSIFIER:
        classified = classify_error(e)
        if classified:
            return classified.reason.value

    # 回退到基础检查
    from openai import (
        RateLimitError,
        APIError,
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
    )

    if isinstance(e, RateLimitError):
        return "RateLimit"
    elif isinstance(e, InternalServerError):
        return "ServerError"
    elif isinstance(e, APIError) and "2064" in str(e):
        return "ServerOverload"
    elif isinstance(e, APIConnectionError):
        return "Connection"
    elif isinstance(e, APITimeoutError):
        return "Timeout"
    return "Unknown"


def get_retry_delay(
        e: Exception,
        attempt: int,
        base_delay: float = 5.0,
) -> float:
    """
    根据错误类型和重试次数计算延迟

    Args:
        e: 异常
        attempt: 当前重试次数 (从 0 开始)
        base_delay: 基础延迟

    Returns:
        延迟秒数
    """
    if _HAS_ERROR_CLASSIFIER:
        classified = classify_error(e)
        if classified:
            # 根据错误类型调整基础延迟
            if classified.reason == FailoverReason.rate_limit:
                base_delay = max(base_delay, 5.0)
            elif classified.reason == FailoverReason.overloaded:
                base_delay = max(base_delay, 3.0)
            elif classified.reason == FailoverReason.timeout:
                base_delay = min(base_delay, 1.0)
            elif classified.reason == FailoverReason.billing:
                return float("inf")  # 计费问题不重试

    return base_delay * (2 ** attempt)


def retry_on_api_error(
        max_retries: int = 15,
        retry_delay: float = 5.0,
        backoff_multiplier: float = 1.0,
        enable_smart_retry: bool = True,
) -> Callable:
    """
    API调用重试装饰器

    Args:
        max_retries: 最大重试次数
        retry_delay: 基础重试延迟（秒）
        backoff_multiplier: 退避倍数，例如2表示5s, 10s, 20s
        enable_smart_retry: 是否启用智能重试（使用 ErrorClassifier）

    Usage:
        @retry_on_api_error(max_retries=3, retry_delay=5)
        def make_api_call():
            return client.chat.completions.create(...)
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_error: Optional[Exception] = None

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e

                    if not is_retriable_error(e):
                        # 智能重试：检查是否需要特殊处理
                        if enable_smart_retry and _HAS_ERROR_CLASSIFIER:
                            classified = classify_error(e)
                            if classified:
                                if classified.should_compress:
                                    logger.info(
                                        f"[API] 上下文溢出，需要压缩后重试"
                                    )
                                elif classified.should_rotate_credential:
                                    logger.warning(
                                        f"[API] 认证失败，需要轮换凭据"
                                    )
                                elif classified.should_fallback:
                                    logger.warning(
                                        f"[API] 模型不可用，需要切换模型"
                                    )
                        raise

                    if attempt < max_retries - 1:
                        # 使用智能延迟计算
                        wait_time = get_retry_delay(
                            e, attempt, retry_delay
                        ) * backoff_multiplier

                        # 添加随机抖动
                        wait_time = wait_time * (0.5 + random.random() * 0.5)

                        error_type = get_error_type_name(e)
                        logger.warning(
                            f"[API] {error_type} error, "
                            f"retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})"
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(
                            f"[API] {get_error_type_name(e)} error failed after {max_retries} attempts"
                        )

            raise last_error

        return wrapper

    return decorator


def create_api_call_with_retry(
        client,
        create_func: Callable,
        max_retries: int = 15,
        retry_delay: float = 5.0,
        enable_smart_retry: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
) -> Any:
    """
    执行带重试的API调用

    Args:
        client: OpenAI客户端
        create_func: 调用chat.completions.create的函数
        max_retries: 最大重试次数
        retry_delay: 基础重试延迟（秒）
        enable_smart_retry: 是否使用智能重试
        cancel_check: 取消检查回调（返回 True 表示应取消重试）

    Returns:
        API响应对象
    """
    last_error: Optional[Exception] = None
    compression_needed = False

    for attempt in range(max_retries):
        # 🛡️ 检查取消
        if cancel_check and cancel_check():
            logger.info("[RetryHelper] 检测到取消，放弃重试")
            raise last_error or Exception("Cancelled by user")

        try:
            return create_func()
        except Exception as e:
            last_error = e

            if not is_retriable_error(e):
                raise

            # 智能重试检查
            if enable_smart_retry and _HAS_ERROR_CLASSIFIER:
                classified = classify_error(e)
                if classified:
                    if classified.should_compress:
                        compression_needed = True
                        logger.info(
                            f"[API] 上下文溢出标记，尝试重试"
                        )
                    elif classified.reason == FailoverReason.billing:
                        # 计费问题不重试
                        logger.error("[API] 计费问题，终止重试")
                        raise

            if attempt < max_retries - 1:
                wait_time = get_retry_delay(e, attempt, retry_delay)

                # 添加随机抖动
                wait_time = wait_time * (0.5 + random.random() * 0.5)

                error_type = get_error_type_name(e)
                logger.warning(
                    f"[API] {error_type} error, "
                    f"retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                # 🛡️ 可取消的 sleep
                elapsed = 0.0
                step = 0.5
                while elapsed < wait_time:
                    if cancel_check and cancel_check():
                        logger.info("[RetryHelper] 等待期间检测到取消，放弃重试")
                        raise last_error or Exception("Cancelled by user")
                    time.sleep(min(step, wait_time - elapsed))
                    elapsed += step
            else:
                logger.error(
                    f"[API] {get_error_type_name(e)} error failed after {max_retries} attempts"
                )

    # 返回压缩标记，让调用者决定如何处理
    if compression_needed and last_error:
        logger.warning(
            "[API] 所有重试失败，但检测到上下文溢出可能"
        )

    raise last_error