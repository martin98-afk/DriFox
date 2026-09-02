# -*- coding: utf-8 -*-
"""assistant_hub UI 入口。

注册组件（参考 agent_trace 同款模式）：

1. **常驻标题栏 tab**（``register_titlebar_tab``）
   - tab_id = ``assistant_hub``
   - label = ``助手``（放在「轨迹」右侧）
   - on_click → ``UIPluginRegistry.toggle_floating_card("assistant_hub")``

2. **full 容器浮动卡**（``register_floating_card``）
   - card_id = ``assistant_hub``
   - container = ``full``
   - widget_class = ``AssistantCardWidget``（左列表 + 右 Tab 编辑器）

3. **Gitee 同步内容注册**（``register_sync_content_provider``）
   - provider_id = ``assistant_hub``
   - 同步整个 <app_data>/assistant_hub/ 目录（助手信息 + 记忆），跨设备同步。

热重载兼容：清理 ``ui_plugin_assistant_hub.*`` 旧子模块。
"""

from __future__ import annotations

import base64
import importlib.util
import mimetypes
import sys
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

CARD_ID = "assistant_hub"

# ── 共享 manager 模块 ──────────────────────────────────────────
# ui 子模块与 hooks/inject_assistant.py 都通过 importlib 按文件路径加载
# assistant_manager.py（模块名固定 assistant_hub_manager 缓存到 sys.modules）。
# 保证两处拿到同一个 AssistantManager 类 → 单例一致，不会出现 UI 建助手、
# hook 读不到的双实例问题。
_SHARED_MANAGER_MODULE = "assistant_hub_manager"


def _ensure_shared_manager_module() -> None:
    root = Path(__file__).resolve().parent.parent
    source = root / "assistant_manager.py"
    try:
        mtime = source.stat().st_mtime
    except OSError:
        mtime = 0.0
    mod = sys.modules.get(_SHARED_MANAGER_MODULE)
    if mod is not None and getattr(mod, "_source_mtime", -1.0) >= mtime:
        return
    spec = importlib.util.spec_from_file_location(
        _SHARED_MANAGER_MODULE,
        str(source),
    )
    if spec is None or spec.loader is None:
        logger.error("[assistant_hub] 无法创建 shared manager spec")
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SHARED_MANAGER_MODULE] = module
    try:
        spec.loader.exec_module(module)
        # 热重载指纹：assistant_hub_manager 是固定全局模块名（ui+hooks 共享单例），
        # 不在主程序 UI 重载的 ui_plugin_* 清理前缀内 → 靠 mtime 自检自愈：
        # 文件更新后下次进入本函数即重新 exec 替换，UI/hook 谁先发现谁刷新。
        module._source_mtime = mtime
        logger.debug("[assistant_hub] shared assistant_hub_manager 已加载")
    except Exception as e:
        logger.error(f"[assistant_hub] shared manager 加载失败: {e}")


_ensure_shared_manager_module()


def _plugin_icons_dir() -> str:
    here = Path(__file__).resolve().parent
    return str(here.parent / "icons")


def _resolve_active_main_widgets() -> List[object]:
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        widgets = list(UIPluginRegistry.get_instance()._window_main_widgets.values())
    except Exception:
        widgets = []
    return [w for w in widgets if w is not None]


def _resolve_global_host():
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
    except Exception:
        return None, None, None
    try:
        host = reg._resolve_global_host()
    except Exception:
        host = None
    if host is None:
        for mw in _resolve_active_main_widgets():
            if getattr(mw, "_card_manager", None) is not None:
                host = mw
                break
    if host is None:
        return None, None, None
    return host, getattr(host, "_card_manager", None), getattr(host, "_window_id", None)


def _is_card_visible() -> bool:
    _host, cm, wid = _resolve_global_host()
    if cm is None or not wid:
        return False
    try:
        return bool(cm.is_card_visible(CARD_ID, wid))
    except Exception:
        return False


def _on_tab_clicked() -> None:
    """标题栏「助手」tab 点击 → 显示助手中心 full 卡片。"""
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
    except Exception as e:
        logger.error(f"[assistant_hub] 无法获取 UIPluginRegistry: {e}")
        return

    if _is_card_visible():
        logger.debug("[assistant_hub] 卡片已可见，忽略重复点击")
        return

    try:
        reg.toggle_floating_card(CARD_ID)
        logger.info("[assistant_hub] 已切换显示助手中心卡片")
    except Exception as e:
        logger.error(f"[assistant_hub] toggle_floating_card 失败: {e}")


# ── @ 卡片智能体区（mention provider）────────────────────────────

_MENTION_PROVIDER_ID = "assistant_hub"


def _mention_list_func() -> List[dict]:
    """@ 卡片顶部智能体条目（内存读取，无 I/O）"""
    try:
        from assistant_hub_manager import AssistantManager

        mgr = AssistantManager.get_instance()
        active = mgr.active_id()
        items = []
        for a in mgr.list_assistants_sorted_by_stable():
            avatar = mgr.assistant_avatar_path(a.id)
            items.append(
                {
                    "key": a.id,
                    "name": a.name or a.id,
                    "description": (a.public_description or "").strip()[:40],
                    "icon_path": str(avatar) if avatar else "",
                    "color": a.color,
                    "active": a.id == active,
                }
            )
        return items
    except Exception as e:
        logger.debug(f"[assistant_hub] mention 条目拉取失败: {e}")
        return []


def _on_mention_selected(entry: dict, ctx: dict) -> None:
    """@ 卡片选中助手 → 会话级临时切换（立即刷新该会话 system prompt）"""
    aid = str(entry.get("key", ""))
    sid = str(ctx.get("session_id", ""))
    if not aid or not sid:
        return
    try:
        from assistant_hub_manager import AssistantManager

        mgr = AssistantManager.get_instance()
        if mgr.set_session_override(sid, aid):
            mgr._invalidate_session_prompt(sid)
            a = mgr.get(aid)
            logger.info(f"[assistant_hub] 会话 {sid[:8]} 临时切换助手: {a.name if a else aid}")
    except Exception as e:
        logger.warning(f"[assistant_hub] 会话临时切换助手失败: {e}")


def _register_mention_provider() -> None:
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        UIPluginRegistry.get_instance().register_mention_provider(
            plugin_name="assistant_hub",
            provider_id=_MENTION_PROVIDER_ID,
            list_func=_mention_list_func,
            on_selected=_on_mention_selected,
        )
        logger.debug("[assistant_hub] 已注册 @ 卡片 mention provider")
    except Exception as e:
        logger.warning(f"[assistant_hub] 注册 mention provider 失败: {e}")


# ── 欢迎卡片「助手」tab ──────────────────────────────────────────

_WELCOME_TAB_MODE = "assistants"
_WELCOME_ACTION_INSERT = "assistant-hub-insert"

# 头像 data-URI 缓存：{(path, mtime): data_uri}
_avatar_uri_cache: Dict[str, str] = {}


def _avatar_data_uri(aid: str, mgr) -> str:
    """助手头像 → base64 data URI（mtime 入键，头像更换自动失效）"""
    try:
        p = mgr.assistant_avatar_path(aid)
        if not p:
            return ""
        key = f"{p}:{int(p.stat().st_mtime)}"
        cached = _avatar_uri_cache.get(key)
        if cached:
            return cached
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        uri = f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode('ascii')}"
        if len(_avatar_uri_cache) > 64:
            _avatar_uri_cache.clear()
        _avatar_uri_cache[key] = uri
        return uri
    except Exception:
        return ""


def _render_assistants_welcome(ctx: Optional[dict] = None) -> str:
    """欢迎卡片「助手」tab：助手卡片网格，点击填 @助手名 到输入区"""
    try:
        from assistant_hub_manager import AssistantManager

        mgr = AssistantManager.get_instance()
        assistants = mgr.list_assistants_sorted_by_stable()
        active_id = mgr.active_id()
    except Exception as e:
        logger.debug(f"[assistant_hub] 欢迎卡片助手数据读取失败: {e}")
        return ""

    if not assistants:
        return "<p style='color:#9aa0a8'>暂无助手，可在标题栏「助手」中创建。</p>"

    is_dark = bool(ctx.get("is_dark")) if isinstance(ctx, dict) else False
    card_bg = "#ffffff" if not is_dark else "#2b2b2b"
    card_border = "#e5e7eb" if not is_dark else "#3f3f46"
    muted = "#6b7280" if not is_dark else "#9ca3af"
    accent_border = "#7c3aed" if not is_dark else "#8b5cf6"

    cards = []
    for a in assistants:
        name = a.name or a.id
        uri = _avatar_data_uri(a.id, mgr)
        if uri:
            avatar_html = f'<img class="ah-avatar" src="{uri}" alt=""/>'
        else:
            avatar_html = (
                f'<div class="ah-avatar ah-avatar-fallback" style="background:{a.color}22;'
                f'color:{a.color};border:1px solid {a.color}55">{(name[:1] or "?").upper()}</div>'
            )
        badges = []
        if a.primary:
            badges.append('<span class="ah-badge ah-badge-primary">主助手</span>')
        if a.id == active_id:
            badges.append('<span class="ah-badge ah-badge-active">使用中</span>')
        badge_html = "".join(badges)
        desc = (a.public_description or "").strip()
        desc_html = f'<div class="ah-desc">{escape(desc)}</div>' if desc else ""
        cards.append(
            f'<div class="ah-card context-tag" data-type="{_WELCOME_ACTION_INSERT}" '
            f'data-content="{escape(name)}" data-action="{_WELCOME_ACTION_INSERT}" '
            f'title="点击在输入框填入 @{name}">'
            f"{avatar_html}"
            f'<div class="ah-info"><div class="ah-name">{escape(name)}{badge_html}</div>{desc_html}</div>'
            f"</div>"
        )

    style = f"""
<style>
.ah-grid {{ display: flex; flex-wrap: wrap; gap: 10px; }}
.ah-card {{
  display: flex; align-items: center; gap: 10px; width: calc(50% - 5px);
  box-sizing: border-box; padding: 10px 12px; border-radius: 10px;
  background: {card_bg}; border: 1px solid {card_border}; cursor: pointer;
  transition: border-color .15s, transform .15s;
}}
.ah-card:hover {{ border-color: {accent_border}; transform: translateY(-1px); }}
.ah-avatar {{
  width: 34px; height: 34px; border-radius: 50%; object-fit: cover; flex: none;
}}
.ah-avatar-fallback {{
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 14px;
}}
.ah-info {{ min-width: 0; }}
.ah-name {{ font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px; }}
.ah-badge {{
  font-size: 10px; padding: 1px 6px; border-radius: 8px; font-weight: 400; flex: none;
}}
.ah-badge-primary {{ background: #7c3aed22; color: #7c3aed; }}
.ah-badge-active {{ background: #16a34a22; color: #16a34a; }}
.ah-desc {{
  font-size: 11px; color: {muted}; margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
</style>
"""
    return f'<div class="ah-grid">{"".join(cards)}</div>{style}'


def _on_welcome_insert_action(content: str, ctx: dict) -> None:
    """欢迎卡片点击助手 → 输入区填 @助手名（只填文本，不切换）"""
    name = (content or "").strip()
    if not name:
        return
    mw = ctx.get("main_widget")
    if mw is None or not hasattr(mw, "input_area"):
        return
    try:
        mw.input_area.insert_assistant_mention(name)
    except Exception as e:
        logger.warning(f"[assistant_hub] 欢迎卡片填入 @助手名 失败: {e}")


def _register_welcome_tab() -> None:
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        reg.register_welcome_tab(
            plugin_name="assistant_hub",
            mode_key=_WELCOME_TAB_MODE,
            label="🤖 助手",
            render_func=_render_assistants_welcome,
            priority=10,
        )
        reg.register_welcome_action(
            plugin_name="assistant_hub",
            action=_WELCOME_ACTION_INSERT,
            handler=_on_welcome_insert_action,
        )
        logger.debug("[assistant_hub] 已注册欢迎卡片「助手」tab + 点击动作")
    except Exception as e:
        logger.warning(f"[assistant_hub] 注册欢迎卡片 tab 失败: {e}")


def _register_sync_provider() -> None:
    """注册 Gitee 同步内容：助手信息 + 记忆跨设备同步。

    与 ConfigSyncService 的 register_sync_content_provider 对接：
    - 本地目录 = AssistantManager.root（<app_data>/assistant_hub）
    - 远端路径 = drifox/ext/assistant_hub.zip
    - 绑定 Gitee 后自动上传/下载；目录变更 watch 到自动上传。
    """
    try:
        from app.core.config_sync import register_sync_content_provider

        from assistant_hub_manager import AssistantManager

        mgr = AssistantManager.get_instance()
        register_sync_content_provider(
            provider_id="assistant_hub",
            label="助手信息与记忆",
            local_dir=str(mgr.root),
            remote_path="drifox/ext/assistant_hub.zip",
            enabled=True,
        )
        logger.info(f"[assistant_hub] 已注册 Gitee 同步内容: {mgr.root}")
    except Exception as e:
        logger.warning(f"[assistant_hub] 注册 Gitee 同步内容失败: {e}")


def _promote_build_system_prompt_hook() -> None:
    """确保 assistant_hub 的 BuildSystemPrompt hook 先于系统 inject_agent_identity 执行。

    系统插件（plugins/system/hooks/hooks.json）里的 builtin_inject_agent_identity
    会读取 context["agent_identity_content"]。assistant_hub 的 hook 需要**先**改
    context 才能实现"替换注入"（否则系统 hook 先输出原智能体提示词，我们的
    修改只变成追加）。注册顺序 = 执行顺序，把我们的 rule 提前到列表头部。
    """
    try:
        from app.core.hook_manager import HookManager

        hm = HookManager.get_instance()
        rules = getattr(hm, "_hooks", {}).get("BuildSystemPrompt", [])
        # 找 assistant_hub 的 rule
        for i, rule in enumerate(rules):
            if getattr(rule, "skill_name", "") == "assistant_hub":
                if i > 0:
                    rule_obj = rules.pop(i)
                    rules.insert(0, rule_obj)
                break
        logger.debug("[assistant_hub] BuildSystemPrompt hook 已提升到最前（先于系统身份注入）")
    except Exception as e:
        logger.warning(f"[assistant_hub] 提升 hook 顺序失败: {e}")


def _kv_sections(content: str) -> List[tuple]:
    """解析标签块「键：值」行 → [(key, value)]；无键行 key 为空串。

    mood（感受/联想/反思/意志）与 plan（目标/路径/风险/取舍）同构。
    """
    sections: List[tuple] = []
    for raw in content.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        if "：" in line:
            key, _, val = line.partition("：")
            sections.append((key.strip(), val.strip()))
        elif ":" in line:
            key, _, val = line.partition(":")
            sections.append((key.strip(), val.strip()))
        else:
            sections.append(("", line))
    return sections


# 人格标签卡皮肤：title（标签名）/ subtitle（副题）/ icon（HTML entity）/ accent（点缀色，
# 固定色明暗主题均可读；文字主色继承消息卡片主题色）
_TAG_SKINS = {
    "mood": {"title": "MOOD", "subtitle": "内心独白", "icon": "&#9829;", "accent": "#c9767e"},
    "plan": {"title": "PLAN", "subtitle": "行动推演", "icon": "&#9678;", "accent": "#6c8ebf"},
}
_NEUTRAL_SKIN = {"title": "", "subtitle": "", "icon": "&#9671;", "accent": "#8a8f98"}

# 副标题兜底灰（不透明度写法 QTextDocument 不支持，用固定灰）
_MUTED = "#9aa0a8"


def _tag_skin(tag: str) -> dict:
    skin = _TAG_SKINS.get(tag) or dict(_NEUTRAL_SKIN)
    if not skin["title"]:
        skin["title"] = tag.upper()
    return skin


def _render_kv_tag_card(content: str, ctx: dict, skin: dict) -> str:
    """人格标签块通用键值卡渲染核心。

    纯函数：可在后台渲染线程调用，禁止触碰 Qt widget。结构为单格
    <table class="layout-table">（主程序全局表格样式与滚动包裹均排除
    layout-table，避免命中斑马纹/边框/圆角外框），td 内 <br> 分行，
    双端兼容 QLabel 富文本（QTextDocument）与 QWebEngineView。

    ctx: {tag, completed, compact}；流式未闭合（completed=False）渲染
    单行占位，避免逐 chunk 闪大卡。
    """
    import html as _html

    from app.utils.design_tokens import scale_font_size

    accent = skin["accent"]
    title_esc = _html.escape(skin["title"])
    subtitle_esc = _html.escape(skin["subtitle"])
    # 字号跟随系统 UI 字号缩放：正文较消息卡正文（14）稍小一档
    body_sz = scale_font_size(13)
    key_sz = scale_font_size(12)
    title_sz = scale_font_size(12)
    sub_sz = scale_font_size(11)
    if not bool(ctx.get("completed", True)):
        verb = "推演中" if subtitle_esc == "行动推演" else "解析中"
        return (
            '<table class="layout-table" border="0" cellspacing="0" cellpadding="0" width="100%" '
            'style="margin:6px 0; border:none; background:transparent;">'
            '<tr><td style="border:none; border-left:2px solid '
            f'{accent}; padding:3px 0 3px 12px; line-height:1.6;">'
            f'<span style="color:{accent}; font-size:{title_sz}px; font-weight:bold;">{skin["icon"]} {title_esc}</span>'
            f'<span style="color:{_MUTED}; font-size:{sub_sz}px;"> &#183; {verb}&#8230;</span>'
            "</td></tr></table>"
        )

    # 标题 + 副题同一行（不换行），键值行以 <br> 分隔
    header = (
        f'<span style="color:{accent}; font-size:{title_sz}px; font-weight:bold;">{skin["icon"]} {title_esc}</span>'
    )
    if subtitle_esc:
        header += f'<span style="color:{_MUTED}; font-size:{sub_sz}px;"> &#183; {subtitle_esc}</span>'
    body: List[str] = []
    for key, val in _kv_sections(content):
        key_esc = _html.escape(key)
        val_esc = _html.escape(val)
        if key:
            body.append(
                f'<span style="color:{accent}; font-size:{key_sz}px;">{key_esc}</span>'
                f'<span style="font-size:{body_sz}px;">&nbsp;&nbsp;{val_esc}</span>'
            )
        else:
            body.append(f'<span style="font-size:{body_sz}px;">{val_esc}</span>')
    inner = "<br>".join([header] + body)
    return (
        '<table class="layout-table" border="0" cellspacing="0" cellpadding="0" width="100%" '
        'style="margin:6px 0; border:none; background:transparent;">'
        '<tr><td style="border:none; border-left:2px solid '
        f'{accent}; padding:3px 0 3px 12px; line-height:1.6;">{inner}</td></tr></table>'
    )


def _make_tag_renderer(tag: str):
    """为指定 tag 生成渲染闭包（persona tag → 皮肤映射）。"""
    skin = _tag_skin(tag)

    def _render(content: str, ctx: dict) -> str:
        return _render_kv_tag_card(content, ctx, skin)

    return _render


def _persona_block_tags() -> List[str]:
    """收集全部人格 frontmatter 声明的块标签（去重小写排序）。

    读 PersonaRegistry 失败时回退内置 mood/plan（v2 人格的两个预置 tag）。
    """
    try:
        from assistant_hub_manager import AssistantManager

        reg = AssistantManager.get_instance().persona_registry()
        tags = sorted({p.tag.strip().lower() for p in reg.list_all() if p.tag and p.tag.strip()})
        if tags:
            return tags
    except Exception as e:
        logger.debug(f"[assistant_hub] 读取 persona tag 失败，回退内置列表: {e}")
    return ["mood", "plan"]


def refresh_persona_tag_renderers() -> None:
    """把当前全部人格 tag 幂等注册为块渲染器（含运行期新增人格的新 tag）。

    persona-creator 写盘新人格 → UI reload registry 后调用本函数，
    新 tag（如 SNAP）即刻获得渲染卡，无需重启。
    """
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        registered = set(reg.get_registered_tag_names())
        for tag in _persona_block_tags():
            if tag in registered:
                continue
            reg.register_tag_renderer(
                plugin_name="assistant_hub",
                tag_name=tag,
                render_func=_make_tag_renderer(tag),
                priority=10,
            )
            logger.info(f"[assistant_hub] 已补注册人格块渲染器: {tag}")
    except Exception as e:
        logger.warning(f"[assistant_hub] 补注册 tag renderer 失败: {e}")


def register_ui(registry) -> None:
    """注册 assistant_hub 的 UI 组件。"""
    # 热重载兼容
    prefix = "ui_plugin_assistant_hub."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    # 延迟 import — 卡片依赖 qfluentwidgets / PyQt5
    from .assistant_card import AssistantCardWidget

    icons_dir = _plugin_icons_dir()
    icon_dark = str(Path(icons_dir) / "icon.svg")
    icon_light = str(Path(icons_dir) / "icon_light.svg")

    # ── full 容器浮动卡 ──
    registry.register_floating_card(
        plugin_name="assistant_hub",
        card_id=CARD_ID,
        widget_class=AssistantCardWidget,
        container="full",
        title="助手中心",
        default_visible=False,
        metadata={
            "icon_dark": icon_dark,
            "icon_light": icon_light,
            "full_card": True,
            "hide_sidebar": True,
        },
    )

    # ── 常驻标题栏 tab（「助手」，位于「轨迹」之后）──
    registry.register_titlebar_tab(
        plugin_name="assistant_hub",
        tab_id=CARD_ID,
        label="助手",
        on_click=_on_tab_clicked,
        priority=10,
    )

    # ── 人格块标签卡（mood/plan/snap 等，按 persona frontmatter tag 动态注册）──
    refresh_persona_tag_renderers()

    # ── Gitee 同步内容：助手信息 + 记忆 ──
    _register_sync_provider()

    # ── @ 卡片智能体区（mention provider，选中后会话级临时切换）──
    _register_mention_provider()

    # ── 欢迎卡片「助手」tab + 点击填 @助手名 ──
    _register_welcome_tab()

    # ── BuildSystemPrompt hook 顺序提升（先于系统身份注入）──
    _promote_build_system_prompt_hook()

    logger.info(
        f"[assistant_hub] UI 组件已注册：titlebar_tab(助手) + floating_card(assistant_hub/full)"
        f" + tag_renderer({_persona_block_tags()}) + gitee sync"
        f" + mention_provider + welcome_tab(助手)"
    )
