# -*- coding: utf-8 -*-
"""欢迎页 tab 渲染函数 — ui/_render.py

参照自系统插件 welcome_changelog 的 _render.py。

render_func 契约：
- 签名 (ctx: dict) -> str，返回完整 HTML 片段
- ctx 由主程序注入（主题色 colors 等，具体键见 ui-plugin-creator references/）
- 可含 <style> / <script>；主程序拼接后渲染，不会二次转义

数据获取惯例（welcome_changelog 分层）：
- 纯静态/本地数据：直接在本文件拼 HTML
- 需要网络/耗时数据：放 _fetcher.py 后台拉取 + 进程内缓存，
  完成后经 app.core.ui_event_bus 事件通知刷新（勿在 render_func 里阻塞请求）
"""


def render_example(ctx: dict) -> str:
    colors = (ctx or {}).get("colors", {})
    text = colors.get("text_primary", "#888")

    return f"""
<style>
.wte-wrap {{ font-family: inherit; color: {text}; padding: 8px 4px; }}
.wte-wrap h3 {{ margin: 0 0 8px; }}
.wte-wrap li {{ margin: 4px 0; }}
</style>
<div class="wte-wrap">
  <h3>👋 欢迎页示例 tab</h3>
  <ul>
    <li>这是 render_example(ctx) 返回的 HTML</li>
    <li>改 _render.py 后禁用再启用插件即可刷新</li>
  </ul>
</div>
"""
