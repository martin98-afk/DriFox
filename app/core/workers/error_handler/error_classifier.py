# -*- coding: utf-8 -*-
"""
API 错误分类器 - ErrorClassifier

基于 Hermes Agent 的成熟错误分类体系，提供：
1. 结构化的错误分类 (FailoverReason)
2. 优先级排序的分类管道
3. 恢复动作提示 (recovery hints)

参考: https://github.com/NousResearch/hermes-agent/blob/main/agent/error_classifier.py
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from loguru import logger

logger = logging.getLogger(__name__)


# ============================================================================
# 错误分类体系
# ============================================================================

class FailoverReason(enum.Enum):
    """
    API 调用失败原因分类，决定恢复策略。
    
    分类原则：
    - auth/auth_permanent: 认证问题 → 刷新/轮换凭据
    - billing/rate_limit: 配额问题 → 退避/轮换
    - overloaded/server_error: 服务端问题 → 重试
    - timeout: 传输问题 → 重建客户端重试
    - context_overflow: 上下文问题 → 压缩
    - model_not_found: 模型问题 → 切换模型
    - unknown: 未知问题 → 带退避重试
    """
    
    # 认证/授权
    auth = "auth"                       # 临时 401/403 → 刷新/轮换
    auth_permanent = "auth_permanent"    # 永久认证失败 → 终止
    
    # 计费/配额
    billing = "billing"                 # 402 或额度耗尽 → 立即轮换
    rate_limit = "rate_limit"           # 429 → 退避后轮换
    
    # 服务端
    overloaded = "overloaded"           # 503/529 → 退避重试
    server_error = "server_error"       # 500/502 → 重试
    
    # 传输
    timeout = "timeout"                 # 连接/读取超时 → 重建客户端
    
    # 上下文/负载
    context_overflow = "context_overflow"   # 上下文过长 → 压缩
    payload_too_large = "payload_too_large" # 413 → 压缩负载
    image_too_large = "image_too_large"     # 单图超限 → 缩小重试
    
    # 模型
    model_not_found = "model_not_found"     # 404 或无效模型 → 切换模型
    
    # 请求格式
    format_error = "format_error"           # 400 格式错误 → 终止或剥离重试
    
    # 捕获-all
    unknown = "unknown"                     # 不可分类 → 带退避重试


@dataclass
class ClassifiedError:
    """
    错误分类结果，包含恢复动作提示。
    
    恢复动作由调用方检查，而不是重新分类。
    """
    reason: FailoverReason
    status_code: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    message: str = ""
    error_context: Dict[str, Any] = field(default_factory=dict)
    
    # 恢复动作提示
    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False
    should_retry: bool = True
    
    @property
    def is_auth(self) -> bool:
        """是否认证相关错误"""
        return self.reason in {FailoverReason.auth, FailoverReason.auth_permanent}
    
    @property
    def is_billing(self) -> bool:
        """是否计费相关错误"""
        return self.reason == FailoverReason.billing
    
    @property
    def is_rate_limit(self) -> bool:
        """是否频率限制"""
        return self.reason == FailoverReason.rate_limit
    
    @property
    def is_context_overflow(self) -> bool:
        """是否上下文溢出"""
        return self.reason == FailoverReason.context_overflow
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "reason": self.reason.value,
            "status_code": self.status_code,
            "provider": self.provider,
            "model": self.model,
            "message": self.message[:200] if self.message else "",
            "retryable": self.retryable,
            "should_compress": self.should_compress,
            "should_rotate_credential": self.should_rotate_credential,
            "should_fallback": self.should_fallback,
            "should_retry": self.should_retry,
        }


# ============================================================================
# 错误模式匹配表
# ============================================================================

# 计费耗尽模式（非瞬时频率限制）
BILLING_PATTERNS = [
    "insufficient credits",
    "insufficient_quota",
    "insufficient balance",
    "credit balance",
    "credits have been exhausted",
    "top up your credits",
    "payment required",
    "billing hard limit",
    "exceeded your current quota",
    "account is deactivated",
    "plan does not include",
]

# 频率限制模式（瞬时，可恢复）
RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttled",
    "requests per minute",
    "tokens per minute",
    "requests per day",
    "try again in",
    "please retry after",
    "resource_exhausted",
    "rate increased too quickly",
    "throttlingexception",
    "too many concurrent requests",
    "servicequotaexceededexception",
]

# 上下文溢出模式
CONTEXT_OVERFLOW_PATTERNS = [
    "context length",
    "context size",
    "maximum context",
    "token limit",
    "too many tokens",
    "reduce the length",
    "exceeds the limit",
    "context window",
    "prompt is too long",
    "prompt exceeds max length",
    "max_tokens",
    "maximum number of tokens",
    # vLLM / 本地推理服务器
    "exceeds the max_model_len",
    "max_model_len",
    "prompt length",
    "input is too long",
    "maximum model length",
    # Ollama
    "context length exceeded",
    "truncating input",
    # llama.cpp
    "slot context",
    "n_ctx_slot",
    # 中文错误消息
    "超过最大长度",
    "上下文长度",
    # AWS Bedrock
    "max input token",
    "input token",
]

# 认证失败模式
AUTH_PATTERNS = [
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid token",
    "token expired",
    "token revoked",
    "access denied",
]

# 模型未找到模式
MODEL_NOT_FOUND_PATTERNS = [
    "is not a valid model",
    "invalid model",
    "model not found",
    "model_not_found",
    "does not exist",
    "no such model",
    "unknown model",
    "unsupported model",
]

# 超时模式
TIMEOUT_PATTERNS = [
    "timed out",
    "turn timed out",
    "request timed out",
    "deadline exceeded",
    "operation timed out",
    "upstream timed out",
]

# 传输错误类型
TRANSPORT_ERROR_TYPES = frozenset({
    "ReadTimeout",
    "ConnectTimeout",
    "PoolTimeout",
    "ConnectError",
    "RemoteProtocolError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "TimeoutError",
    "ReadError",
    "ServerDisconnectedError",
    "SSLError",
    "SSLZeroReturnError",
    "SSLWantReadError",
    "SSLWantWriteError",
    "SSLEOFError",
    "SSLSyscallError",
    "APIConnectionError",
    "APITimeoutError",
})

# 服务器断开模式（无状态码，但可能表示上下文溢出）
SERVER_DISCONNECT_PATTERNS = [
    "server disconnected",
    "peer closed connection",
    "connection reset by peer",
    "connection was closed",
    "network connection lost",
    "unexpected eof",
    "incomplete chunked read",
]

# SSL 临时失败模式（不应触发上下文压缩）
SSL_TRANSIENT_PATTERNS = [
    "bad record mac",
    "ssl alert",
    "tls alert",
    "ssl handshake failure",
    "tlsv1 alert",
    "sslv3 alert",
    "bad_record_mac",
    "ssl_alert",
    "tls_alert",
    "tls_alert_internal_error",
    "[ssl:",
]

# Payload 过大模式
PAYLOAD_TOO_LARGE_PATTERNS = [
    "request entity too large",
    "payload too large",
    "error code: 413",
]

# 图片过大模式
IMAGE_TOO_LARGE_PATTERNS = [
    "image exceeds",
    "image too large",
    "image_too_large",
    "image size exceeds",
]


# ============================================================================
# 错误分类器
# ============================================================================

class ErrorClassifier:
    """
    API 错误分类器
    
    提供优先级排序的分类管道，将异常分类为 FailoverReason，
    并提供恢复动作提示。
    
    使用方式：
        classifier = ErrorClassifier()
        
        try:
            response = make_api_call()
        except Exception as e:
            classified = classifier.classify(
                e,
                provider="openai",
                model="gpt-4",
                approx_tokens=50000,
                context_length=128000,
            )
            
            if classified.should_compress:
                # 压缩上下文后重试
                pass
            elif classified.should_rotate_credential:
                # 轮换凭据后重试
                pass
            elif classified.should_fallback:
                # 切换模型后重试
                pass
            elif classified.retryable:
                # 带退避重试
                pass
            else:
                # 不可恢复，终止
                raise
    """
    
    def __init__(self, quiet: bool = False):
        self._quiet = quiet
        self._stats = {
            "total": 0,
            "by_reason": {},
        }
    
    def classify(
        self,
        error: Exception,
        *,
        provider: str = "",
        model: str = "",
        approx_tokens: int = 0,
        context_length: int = 128000,
        num_messages: int = 0,
    ) -> ClassifiedError:
        """
        将异常分类为结构化的 ClassifiedError
        
        Args:
            error: API 调用抛出的异常
            provider: 当前 provider 名称 (如 "openai", "anthropic")
            model: 当前模型标识
            approx_tokens: 当前上下文的大致 token 数
            context_length: 模型最大上下文长度
            num_messages: 消息数量
            
        Returns:
            ClassifiedError: 结构化的错误分类
        """
        self._stats["total"] += 1
        
        # 提取错误信息
        status_code = self._extract_status_code(error)
        error_type = type(error).__name__
        body = self._extract_error_body(error)
        error_code = self._extract_error_code(body)
        
        # 构建完整错误消息用于模式匹配
        error_msg = self._build_error_message(error, body)
        
        provider_lower = (provider or "").strip().lower()
        model_lower = (model or "").strip().lower()
        
        # 创建结果工厂函数
        def _result(
            reason: FailoverReason,
            retryable: bool = True,
            should_compress: bool = False,
            should_rotate_credential: bool = False,
            should_fallback: bool = False,
            should_retry: bool = True,
        ) -> ClassifiedError:
            return ClassifiedError(
                reason=reason,
                status_code=status_code,
                provider=provider,
                model=model,
                message=self._extract_message(error, body),
                error_context={
                    "error_type": error_type,
                    "error_code": error_code,
                    "approx_tokens": approx_tokens,
                    "num_messages": num_messages,
                },
                retryable=retryable,
                should_compress=should_compress,
                should_rotate_credential=should_rotate_credential,
                should_fallback=should_fallback,
                should_retry=should_retry,
            )
        
        # 更新统计
        def _update_stats(reason: FailoverReason):
            reason_key = reason.value
            self._stats["by_reason"][reason_key] = \
                self._stats["by_reason"].get(reason_key, 0) + 1
        
        # ========== 1. HTTP 状态码分类 ==========
        if status_code is not None:
            classified = self._classify_by_status(
                status_code, error_msg, error_code, body,
                provider=provider_lower,
                model=model_lower,
                approx_tokens=approx_tokens,
                context_length=context_length,
                num_messages=num_messages,
                result_fn=_result,
            )
            if classified is not None:
                _update_stats(classified.reason)
                return classified
        
        # ========== 2. 错误码分类 ==========
        if error_code:
            classified = self._classify_by_error_code(
                error_code, error_msg, _result
            )
            if classified is not None:
                _update_stats(classified.reason)
                return classified
        
        # ========== 3. 消息模式分类 ==========
        classified = self._classify_by_message(
            error_msg, error_type,
            approx_tokens=approx_tokens,
            context_length=context_length,
            num_messages=num_messages,
            result_fn=_result,
        )
        if classified is not None:
            _update_stats(classified.reason)
            return classified
        
        # ========== 4. SSL 临时错误 → 超时重试（不压缩） ==========
        if any(p in error_msg for p in SSL_TRANSIENT_PATTERNS):
            result = _result(
                FailoverReason.timeout,
                retryable=True,
                should_compress=False,
            )
            _update_stats(result.reason)
            return result
        
        # ========== 5. 服务器断开 + 大会话 → 上下文溢出 ==========
        is_disconnect = any(p in error_msg for p in SERVER_DISCONNECT_PATTERNS)
        if is_disconnect and status_code is None:
            is_large_session = (
                approx_tokens > context_length * 0.7 or
                num_messages > 50
            )
            if is_large_session:
                result = _result(
                    FailoverReason.context_overflow,
                    retryable=True,
                    should_compress=True,
                    should_retry=False,  # 需要压缩后再重试
                )
                _update_stats(result.reason)
                return result
        
        # ========== 6. 传输错误类型 ==========
        if error_type in TRANSPORT_ERROR_TYPES:
            is_timeout_error = any(p in error_msg for p in TIMEOUT_PATTERNS)
            result = _result(
                FailoverReason.timeout if is_timeout_error else FailoverReason.unknown,
                retryable=True,
                should_compress=False,
            )
            _update_stats(result.reason)
            return result
        
        # ========== 7. 回退: 未知错误 ==========
        result = _result(
            FailoverReason.unknown,
            retryable=True,
            should_compress=False,
        )
        _update_stats(result.reason)
        
        if not self._quiet:
            logger.warning(
                f"[ErrorClassifier] 未知错误: {error_type}, "
                f"message: {error_msg[:100]}..."
            )
        
        return result
    
    def _extract_status_code(self, error: Exception) -> Optional[int]:
        """从错误中提取 HTTP 状态码"""
        # 优先从 error 对象的属性获取
        if hasattr(error, "status_code"):
            return getattr(error, "status_code", None)
        if hasattr(error, "response"):
            response = getattr(error, "response", None)
            if hasattr(response, "status_code"):
                return response.status_code
        
        # 尝试从错误消息解析
        error_str = str(error)
        
        # 匹配 "401" 或 "HTTP 401" 模式
        import re
        match = re.search(r"\b(4\d{2}|5\d{2})\b", error_str)
        if match:
            return int(match.group(1))
        
        return None
    
    def _extract_error_body(self, error: Exception) -> Dict[str, Any]:
        """提取错误响应体"""
        body = {}
        
        # 尝试从 response 对象获取
        if hasattr(error, "response"):
            response = getattr(error, "response", None)
            if hasattr(response, "body"):
                body = getattr(response, "body", {})
            elif hasattr(response, "text"):
                try:
                    body = json.loads(response.text or "{}")
                except (json.JSONDecodeError, TypeError):
                    pass
        
        # 尝试从 error body 获取
        if hasattr(error, "body"):
            body = getattr(error, "body", body)
        
        return body if isinstance(body, dict) else {}
    
    def _extract_error_code(self, body: Dict[str, Any]) -> Optional[str]:
        """从响应体提取错误码"""
        # OpenAI 格式
        if "error" in body:
            error_obj = body["error"]
            if isinstance(error_obj, dict):
                return error_obj.get("code")
        
        # 其他格式
        return body.get("code") or body.get("error_code")
    
    def _build_error_message(self, error: Exception, body: Dict) -> str:
        """构建用于模式匹配的完整错误消息"""
        parts = [str(error).lower()]
        
        # 添加 body message
        if body:
            body_msg = str(body.get("message", "")).lower()
            if body_msg and body_msg not in parts[0]:
                parts.append(body_msg)
            
            # 提取嵌套的 error.message
            if "error" in body:
                error_obj = body["error"]
                if isinstance(error_obj, dict):
                    nested_msg = str(error_obj.get("message", "")).lower()
                    if nested_msg and nested_msg not in parts[0]:
                        parts.append(nested_msg)
        
        return " ".join(parts)
    
    def _extract_message(self, error: Exception, body: Dict) -> str:
        """提取用户友好的错误消息"""
        if body:
            # 优先使用 body.message
            msg = body.get("message")
            if msg:
                return str(msg)
            
            # 尝试从 error 对象提取
            if "error" in body:
                error_obj = body["error"]
                if isinstance(error_obj, dict):
                    return str(error_obj.get("message", str(error)))
        
        return str(error)
    
    def _classify_by_status(
        self,
        status_code: int,
        error_msg: str,
        error_code: Optional[str],
        body: Dict,
        provider: str,
        model: str,
        approx_tokens: int,
        context_length: int,
        num_messages: int,
        result_fn,
    ) -> Optional[ClassifiedError]:
        """根据 HTTP 状态码分类"""
        
        if status_code == 400:
            # 格式错误或参数错误
            # 检查是否是上下文溢出（某些 provider 用 400 表示）
            if any(p in error_msg for p in CONTEXT_OVERFLOW_PATTERNS):
                return result_fn(
                    FailoverReason.context_overflow,
                    retryable=True,
                    should_compress=True,
                    should_retry=False,
                )
            
            # 图片过大
            if any(p in error_msg for p in IMAGE_TOO_LARGE_PATTERNS):
                return result_fn(
                    FailoverReason.image_too_large,
                    retryable=True,
                    should_compress=False,
                )
            
            # 普通格式错误
            return result_fn(
                FailoverReason.format_error,
                retryable=False,
                should_compress=False,
            )
        
        elif status_code == 401 or status_code == 403:
            # 认证失败
            if status_code == 401:
                return result_fn(
                    FailoverReason.auth,
                    retryable=True,
                    should_rotate_credential=True,
                    should_retry=False,
                )
            else:
                # 403 可能是永久的（权限不足）
                return result_fn(
                    FailoverReason.auth_permanent,
                    retryable=False,
                    should_rotate_credential=True,
                )
        
        elif status_code == 402:
            # 计费问题
            return result_fn(
                FailoverReason.billing,
                retryable=False,
                should_rotate_credential=True,
            )
        
        elif status_code == 413:
            # Payload 过大
            return result_fn(
                FailoverReason.payload_too_large,
                retryable=True,
                should_compress=True,
                should_retry=False,
            )
        
        elif status_code == 429:
            # 频率限制
            # 区分瞬时限速和额度耗尽
            if any(p in error_msg for p in BILLING_PATTERNS):
                return result_fn(
                    FailoverReason.billing,
                    retryable=False,
                    should_rotate_credential=True,
                )
            
            return result_fn(
                FailoverReason.rate_limit,
                retryable=True,
                should_compress=False,
                should_retry=True,
            )
        
        elif status_code == 500:
            return result_fn(
                FailoverReason.server_error,
                retryable=True,
                should_compress=False,
            )
        
        elif status_code in (502, 503, 529):
            return result_fn(
                FailoverReason.overloaded,
                retryable=True,
                should_compress=False,
            )
        
        return None
    
    def _classify_by_error_code(
        self,
        error_code: str,
        error_msg: str,
        result_fn,
    ) -> Optional[ClassifiedError]:
        """根据错误码分类"""
        if not error_code:
            return None
        
        code_lower = str(error_code).lower()
        
        if "rate_limit" in code_lower or "throttl" in code_lower:
            return result_fn(
                FailoverReason.rate_limit,
                retryable=True,
            )
        
        if "invalid_api_key" in code_lower or "auth" in code_lower:
            return result_fn(
                FailoverReason.auth,
                retryable=False,
                should_rotate_credential=True,
            )
        
        if "model_not_found" in code_lower or "invalid_model" in code_lower:
            return result_fn(
                FailoverReason.model_not_found,
                retryable=False,
                should_fallback=True,
            )
        
        return None
    
    def _classify_by_message(
        self,
        error_msg: str,
        error_type: str,
        approx_tokens: int,
        context_length: int,
        num_messages: int,
        result_fn,
    ) -> Optional[ClassifiedError]:
        """根据错误消息模式分类"""
        
        # 计费相关（优先于频率限制）
        if any(p in error_msg for p in BILLING_PATTERNS):
            return result_fn(
                FailoverReason.billing,
                retryable=False,
                should_rotate_credential=True,
            )
        
        # 上下文溢出
        if any(p in error_msg for p in CONTEXT_OVERFLOW_PATTERNS):
            return result_fn(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
                should_retry=False,
            )
        
        # Payload 过大
        if any(p in error_msg for p in PAYLOAD_TOO_LARGE_PATTERNS):
            return result_fn(
                FailoverReason.payload_too_large,
                retryable=True,
                should_compress=True,
                should_retry=False,
            )
        
        # 图片过大
        if any(p in error_msg for p in IMAGE_TOO_LARGE_PATTERNS):
            return result_fn(
                FailoverReason.image_too_large,
                retryable=True,
                should_compress=False,
            )
        
        # 模型未找到
        if any(p in error_msg for p in MODEL_NOT_FOUND_PATTERNS):
            return result_fn(
                FailoverReason.model_not_found,
                retryable=False,
                should_fallback=True,
            )
        
        # 认证失败
        if any(p in error_msg for p in AUTH_PATTERNS):
            return result_fn(
                FailoverReason.auth,
                retryable=False,
                should_rotate_credential=True,
            )
        
        # 超时
        if any(p in error_msg for p in TIMEOUT_PATTERNS):
            return result_fn(
                FailoverReason.timeout,
                retryable=True,
                should_compress=False,
            )
        
        # 频率限制
        if any(p in error_msg for p in RATE_LIMIT_PATTERNS):
            return result_fn(
                FailoverReason.rate_limit,
                retryable=True,
            )
        
        return None
    
    def get_stats(self) -> Dict[str, Any]:
        """获取分类统计"""
        return {
            "total": self._stats["total"],
            "by_reason": dict(self._stats["by_reason"]),
            "success_rate": (
                1 - self._stats["by_reason"].get("unknown", 0) / max(1, self._stats["total"])
            ),
        }
    
    def reset_stats(self) -> None:
        """重置统计"""
        self._stats = {"total": 0, "by_reason": {}}


# ============================================================================
# 全局单例
# ============================================================================

_classifier: Optional[ErrorClassifier] = None


def get_error_classifier() -> ErrorClassifier:
    """获取全局错误分类器实例"""
    global _classifier
    if _classifier is None:
        _classifier = ErrorClassifier()
    return _classifier


def classify_error(
    error: Exception,
    **kwargs,
) -> ClassifiedError:
    """快捷函数：使用全局分类器分类错误"""
    return get_error_classifier().classify(error, **kwargs)