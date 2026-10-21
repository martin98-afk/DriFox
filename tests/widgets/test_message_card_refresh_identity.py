# -*- coding: utf-8 -*-
"""test_message_card_refresh_identity.py — assistant 占位卡身份行延迟刷新回归。

2026-09-22 症状：输入框上下切换历史输入（含 @助手名）发送后，回答内容/工具
档位已是新助手，但身份行（头像+名称）仍显示主助手。

根因：assistant 占位卡在发送同步段创建并解析身份（早于后台 PreSendWorker 的
PreUserMessage hook @检测），旧身份被固化在卡片 ``_identity`` 上；hook 完成后
清缓存救不了已解析的卡片。修复：流式开始时（hook 必已完成）调
``refresh_identity()`` 以最新身份重画。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def _bare_card(role: str = "assistant", header=None):
    """跳过 __init__ 构造最小卡片（refresh_identity 只依赖这五个属性）。

    message_card 模块链含 QtWebEngineWidgets，必须在 QCoreApplication 创建前
    导入；测试文件顶层 import 会在收集期炸（先于 conftest 的 Qt 属性设置），
    故统一在用例内延迟 import（与 test_message_card_meta_deleted.py 同模式）。
    """
    from app.core.infra.message_identity import MessageIdentity
    from app.widgets.message_card import MessageCard

    card = MessageCard.__new__(MessageCard)
    card.role = role
    card._parent = None
    card._source_message = {"role": role, "content": ""}
    card._identity = MessageIdentity(name="旧助手", avatar="")
    card._identity_header = header
    return card


def test_refresh_identity_updates_stale_header(monkeypatch):
    """占位卡固化旧身份后，refresh_identity 以最新解析结果重画 header。"""
    from app.core.infra import message_identity
    from app.core.infra.message_identity import MessageIdentity

    new_identity = MessageIdentity(name="花子", avatar="C:/x/avatar.png")
    monkeypatch.setattr(
        message_identity, "resolve_for_message", lambda msg, role, **kw: new_identity
    )
    calls = []
    header = type("H", (), {"set_identity": staticmethod(lambda i: calls.append(i))})()
    card = _bare_card(header=header)

    card.refresh_identity()

    assert card._identity == new_identity
    assert calls == [new_identity]


def test_refresh_identity_skips_when_same(monkeypatch):
    """身份未变化（多轮工具迭代重复触发）→ 不重画 header，零成本幂等。"""
    from app.core.infra import message_identity
    from app.core.infra.message_identity import MessageIdentity

    same_identity = MessageIdentity(name="花子", avatar="")
    monkeypatch.setattr(
        message_identity, "resolve_for_message", lambda msg, role, **kw: same_identity
    )
    calls = []
    header = type("H", (), {"set_identity": staticmethod(lambda i: calls.append(i))})()
    card = _bare_card(header=header)
    card._identity = same_identity

    card.refresh_identity()

    assert calls == []


def test_refresh_identity_without_header_only_updates_cache(monkeypatch):
    """header 未构建（懒建路径）→ 只更新身份缓存，后续懒建拿到新值。"""
    from app.core.infra import message_identity
    from app.core.infra.message_identity import MessageIdentity

    new_identity = MessageIdentity(name="花子", avatar="")
    monkeypatch.setattr(
        message_identity, "resolve_for_message", lambda msg, role, **kw: new_identity
    )
    card = _bare_card(header=None)

    card.refresh_identity()

    assert card._identity == new_identity


def test_refresh_identity_swallows_resolver_error(monkeypatch):
    """解析链异常（插件卸载窗口）→ 保持原身份，渲染路径不中断。"""
    from app.core.infra import message_identity
    from app.core.infra.message_identity import MessageIdentity

    def _boom(msg, role, **kw):
        raise RuntimeError("plugin unloading")

    monkeypatch.setattr(message_identity, "resolve_for_message", _boom)
    card = _bare_card(header=None)

    card.refresh_identity()  # 不抛

    assert card._identity == MessageIdentity(name="旧助手", avatar="")
