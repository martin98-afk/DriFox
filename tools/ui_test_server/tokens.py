# -*- coding: utf-8 -*-
"""ui_click 两步确认协议（迁移自 f20c56b7 插件实现，适配服务层单例）。

- :class:`TokenStore`：一次性 confirm_token 发放 / 校验 / 惰性过期清扫
- :data:`DANGEROUS_KEYWORDS` + :func:`match_dangerous`：危险动作黑名单
  （辅助防线，命中即拒，有 token 也拒）

协议语义（与 f20c56b7 插件版逐点等价）：
- ui_inspect mode=find 命中可点按钮 → issue(object_name) 下发 5 分钟令牌
- ui_click 必须携带匹配且未过期令牌；校验通过即消费作废（一次性）
- 令牌与 object_name 绑定，错配拒绝
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Dict, Optional, Tuple

TOKEN_TTL_S = 300.0

DANGEROUS_KEYWORDS = ("删除", "移除", "清空", "撤回", "解散", "delete", "remove", "clear")


def match_dangerous(text: str) -> Optional[str]:
    """目标文本命中危险关键词则返回该关键词（小写化匹配英文项）。"""
    probe = (text or "").lower()
    for kw in DANGEROUS_KEYWORDS:
        if kw.lower() in probe:
            return kw
    return None


class TokenStore:
    """一次性 confirm_token 存储（线程安全；服务层单例使用）。"""

    def __init__(self, ttl_s: float = TOKEN_TTL_S) -> None:
        self._ttl = ttl_s
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._seq = itertools.count(1)

    def issue(self, object_name: str) -> Tuple[str, float]:
        """为 object_name 发放一次性令牌，返回 (token, expires_epoch)。"""
        token = f"tok_{next(self._seq):06d}_{abs(hash((object_name, time.time()))) % 10**8:08d}"
        expires = time.time() + self._ttl
        with self._lock:
            self._sweep_locked()
            self._store[token] = {"object_name": object_name, "expires": expires}
        return token, expires

    def validate(self, token: str, object_name: str, consume: bool = True) -> Tuple[bool, str]:
        """校验令牌：缺失/过期/错配给出原因；通过且 consume=True 时消费作废。

        注意：本方法不做惰性清扫（清扫在 issue 路径），保证过期项
        能返回精确的 ``expired_token`` 而非 ``missing_token``。
        """
        with self._lock:
            entry = self._store.get(token)
            if not token or entry is None:
                return False, "missing_token"
            if entry["expires"] < time.time():
                self._store.pop(token, None)
                return False, "expired_token"
            if entry["object_name"] != object_name:
                return False, "token_mismatch"
            if consume:
                self._store.pop(token, None)
            return True, ""

    def peek(self, token: str) -> Optional[Dict[str, Any]]:
        """查看令牌条目（不消费）；不存在返回 None。"""
        with self._lock:
            return self._store.get(token)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def _sweep_locked(self) -> None:
        """惰性清扫过期令牌（调用方持锁）。"""
        now = time.time()
        expired = [t for t, e in self._store.items() if e["expires"] < now]
        for t in expired:
            self._store.pop(t, None)


__all__ = ["DANGEROUS_KEYWORDS", "TokenStore", "match_dangerous"]
