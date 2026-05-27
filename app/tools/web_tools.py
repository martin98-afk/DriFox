# -*- coding: utf-8 -*-
"""
网页工具集 - 提供网页获取和搜索功能

支持：
- fetch_web: 获取网页内容，支持 markdown/html/text 格式
- search_web: 搜索网页，支持 DuckDuckGo
"""
import os
import re
import httpx
import html2text

from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from loguru import logger
from app.tools.result import ToolResult
from app.utils.config import Settings


# ========== 性能优化：预编译正则表达式 ==========
_NEWLINE_PATTERN = re.compile(r"\n+")
_MULTI_NEWLINE_PATTERN = re.compile(r"\n{3,}")
_TITLE_PATTERN = re.compile(r'class="result__title"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_SNIPPET_PATTERN = re.compile(r'class="result__snippet"[^>]*>(.*?)</div>', re.DOTALL)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

# 共享的 HTTP headers 配置
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _fetch_html_content(url: str) -> tuple[httpx.Response, str]:
    """获取网页内容的共享函数"""
    with httpx.Client(timeout=30, follow_redirects=True, headers=_DEFAULT_HEADERS) as client:
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
        搜索网络，支持 SerpAPI 或 DuckDuckGo 回退

        Args:
            query: 搜索关键词
            num_results: 返回结果数量
        """
        return self._search_sync(query, num_results)

    def _search_sync(self, query: str, num_results: int) -> ToolResult:
        """同步搜索"""
        api_key = (
            os.environ.get("SERPAPI_KEY") or Settings.get_instance().SERPAPI_KEY.value
        )

        if api_key == "your-serpapi-key-here" or not api_key:
            return self._search_duckduckgo_sync(query, num_results)

        try:
            proxies = None
            http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
            if http_proxy:
                proxies = {"http": http_proxy, "https": http_proxy}

            params = {
                "engine": "duckduckgo",
                "q": query,
                "kl": "us-en",
                "api_key": api_key,
            }

            response = httpx.get(
                "https://serpapi.com/search",
                params=params,
                proxies=proxies,
                timeout=30,
                follow_redirects=True,
            )

            if response.status_code == 401:
                logger.warning("SerpAPI key invalid, falling back to DuckDuckGo")
                return self._search_duckduckgo_sync(query, num_results)
            if response.status_code == 403:
                logger.warning("SerpAPI quota exceeded, falling back to DuckDuckGo")
                return self._search_duckduckgo_sync(query, num_results)

            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("organic_results", [])[:num_results]:
                title = item.get("title", "")
                link = item.get("link", "")
                snippet = item.get("snippet", "")
                if title and link:
                    results.append(f"- {title}\n  {link}\n  {snippet}")

            return ToolResult(
                True, content="\n\n".join(results) if results else "No results found"
            )

        except httpx.TimeoutException:
            logger.warning("SerpAPI timeout, falling back to DuckDuckGo")
            return self._search_duckduckgo_sync(query, num_results)
        except httpx.RequestError as e:
            logger.warning(f"SerpAPI request failed: {e}, falling back to DuckDuckGo")
            return self._search_duckduckgo_sync(query, num_results)
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"SerpAPI HTTP error: {e.response.status_code}, falling back to DuckDuckGo"
            )
            return self._search_duckduckgo_sync(query, num_results)
        except Exception as e:
            logger.warning(f"SerpAPI error: {e}, falling back to DuckDuckGo")
            return self._search_duckduckgo_sync(query, num_results)

    def _search_duckduckgo_sync(self, query: str, num_results: int) -> ToolResult:
        """DuckDuckGo 同步搜索"""
        try:
            url = "https://html.duckduckgo.com/html/"
            r = httpx.get(
                url,
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (compatible)"},
                timeout=30,
                follow_redirects=True,
            )
            titles = _TITLE_PATTERN.findall(r.text)
            snippets = _SNIPPET_PATTERN.findall(r.text)
            results = []
            for i, (link, title) in enumerate(titles[:num_results]):
                t = _HTML_TAG_PATTERN.sub("", title).strip()
                s = (
                    _HTML_TAG_PATTERN.sub("", snippets[i]).strip()
                    if i < len(snippets)
                    else ""
                )
                results.append(f"**{t}**\n{link}\n{s}")
            return ToolResult(
                True, content="\n\n".join(results) if results else "No results found"
            )
        except Exception as e:
            logger.error(f"DuckDuckGo search failed: {e}")
            return ToolResult(False, error=f"DuckDuckGo search failed: {str(e)}")
