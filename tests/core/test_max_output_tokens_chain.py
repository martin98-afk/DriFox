# -*- coding: utf-8 -*-
"""P1-20 / P1-21 回归：per-model 输出上限接通 + 「最大Token」语义拆分

背景（设计文档 §2.4）
--------------------
1. **per-model 输出上限被丢弃**：61 个模型的真实上限被 family 级 8192/65536 覆盖，
   最高折损 74%（space-bunny 524288 / grok-4.7 500000 / MiniMax-M3 512000）。
2. **`最大Token` 一键双语义**：既当上下文窗口又直接当 max_tokens 发出，用户填
   200000（窗口）会被真的当输出上限发出去。

修法：
- `_cap_max_output_tokens`（chat_worker / subagent_worker 同源同改）前置 L0 caps 层：
  命中 → `min(用户值, caps 上限)`；用户未设（<=0）→ caps 上限。
- 绝对上限兜底改为 `profile.absolute_limit or ABSOLUTE_FALLBACK_CEILING`（65536），
  只在插件与 family 两链都缺失时生效。
- 新键「最大输出」承担输出语义；发送链只读它，不回退旧键。

本组锁定 N1-N4：
    N1 caps=524288 不被 65536 截断
    N2 兜底 65536 + 插件 absolute_limit 优先
    N3 history_compactor 拿 caps 值（经 resolve_max_output_tokens）
    N4 三层锁：schema api_param / subagent 通配路径 / api_param 缺失负例
"""

import os
import sys
from unittest.mock import patch

import pytest

from app.constants import PARAM_SCHEMA
from app.core.modelmeta import model_capabilities as mc


# ══════════════════════════════════════════════════════════════════
# N1: caps 不被兜底常量截断
# ══════════════════════════════════════════════════════════════════


class TestN1CapsNotTruncated:
    def test_caps_524288_not_capped_by_fallback_ceiling(self):
        """caps=524288（space-bunny 真实值）→ 不被 65536 兜底截断"""
        with patch("app.core.workers.chat_worker.get_caps_max_output_tokens", return_value=524288):
            from app.core.workers.chat_worker import OpenAIChatWorker

            worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
            worker.llm_config = {"provider_name": "某服务商", "模型名称": "space-bunny"}

            # 用户设 200000 < caps → 用户值
            assert worker._cap_max_output_tokens("space-bunny", 200000) == 200000
            # 用户设 999999 > caps → 截到 caps
            assert worker._cap_max_output_tokens("space-bunny", 999999) == 524288
            # 用户未设（0）→ caps 上限（不再回落 family 的 8192/65536）
            assert worker._cap_max_output_tokens("space-bunny", 0) == 524288
            # 用户设负值 → 同上
            assert worker._cap_max_output_tokens("space-bunny", -1) == 524288

    def test_caps_512000_minimax_m3(self):
        """MiniMax-M3 512000：折损修复的典型（原被 65536 覆盖）"""
        with patch("app.core.workers.chat_worker.get_caps_max_output_tokens", return_value=512000):
            from app.core.workers.chat_worker import OpenAIChatWorker

            worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
            worker.llm_config = {"provider_name": "MiniMax", "模型名称": "MiniMax-M3"}
            assert worker._cap_max_output_tokens("MiniMax-M3", 0) == 512000


# ══════════════════════════════════════════════════════════════════
# N2: 兜底常量 + 插件 absolute_limit 优先
# ══════════════════════════════════════════════════════════════════


class TestN2AbsoluteLimitFallback:
    def test_fallback_ceiling_when_no_caps_and_no_declaration(self):
        """caps 未命中 + family 无 absolute_limit → 兜底 65536"""
        with patch("app.core.workers.chat_worker.get_caps_max_output_tokens", return_value=None):
            from app.core.workers.chat_worker import OpenAIChatWorker

            worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
            worker.llm_config = {"provider_name": "无声明服务商", "模型名称": "m"}
            with patch("app.core.workers.chat_worker.get_provider_profile", return_value={"family": "custom"}):
                assert worker._cap_max_output_tokens("m", 99999999) == mc.ABSOLUTE_FALLBACK_CEILING

    def test_plugin_absolute_limit_takes_precedence(self):
        """插件声明 absolute_limit=200000 → 优先于兜底 65536"""
        with patch("app.core.workers.chat_worker.get_caps_max_output_tokens", return_value=None):
            from app.core.workers.chat_worker import OpenAIChatWorker

            worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
            worker.llm_config = {"provider_name": "插件服务商", "模型名称": "m"}
            with patch(
                "app.core.workers.chat_worker.get_provider_profile",
                return_value={"family": "custom", "absolute_limit": 200000},
            ):
                assert worker._cap_max_output_tokens("m", 99999999) == 200000

    def test_zero_absolute_limit_falls_back_to_ceiling(self):
        """插件 absolute_limit=0（非法/缺省）→ `or` 语义落到兜底常量"""
        with patch("app.core.workers.chat_worker.get_caps_max_output_tokens", return_value=None):
            from app.core.workers.chat_worker import OpenAIChatWorker

            worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
            worker.llm_config = {"provider_name": "X", "模型名称": "m"}
            with patch(
                "app.core.workers.chat_worker.get_provider_profile",
                return_value={"family": "custom", "absolute_limit": 0},
            ):
                assert worker._cap_max_output_tokens("m", 99999999) == mc.ABSOLUTE_FALLBACK_CEILING

    def test_subagent_worker_same_behavior(self):
        """⚠ 同源同改：subagent_worker 的 caps 层与兜底语义必须与 chat_worker 一致

        两处是复制粘贴实现，漏改一处 = 主对话与子智能体输出上限分叉。
        """
        with patch("app.core.workers.subagent_worker.get_caps_max_output_tokens", return_value=524288):
            from app.core.workers.subagent_worker import SubAgentExecutor

            worker = SubAgentExecutor.__new__(SubAgentExecutor)
            worker.llm_config = {"provider_name": "P", "模型名称": "m"}
            assert worker._cap_max_output_tokens("m", 0, None) == 524288
            assert worker._cap_max_output_tokens("m", 999999, None) == 524288

        with patch("app.core.workers.subagent_worker.get_caps_max_output_tokens", return_value=None):
            from app.core.workers.subagent_worker import SubAgentExecutor

            worker = SubAgentExecutor.__new__(SubAgentExecutor)
            worker.llm_config = {"provider_name": "P", "模型名称": "m"}
            with patch(
                "app.core.workers.subagent_worker.get_provider_profile",
                return_value={"family": "custom"},
            ):
                assert worker._cap_max_output_tokens("m", 99999999, None) == mc.ABSOLUTE_FALLBACK_CEILING


# ══════════════════════════════════════════════════════════════════
# N3: resolve 链拿到 caps 值
# ══════════════════════════════════════════════════════════════════


class TestN3ResolveChain:
    def test_resolve_max_output_uses_caps_layer(self):
        """resolve_max_output_tokens L2 命中 caps → 返回 caps 值"""
        with patch.object(
            mc,
            "get_model_capabilities",
            return_value={"max_output_tokens": 524288},
        ):
            assert mc.resolve_max_output_tokens({"模型名称": "space-bunny"}, default=4096) == 524288

    def test_resolve_l1_new_key_wins(self):
        """L1：新键「最大输出」优先于其他键"""
        assert mc.resolve_max_output_tokens({"最大输出": 12345, "最大新Token": 999}) == 12345

    def test_resolve_ignores_old_max_tokens_key(self):
        """P1-21：旧键「最大Token」不再被读成输出上限（语义拆分）"""
        with patch.object(mc, "get_model_capabilities", return_value={"max_output_tokens": 8192}):
            # llm_config 只有旧键「最大Token」= 200000（窗口值）→ 走 caps 的 8192，不读出 200000
            result = mc.resolve_max_output_tokens({"最大Token": 200000, "模型名称": "m"})
            assert result == 8192, f"旧键不应被当输出上限，实际 {result}"

    def test_history_compactor_gets_caps_value(self):
        """N3：history_compactor.get_budget 经 resolve 链拿到 caps 值（预留随之变化）"""
        from app.core.context.history_compactor import HistoryCompactor

        with patch.object(mc, "resolve_max_output_tokens", return_value=524288):
            compactor = HistoryCompactor.__new__(HistoryCompactor)
            compactor._get_model_config = lambda: {"模型名称": "space-bunny"}
            budget = compactor.get_budget({"模型名称": "space-bunny", "最大Token": 1000000})
            # context_limit=1000000（L1 显式），reserved = min(800, 524288) = 800（非 o1/o3）
            assert budget == 1000000 - 800

    def test_fallback_constant_value(self):
        """兜底常量值锁定 65536（改名后不得悄悄改值）"""
        assert mc.ABSOLUTE_FALLBACK_CEILING == 65536


# ══════════════════════════════════════════════════════════════════
# N4: 「最大输出」三层锁
# ══════════════════════════════════════════════════════════════════


class TestN4NewKeyLocks:
    def test_schema_api_param_is_max_tokens(self):
        """锁一：schema 里「最大输出」的 api_param 必须为 max_tokens

        发送链（subagent 通配分支 :1024）按 api_param 收集请求参数——若该值写错，
        新键会静默失效（参数根本不进 req_kwargs）。
        """
        meta = PARAM_SCHEMA.get("最大输出")
        assert meta is not None, "「最大输出」必须存在于 PARAM_SCHEMA"
        assert meta.get("api_param") == "max_tokens", "api_param 必须是 max_tokens（发送链识别依据）"
        assert meta.get("order") == 101, "order 应紧邻「最大Token」(100)"

    def test_subagent_wildcard_path_collects_new_key(self):
        """锁二：subagent 通配分支按 api_param 收集 → 「最大输出」进 req_kwargs 并被钳制"""
        from app.core.workers.subagent_worker import SubAgentExecutor

        collected = {}
        # 复刻 :1012-1027 的收集逻辑（api_param 命中 max_tokens → 进 req_kwargs）
        for cn_key, value in {"最大输出": 999999}.items():
            meta = PARAM_SCHEMA.get(cn_key, {})
            en_key = meta.get("api_param")
            assert en_key == "max_tokens", "通配分支依赖 api_param"
            if en_key in ["temperature", "max_tokens", "top_p"]:
                collected[en_key] = value

        assert "max_tokens" in collected, "新键必须走通配分支进 req_kwargs"

        with patch("app.core.workers.subagent_worker.get_caps_max_output_tokens", return_value=524288):
            worker = SubAgentExecutor.__new__(SubAgentExecutor)
            worker.llm_config = {"provider_name": "P", "模型名称": "m"}
            capped = worker._cap_max_output_tokens("m", collected["max_tokens"], None)
            assert capped == 524288, "经 :1027 钳制后应为 caps 上限"

    def test_missing_api_param_new_key_silently_drops(self):
        """锁三（负例）：新键若不带 api_param → 通配分支静默丢弃（这正是必须写测试的原因）"""
        fake_schema = {**PARAM_SCHEMA, "假新键": {"display_name": "假新键", "ui_type": "spinbox", "order": 999}}
        collected = {}
        for cn_key, value in {"假新键": 123}.items():
            meta = fake_schema.get(cn_key, {})
            en_key = meta.get("api_param")
            if not en_key and cn_key.replace("_", "").isalnum() and not cn_key[0].isdigit():
                # 中文键不匹配 _VALID_IDENTIFIER_PATTERN → en_key 保持 None
                en_key = None
            if not en_key:
                continue
            collected[en_key] = value
        assert collected == {}, "缺 api_param 的键会被静默丢弃（故新键必须声明它）"

    def test_max_token_key_kept_for_context_window(self):
        """「最大Token」仍在 schema 里（作窗口用），但发送链不再读它"""
        assert "最大Token" in PARAM_SCHEMA
        from pathlib import Path

        # 插件目录名 `system-transports` 含中划线，不可直接 import → 读源码断言
        src = Path("plugins/system-transports/transports/openai_chat.py").read_text(encoding="utf-8")
        assert 'llm_config.get("最大输出")' in src, "发送侧应读新键"
        assert 'llm_config.get("最大Token")' not in src, "发送侧不得再直读旧键"

    def test_context_limit_keys_untouched(self):
        """`_CONTEXT_LIMIT_KEYS` 不动（上下文窗口语义保持原样）"""
        assert mc._CONTEXT_LIMIT_KEYS == ("最大Token", "context_limit", "上下文长度", "max_context_tokens")


class TestAliasCollisionGuard:
    """别名收尾：四个键共享 api_param="max_tokens" 时只让「最大输出」胜出

    通配分支按 dict 迭代序赋值 → 后迭代者覆盖先者。窗口键（最大Token / 上下文长度）
    若参与，会把输出值顶掉（用户填了「最大输出」却不生效）。
    """

    def test_window_keys_excluded_from_wildcard(self):
        """复刻通配分支收集逻辑：窗口键被跳过，「最大输出」进 req_kwargs"""
        from app.core.workers.subagent_worker import _CONTEXT_WINDOW_ONLY_KEYS

        # 两键同填（窗口值在字典序上更靠后，未修时它会覆盖输出值）
        config = {"最大输出": 8192, "最大Token": 200000, "上下文长度": 200000}
        req_kwargs = {}
        for cn_key, value in config.items():
            meta = PARAM_SCHEMA.get(cn_key, {})
            en_key = meta.get("api_param")
            if not en_key:
                continue
            if cn_key in _CONTEXT_WINDOW_ONLY_KEYS:
                continue
            if en_key in ["temperature", "max_tokens", "top_p"]:
                req_kwargs[en_key] = value

        assert req_kwargs["max_tokens"] == 8192, f"应取「最大输出」，实际 {req_kwargs['max_tokens']}"

    def test_only_window_keys_filled_sends_nothing(self):
        """只填了窗口键 → 不发 max_tokens（不再被当输出上限）"""
        from app.core.workers.subagent_worker import _CONTEXT_WINDOW_ONLY_KEYS

        config = {"最大Token": 200000}
        req_kwargs = {}
        for cn_key, value in config.items():
            meta = PARAM_SCHEMA.get(cn_key, {})
            en_key = meta.get("api_param")
            if not en_key or cn_key in _CONTEXT_WINDOW_ONLY_KEYS:
                continue
            if en_key in ["temperature", "max_tokens", "top_p"]:
                req_kwargs[en_key] = value

        assert "max_tokens" not in req_kwargs, "窗口值不得被当输出上限发出"

    def test_max_new_tokens_still_allowed(self):
        """英文同义键 max_new_tokens 仍进 max_tokens（不在跳过名单）"""
        from app.core.workers.subagent_worker import _CONTEXT_WINDOW_ONLY_KEYS

        assert "max_new_tokens" not in _CONTEXT_WINDOW_ONLY_KEYS
        config = {"max_new_tokens": 4096}
        req_kwargs = {}
        for cn_key, value in config.items():
            meta = PARAM_SCHEMA.get(cn_key, {})
            en_key = meta.get("api_param")
            if not en_key or cn_key in _CONTEXT_WINDOW_ONLY_KEYS:
                continue
            if en_key in ["temperature", "max_tokens", "top_p"]:
                req_kwargs[en_key] = value
        assert req_kwargs["max_tokens"] == 4096

    def test_skip_list_definition_matches_schema(self):
        """跳过名单的两个键在 schema 里确实共享 max_tokens（防名单写错键名）"""
        from app.core.workers.subagent_worker import _CONTEXT_WINDOW_ONLY_KEYS

        assert _CONTEXT_WINDOW_ONLY_KEYS == frozenset({"最大Token", "上下文长度"})
        for key in _CONTEXT_WINDOW_ONLY_KEYS:
            assert PARAM_SCHEMA.get(key, {}).get("api_param") == "max_tokens", f"{key} 应共享 max_tokens"
