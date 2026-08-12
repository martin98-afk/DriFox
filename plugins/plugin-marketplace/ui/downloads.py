# -*- coding: utf-8 -*-
"""插件下载量实时查询 — 社区插件（无 downloads 字段）向 CountAPI 查询计数

与 installer.py / 市场端 tools/downloads_stats.py 保持一致的 key 命名：
- COUNT_API_BASE / COUNT_KEY_PREFIX 必须与两侧一致（drifox-plugins-{插件名}）
- GET {COUNT_API_BASE}/get/{key} 返回 {"value": count}，只读不 +1

设计：
- 内存缓存 + TTL（默认 1h）：命中直接返回，零网络请求
- 失败防抖（默认 60s）：查询失败的插件短时间不重试，避免渲染循环打爆计数服务
- 批量：fetch_missing(names) 一次处理一批，逐 key 查询
"""

import json
import time
import urllib.request
from typing import Dict, List, Optional

from loguru import logger

# 与 installer.py 上报链路保持一致（key = 前缀 + 插件名）
COUNT_API_BASE = "https://countapi.mileshilliard.com/api/v1"
COUNT_KEY_PREFIX = "drifox-plugins-"
_COUNT_UA = "DriFox/0.5 (+https://github.com/martin98-afk/drifox-plugins)"

# 缓存 TTL（秒）：命中直接返回，不重查
TTL = 3600
# 失败防抖（秒）：失败 key 在窗口内不重试
FAIL_TTL = 60


class DownloadsFetcher:
    """下载量实时查询：内存缓存 + TTL + 失败防抖 + 批量"""

    def __init__(self, timeout: float = 5.0):
        self._cache: Dict[str, tuple] = {}  # {name: (count, fetched_ts)}
        self._fail_ts: Dict[str, float] = {}  # {name: ts} 失败防抖
        self._timeout = timeout

    def fetch_missing(self, names: List[str]) -> Dict[str, int]:
        """查询缺失计数的插件，返回 {name: count}

        - 缓存命中（TTL 内）→ 直接返回
        - 失败防抖内 → 跳过（不重试）
        - 其余 → 逐 key 查询，成功写缓存，失败记防抖
        """
        result: Dict[str, int] = {}
        to_fetch: List[str] = []
        now = time.time()

        for n in names:
            if n in self._cache and now - self._cache[n][1] < TTL:
                result[n] = self._cache[n][0]
            elif n in self._fail_ts and now - self._fail_ts[n] < FAIL_TTL:
                pass  # 失败防抖内，跳过
            else:
                to_fetch.append(n)

        for n in to_fetch:
            count = self._get_one(n)
            if count is not None:
                self._cache[n] = (count, time.time())
                result[n] = count
            else:
                self._fail_ts[n] = time.time()

        return result

    def _get_one(self, name: str) -> Optional[int]:
        """GET CountAPI /get/{key}，返回计数值；失败返回 None"""
        key = f"{COUNT_KEY_PREFIX}{name}"
        req = urllib.request.Request(
            f"{COUNT_API_BASE}/get/{key}",
            headers={"User-Agent": _COUNT_UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            value = data.get("value")
            if isinstance(value, (int, float)):
                return int(value)
            return None
        except Exception as e:
            logger.debug(f"[Marketplace] 查询下载量失败 {name}: {e}")
            return None


# ── 单例 ──

_instance: Optional[DownloadsFetcher] = None


def get_downloads_fetcher() -> DownloadsFetcher:
    global _instance
    if _instance is None:
        _instance = DownloadsFetcher()
    return _instance
