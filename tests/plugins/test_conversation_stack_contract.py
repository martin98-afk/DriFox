# -*- coding: utf-8 -*-
"""ConversationStack 契约：声明 ConversationCore.create + ConversationExecutor 构建面"""


def test_contract_importable():
    from app.plugins.contracts.conversation_stack import ConversationStackFactory

    assert ConversationStackFactory is not None


def test_real_conversation_core_satisfies_factory_signature():
    """真实 ConversationCore.create 的参数集 ⊆ 契约声明（签名漂移守卫）"""
    import inspect

    from app.core.conversation.core import ConversationCore
    from app.plugins.contracts.conversation_stack import ConversationStackFactory

    real_params = set(inspect.signature(ConversationCore.create).parameters)
    contract_params = set(inspect.signature(ConversationStackFactory.create_core).parameters)
    missing = real_params - contract_params
    assert not missing, f"ConversationCore.create 新增参数未入契约: {missing}"
