# -*- coding: utf-8 -*-
"""
系统工具插件 — 网络工具（自包含实现）

websearch / webfetch 完全自包含：httpx/html2text/bs4 直接实现，
API key 优先读环境变量，未设置时使用插件内置默认值（主程序不再注入）。
"""

import re
from typing import Optional

import html2text
import httpx
from bs4 import BeautifulSoup
from loguru import logger

from app.tools.registry import make_summarize_from_preview

from app.tools.result import ToolResult

GROUP_NETWORK = "网络"

_NEWLINE_PATTERN = re.compile(r"\n{3,}")

# 共享 httpx 客户端（连接池复用）
_http_client: Optional[httpx.Client] = None


def _get_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DriFox/1.0"},
            follow_redirects=True,
            timeout=15,
        )
    return _http_client


# ── E1 插件配置契约：schema 声明在 plugin.json（config_schema），存储走 PluginConfigStore ──


def _ensure_migrated() -> None:
    """旧 <app_data>/tools/web_search_keys.json 一次性迁移到统一存储（幂等）。

    旧文件存在且新存储为空 → 迁移并改名 .bak；否则静默跳过。
    """
    from pathlib import Path

    from app.plugins.managers.plugin_config_store import PluginConfigStore
    from app.utils.utils import get_app_data_dir

    legacy = Path(get_app_data_dir()) / "tools" / "web_search_keys.json"
    if not legacy.exists():
        return
    PluginConfigStore().migrate(
        "system",
        legacy,
        key_map={"tavily_api_key": "tavily_api_key", "tinyfish_api_key": "tinyfish_api_key"},
    )


def _api_key(tool_ctx, name: str) -> str:
    """读取搜索服务 API key：环境变量 → 插件配置 → schema 默认（E1 三级链）

    - 环境变量：TAVILY_API_KEY / TINYFISH_API_KEY（最高优先级）
    - 插件配置：PluginConfigStore（plugin.json config_schema 声明，设置面板自动渲染）
    - 默认值：plugin.json config_schema.default（内置兜底）
    """
    _ensure_migrated()
    from app.plugins.managers.plugin_config_store import PluginConfigStore

    key = "tavily_api_key" if name == "TAVILY_API_KEY" else "tinyfish_api_key"
    val = PluginConfigStore().get("system", key)
    return str(val or "")


_WEBSEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "websearch",
        "description": "网络搜索",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词"},
                "num_results": {"type": "integer", "description": "结果数量"},
            },
            "required": ["query"],
        },
    },
}


def _websearch_impl(tool_ctx, **kwargs):
    query = kwargs.get("query", "")
    num_results = int(kwargs.get("num_results") or 10)
    if not query:
        return ToolResult(False, error="query 不能为空")

    # ── 1. Tavily（独立索引，含全文内容）──
    tavily_key = _api_key(tool_ctx, "TAVILY_API_KEY")
    if tavily_key:
        try:
            response = _get_client().post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {tavily_key}", "Content-Type": "application/json"},
                json={
                    "query": query,
                    "search_depth": "basic",
                    "max_results": num_results,
                    "include_answer": False,
                },
                timeout=15,
            )
            if response.status_code == 401:
                logger.warning("[WebTools] Tavily key invalid")
            elif response.status_code == 429:
                logger.warning("[WebTools] Tavily rate limited")
            else:
                response.raise_for_status()
                data = response.json()
                results = []
                for item in data.get("results", [])[:num_results]:
                    title = item.get("title", "")
                    link = item.get("url", "")
                    snippet = (item.get("content") or item.get("snippet") or "")[:400]
                    if title and link:
                        results.append(f"**{title}**\n{link}\n{snippet}")
                if results:
                    return ToolResult(
                        True,
                        content=f"搜索结果 ({len(results)}):\n\n" + "\n\n".join(results),
                    )
        except Exception as e:
            logger.warning(f"[WebTools] Tavily error: {e}")

    # ── 2. TinyFish（无限免费兜底）──
    tinyfish_key = _api_key(tool_ctx, "TINYFISH_API_KEY")
    if tinyfish_key:
        try:
            response = _get_client().get(
                "https://api.search.tinyfish.ai",
                headers={"X-API-Key": tinyfish_key},
                params={"query": query, "count": min(num_results, 20)},
                timeout=15,
            )
            if response.status_code == 401:
                logger.warning("[WebTools] TinyFish key invalid")
            elif response.status_code == 429:
                logger.warning("[WebTools] TinyFish rate limited")
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
                    return ToolResult(
                        True,
                        content=f"搜索结果 ({len(results)}):\n\n" + "\n\n".join(results),
                    )
        except Exception as e:
            logger.warning(f"[WebTools] TinyFish error: {e}")

    return ToolResult(False, error="搜索失败：无可用搜索引擎（未配置 API key 或请求失败）")


_WEBFETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "webfetch",
        "description": "获取网页内容",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "网页URL"},
                "format": {"type": "string", "description": "返回格式: html/text/markdown"},
            },
            "required": ["url"],
        },
    },
}


def _webfetch_impl(tool_ctx, **kwargs):
    url = kwargs.get("url", "")
    fmt = kwargs.get("format", "markdown")
    max_chars = int(kwargs.get("max_chars") or 26000)
    if not url:
        return ToolResult(False, error="url 不能为空")
    try:
        response = _get_client().get(url)
        response.raise_for_status()
        html_content = response.text
        if fmt == "html":
            return ToolResult(True, content=html_content[:max_chars])
        soup = BeautifulSoup(html_content, "html.parser")
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]):
            element.decompose()
        if fmt == "text":
            text = soup.get_text(separator="\n")
            clean_text = _NEWLINE_PATTERN.sub("\n", text).strip()
            return ToolResult(True, content=clean_text[:max_chars])
        # markdown
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        h.ignore_emphasis = False
        md = h.handle(html_content)
        md = _NEWLINE_PATTERN.sub("\n\n", md).strip()
        return ToolResult(True, content=md[:max_chars])
    except httpx.HTTPStatusError as e:
        return ToolResult(False, error=f"HTTP {e.response.status_code}: {url}")
    except Exception as e:
        return ToolResult(False, error=f"Fetch error: {str(e)}")


def _preview_websearch(tool_args: dict) -> str:
    query = tool_args.get("query", "")
    return f'搜索 "{query}"' if query else "网络搜索"


def _preview_webfetch(tool_args: dict) -> str:
    url = tool_args.get("url", "")
    return f"获取网页 {url}" if url else "获取网页"


def register(registry):
    registry.register(
        "websearch",
        _WEBSEARCH_SCHEMA,
        impl=_websearch_impl,
        danger="safe",
        icon="websearch",
        cn_name="网页搜索",
        group=GROUP_NETWORK,
        description="网络关键词搜索",
        aliases=["WebSearch", "web_search", "Search", "SearchWeb"],
        preview=_preview_websearch,
        summarize=make_summarize_from_preview(_preview_websearch),
        metadata={"permission_arg": "query"},
    )
    registry.register(
        "webfetch",
        _WEBFETCH_SCHEMA,
        impl=_webfetch_impl,
        danger="safe",
        icon="websearch",
        cn_name="抓取网页",
        group=GROUP_NETWORK,
        description="获取网页内容",
        aliases=["WebFetch", "Fetch", "FetchPage", "FetchUrl"],
        preview=_preview_webfetch,
        summarize=make_summarize_from_preview(_preview_webfetch),
        metadata={"permission_arg": "url"},
    )
