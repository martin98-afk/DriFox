# -*- coding: utf-8 -*-
"""F7（Bug9）：consolidate_messages LRU 缓存脏命中修复测试

问题（T4 实证）：consolidate_messages 缓存 key=(id, len, 首尾 role 指纹)。
流式收尾/更新会在**原消息对象上原地写** model_name/provider_name/config_id
（_on_stream_finished）与 elapsed（_on_messages_updated）——len 与首尾 role
不变 → 缓存命中返回缺这些字段的旧列表（脏数据）。

修复（方案 2）：指纹升级为覆盖每条消息的原地可变字段
（role/_hook_event/model_name/provider_name/config_id/elapsed），字段值变化
即指纹变化 → 缓存失效。

覆盖：
- 原地写 model_name / elapsed / provider_name / config_id → 缓存失效返回完整数据
- 无字段变化 → 缓存命中（性能回归：返回同一列表对象）
- 消息追加（len 变化）→ 缓存失效（回归）
- 不同列表对象（id 不同）不串缓存
"""

from app.core.message_content import consolidate_messages


def _fresh_messages():
    return [
        {"role": "user", "content": "问题", "timestamp": "t1"},
        {"role": "assistant", "content": "回答", "timestamp": "t2"},
    ]


class TestConsolidateCacheInvalidation:
    def test_inplace_model_name_invalidates(self):
        """原地补写 model_name → 缓存失效，返回含 model_name 的完整数据（Bug9 核心）"""
        msgs = _fresh_messages()
        first = consolidate_messages(msgs)
        assert first[1].get("model_name") is None, "前置：消息无 model_name 时输出无该字段"

        # 模拟 _on_stream_finished 原地写
        msgs[1]["model_name"] = "gpt-4o"
        second = consolidate_messages(msgs)
        assert second[1].get("model_name") == "gpt-4o", "原地写 model_name 后缓存应失效并返回新值"

    def test_inplace_elapsed_invalidates(self):
        """原地补写 elapsed → 缓存失效（_on_messages_updated 路径）"""
        msgs = _fresh_messages()
        consolidate_messages(msgs)
        msgs[1]["elapsed"] = 3.5
        second = consolidate_messages(msgs)
        assert second[1].get("elapsed") == 3.5, "原地写 elapsed 后缓存应失效并返回新值"

    def test_inplace_provider_and_config_invalidates(self):
        """原地补写 provider_name + config_id → 缓存失效"""
        msgs = _fresh_messages()
        consolidate_messages(msgs)
        msgs[1]["provider_name"] = "openai"
        msgs[1]["config_id"] = "cfg-1"
        second = consolidate_messages(msgs)
        assert second[1].get("provider_name") == "openai"
        assert second[1].get("config_id") == "cfg-1"

    def test_cache_hit_returns_same_object(self):
        """无字段变化时缓存命中（性能回归：返回同一列表对象，不重复 normalize）"""
        msgs = _fresh_messages()
        first = consolidate_messages(msgs)
        second = consolidate_messages(msgs)
        assert second is first, "无变化时应命中缓存（同一列表对象）"

    def test_append_invalidates(self):
        """消息追加（len 变化）→ 缓存失效（回归：追加语义不变）"""
        msgs = _fresh_messages()
        consolidate_messages(msgs)
        msgs.append({"role": "user", "content": "追问", "timestamp": "t3"})
        second = consolidate_messages(msgs)
        assert len(second) == 3

    def test_inplace_on_earlier_message_invalidates(self):
        """原地写非末尾消息（如第 1 条）也触发失效（指纹覆盖全列表）"""
        msgs = _fresh_messages()
        consolidate_messages(msgs)
        msgs[0]["model_name"] = "legacy"  # user 消息被原地加 model_name（异常但防御）
        second = consolidate_messages(msgs)
        assert second[0].get("model_name") == "legacy"

    def test_different_list_ids_no_cross_hit(self):
        """不同列表对象（id 不同）同内容 → 缓存 key 不同，互不影响"""
        a = _fresh_messages()
        b = _fresh_messages()
        ra = consolidate_messages(a)
        rb = consolidate_messages(b)
        assert rb is not ra, "不同 list 对象不应共享缓存条目"

    def test_inplace_hook_event_invalidates(self):
        """原地补写 _hook_event（F6 迁移路径）→ 缓存失效"""
        msgs = [{"role": "user", "content": "📨 **来自 [build] 的任务邮件：**\n任务", "timestamp": "t0"}]
        consolidate_messages(msgs)
        msgs[0]["_hook_event"] = "TeamMail"
        second = consolidate_messages(msgs)
        assert second[0].get("_hook_event") == "TeamMail"
