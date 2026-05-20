# -*- coding: utf-8 -*-
"""
错误处理与重试模块

统一管理 API 错误分类和重试逻辑：
- error_classifier: 结构化错误分类
- retry_helper: 基础重试工具和装饰器
- smart_retry: 智能重试助手（基于错误分类）
"""

from app.core.workers.error_handler.error_classifier import (
    ErrorClassifier,
    ClassifiedError,
    FailoverReason,
    get_error_classifier,
)
from app.core.workers.error_handler.retry_helper import (
    retry_on_api_error,
    create_api_call_with_retry,
    is_retriable_error,
    classify_error,
    get_error_type_name,
    get_retry_delay,
)
from app.core.workers.error_handler.smart_retry import (
    RetryConfig,
    RetryResult,
    SmartRetryHelper,
    smart_retry,
    create_smart_api_call_with_retry,
)

__all__ = [
    # Error Classifier
    "ErrorClassifier",
    "ClassifiedError",
    "FailoverReason",
    "get_error_classifier",
    # Retry Helper
    "retry_on_api_error",
    "create_api_call_with_retry",
    "is_retriable_error",
    "classify_error",
    "get_error_type_name",
    "get_retry_delay",
    # Smart Retry
    "RetryConfig",
    "RetryResult",
    "SmartRetryHelper",
    "smart_retry",
    "create_smart_api_call_with_retry",
]