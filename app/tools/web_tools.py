# -*- coding: utf-8 -*-
"""
网页工具集 - 提供网页获取和搜索功能

支持：
- fetch_web: 获取网页内容，支持 markdown/html/text 格式
- search_web: 搜索网页，支持 Tavily / TinyFish 双引擎互备
"""
import os
import re
import threading
from pathlib import Path
from typing import Optional

import html2text
import httpx
from bs4 import BeautifulSoup
from loguru import logger

from app.tools.result import ToolResult
from app.utils.config import Settings

# ========== 性能优化：预编译正则表达式 ==========
_NEWLINE_PATTERN = re.compile(r"\n+")
_MULTI_NEWLINE_PATTERN = re.compile(r"\n{3,}")

# 共享的 HTTP headers 配置
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ========== 性能优化：复用 httpx 连接池 ==========
_HTTP_CLIENT_LOCK = threading.Lock()
_HTTP_CLIENT: Optional[httpx.Client] = None

def _get_http_client() -> httpx.Client:
    """获取共享的 httpx 客户端（懒加载，线程安全，带连接池复用）"""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        with _HTTP_CLIENT_LOCK:
            if _HTTP_CLIENT is None:
                _HTTP_CLIENT = httpx.Client(
                    timeout=30,
                    follow_redirects=True,
                    headers=_DEFAULT_HEADERS,
                    limits=httpx.Limits(
                        max_keepalive_connections=8,
                        max_connections=16,
                        keepalive_expiry=60,
                    ),
                )
    return _HTTP_CLIENT


def _fetch_html_content(url: str) -> tuple[httpx.Response, str]:
    """获取网页内容（使用共享的 httpx 客户端，复用连接池）"""
    client = _get_http_client()
    response = client.get(url)
    response.raise_for_status()
    return response, response.text


class WebTools:
    def __init__(self, owner):
        self._owner = owner

    @property
    def workdir(self) -> Path:
        return self._owner.workdir

    def fetch_web(
        self,
        url: str,
        format: str = "markdown",
        max_chars: int = 26000,
    ) -> ToolResult:
        """
        获取网页内容，支持 markdown/html/text 格式

        Args:
            url: 网页 URL
            format: 返回格式 (markdown/html/text)
            max_chars: 最大返回字符数
        """
        return self._fetch_sync(url, format, max_chars)

    def _fetch_sync(self, url: str, format: str, max_chars: int) -> ToolResult:
        """同步获取网页（使用共享函数）"""
        try:
            response, html_content = _fetch_html_content(url)

            if format == "html":
                return ToolResult(True, content=html_content[:max_chars])

            soup = BeautifulSoup(html_content, "html.parser")
            for element in soup(
                [
                    "script",
                    "style",
                    "nav",
                    "footer",
                    "header",
                    "aside",
                    "iframe",
                    "noscript",
                ]
            ):
                element.decompose()

            if format == "text":
                text = soup.get_text(separator="\n")
                clean_text = _NEWLINE_PATTERN.sub("\n", text).strip()
                return ToolResult(True, content=clean_text[:max_chars])

            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0
            h.ignore_emphasis = False
            markdown_text = h.handle(str(soup))
            markdown_text = _MULTI_NEWLINE_PATTERN.sub("\n\n", markdown_text)
            return ToolResult(True, content=markdown_text[:max_chars])

        except httpx.HTTPStatusError as e:
            return ToolResult(False, error=f"HTTP error: {e.response.status_code}")
        except Exception as e:
            return ToolResult(False, error=f"Fetch error: {str(e)}")

    def search_web(
        self,
        query: str,
        num_results: int = 10,
    ) -> ToolResult:
        """
        搜索网络，支持 Tavily → TinyFish 双引擎互备

        Args:
            query: 搜索关键词
            num_results: 返回结果数量
        """
        return self._search_sync(query, num_results)

    def _search_sync(self, query: str, num_results: int) -> ToolResult:
        """同步搜索（Tavily → TinyFish 双引擎互备）"""
        # ── 1. Tavily（独立索引，含全文内容，AI 友好）──
        tavily_key = (
            os.environ.get("TAVILY_API_KEY")
            or Settings.get_instance().TAVILY_API_KEY.value
        )
        if tavily_key:
            try:
                client = _get_http_client()
                response = client.post(
                    "https://api.tavily.com/search",
                    headers={
                        "Authorization": f"Bearer {tavily_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": query,
                        "search_depth": "basic",
                        "max_results": num_results,
                        "include_answer": False,
                    },
                    timeout=15,
                )

                if response.status_code == 401:
                    logger.warning("Tavily key invalid")
                elif response.status_code == 429:
                    logger.warning("Tavily rate limited")
                else:
                    response.raise_for_status()
                    data = response.json()
                    results = []
                    for item in data.get("results", [])[:num_results]:
                        title = item.get("title", "")
                        link = item.get("url", "")
                        content = item.get("content", "")
                        if title and link:
                            results.append(f"**{title}**\n{link}\n{content}")
                    if results:
                        return ToolResult(True, content="\n\n".join(results))
                    logger.warning("Tavily returned empty results")

            except httpx.TimeoutException:
                logger.warning("Tavily timeout")
            except httpx.RequestError as e:
                logger.warning(f"Tavily request failed: {e}")
            except httpx.HTTPStatusError as e:
                logger.warning(f"Tavily HTTP error: {e.response.status_code}")
            except Exception as e:
                logger.warning(f"Tavily error: {e}")

        # ── 2. TinyFish（无限免费兜底）──
        tinyfish_key = (
            os.environ.get("TINYFISH_API_KEY")
            or Settings.get_instance().TINYFISH_API_KEY.value
        )
        if tinyfish_key:
            try:
                client = _get_http_client()
                response = client.get(
                    "https://api.search.tinyfish.ai",
                    headers={"X-API-Key": tinyfish_key},
                    params={"query": query, "count": min(num_results, 20)},
                    timeout=15,
                )

                if response.status_code == 401:
                    logger.warning("TinyFish key invalid")
                elif response.status_code == 429:
                    logger.warning("TinyFish rate limited")
                else:
                    response.raise_for_status()
                    data = response.json()
                    results = []
                    for item in data.get("results", [])[:num_results]:
                        title = item.get("title", "")
                        link = item.get("url", "")
                        snippet = item.get("snippet", "")
                        if title and link:
                            results.append(f"**{title}**\n{link}\n{snippet}")
                    if results:
                        return ToolResult(True, content="\n\n".join(results))
                    logger.warning("TinyFish returned empty results")

            except httpx.TimeoutException:
                logger.warning("TinyFish timeout")
            except httpx.RequestError as e:
                logger.warning(f"TinyFish request failed: {e}")
            except httpx.HTTPStatusError as e:
                logger.warning(f"TinyFish HTTP error: {e.response.status_code}")
            except Exception as e:
                logger.warning(f"TinyFish error: {e}")

        # ── 3. 所有搜索引擎均不可用 ──
        return ToolResult(
            False,
            error="All search backends unavailable (Tavily → TinyFish both failed)",
        )
