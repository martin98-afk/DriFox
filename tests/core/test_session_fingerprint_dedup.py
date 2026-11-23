# -*- coding: utf-8 -*-
"""末条消息内容指纹的去重有效性回归测试（A5）。

背景：`SessionRepository.save` 用「(消息条数, 末条指纹)」做增量内容键，
内容未变时跳过昂贵的 serialize + zstd + 写盘。

两处必须锁住的行为：

1. **deepcopy 后仍能去重**：`history_manager` 每次保存都对 messages 做
   `copy.deepcopy`，末条每次都是新对象。指纹里**不能有 `id(msg)`**，
   否则内容键永不相等，去重短路永久失效（实测比不优化更慢）。
2. **嵌套结构中间元素变化必须改指纹**：末条 `content` / `tool_calls` 常为
   `list[dict]`，流式增长发生在中间元素的文本上。只比容器规模会漏判 →
   跳过保存 → 崩溃窗口丢流式中间态。

这两个用例在「含 id 分量」或「嵌套只看规模」的实现上都会失败。
"""

import copy

from app.core.store.session_repository import _light_msg_fingerprint


class TestDeepcopyStillDedups:
    """指纹必须只依赖内容，不依赖对象身份。"""

    def test_deepcopy_same_content_same_fingerprint(self):
        """deepcopy 出的等价对象，指纹必须完全相同。"""
        msg = {"role": "assistant", "content": "hello world", "idx": 3}
        cloned = copy.deepcopy(msg)

        assert cloned is not msg, "前置条件：deepcopy 必须产生新对象"
        assert _light_msg_fingerprint(msg) == _light_msg_fingerprint(cloned), (
            "内容相同但对象不同时指纹必须相等，否则保存去重永久失效"
        )

    def test_deepcopy_repeated_ten_times_stable(self):
        """连续 deepcopy 10 次，指纹稳定不变（覆盖多次保存的场景）。"""
        base = {"role": "user", "content": [{"type": "text", "text": "hi"}], "n": 1}
        prints = {_light_msg_fingerprint(copy.deepcopy(base)) for _ in range(10)}
        assert len(prints) == 1, f"10 次 deepcopy 应得到同一指纹，实际 {len(prints)} 种"

    def test_fingerprint_survives_json_roundtrip(self):
        """JSON 往返（另一种常见的「内容相同对象不同」）也不应改变指纹。"""
        import json

        msg = {"role": "assistant", "content": "abc", "tool_calls": [{"id": "t1", "name": "read"}]}
        roundtrip = json.loads(json.dumps(msg))
        assert _light_msg_fingerprint(msg) == _light_msg_fingerprint(roundtrip)


class TestNestedContentSensitivity:
    """嵌套结构的内容变化必须改变指纹（防漏存）。

    ⚠️ 每条用例都带 `_PAD` 字段：指纹有「小消息快速路径」（字段数 ≤8 且无长
    内容时直接 `hash(str(msg))`），不加 pad 时这些用例全走 str() 兜底，
    `_fp_hash_value` 的 list/dict 递归分支根本没执行 —— 测试会空转通过。
    `_PAD` 40000 字节 > `_FP_FAST_PATH_BYTES`(32768)，强制命中分片路径。
    """

    _PAD = "x" * 40000

    def test_list_middle_text_growth_changes_fingerprint(self):
        """list 中间元素的文本增长 → 指纹必须变（流式核心场景）。"""
        before = {
            "role": "assistant",
            "_pad": self._PAD,
            "content": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "streaming"},
                {"type": "text", "text": "z"},
            ],
        }
        after = copy.deepcopy(before)
        after["content"][1]["text"] = "streaming and growing more"  # 中间元素增长

        assert _light_msg_fingerprint(before) != _light_msg_fingerprint(after), (
            "中间元素文本增长必须改变指纹，否则流式中间态会被跳过保存"
        )

    def test_list_same_length_different_text_changes_fingerprint(self):
        """list 中间元素文本「等长替换」也必须改变指纹。"""
        before = {
            "_pad": self._PAD,
            "content": [{"text": "aaa"}, {"text": "bbb"}, {"text": "ccc"}],
        }
        after = copy.deepcopy(before)
        after["content"][1]["text"] = "bXb"  # 等长、中间位置

        assert _light_msg_fingerprint(before) != _light_msg_fingerprint(after)

    def test_nested_dict_value_change_detected(self):
        """深层 dict 的标量变化必须被检出。"""
        before = {
            "_pad": self._PAD,
            "tool_result": {"data": {"inner": {"code": 200, "msg": "ok"}}},
        }
        after = copy.deepcopy(before)
        after["tool_result"]["data"]["inner"]["code"] = 500

        assert _light_msg_fingerprint(before) != _light_msg_fingerprint(after)

    def test_tool_calls_args_growth_detected(self):
        """tool_calls[].function.arguments 增长必须被检出（流式工具入参）。"""
        before = {
            "_pad": self._PAD,
            "tool_calls": [{"function": {"name": "write", "arguments": '{"a": 1'}}],
        }
        after = {
            "_pad": self._PAD,
            "tool_calls": [{"function": {"name": "write", "arguments": '{"a": 1, "b": 2}'}}],
        }

        assert _light_msg_fingerprint(before) != _light_msg_fingerprint(after)

    def test_list_length_change_detected(self):
        """list 元素数量变化必须被检出。"""
        assert _light_msg_fingerprint({"_pad": self._PAD, "c": [1, 2]}) != _light_msg_fingerprint(
            {"_pad": self._PAD, "c": [1, 2, 3]}
        )

    def test_list_element_equal_length_replacement_detected(self):
        """list 元素**等长替换**必须被检出（只留规模的实现检不出，这是核心锁点）。"""
        before = {"_pad": self._PAD, "c": [1, 2, 3]}
        after = {"_pad": self._PAD, "c": [1, 9, 3]}  # 长度不变，中间元素变
        assert _light_msg_fingerprint(before) != _light_msg_fingerprint(after)

    def test_bigram_dict_key_rename_detected(self):
        """dict 键改名（值不变）必须被检出。"""
        assert _light_msg_fingerprint({"_pad": self._PAD, "c": {"a": 1}}) != _light_msg_fingerprint(
            {"_pad": self._PAD, "c": {"b": 1}}
        )

    def test_pad_actually_forces_chunking_path(self):
        """守卫用例：确认 _PAD 真的让指纹走分片路径（否则上面 6 条都是空转）。

        分片路径的标志：指纹首元素是 "n"；小消息快速路径首元素是 "small"。
        """
        msg_with_pad = {"_pad": self._PAD, "c": [1, 2]}
        msg_small = {"c": [1, 2]}
        assert _light_msg_fingerprint(msg_with_pad)[0] == "n", "带 pad 必须走分片路径"
        assert _light_msg_fingerprint(msg_small)[0] == "small", "不帶 pad 应走小消息快速路径"


class TestSafetyAndBudget:
    """预算与异常安全：不得因异常或超深结构炸掉保存链。"""

    def test_deep_nesting_terminates(self):
        """超深嵌套不得无限递归（预算降级）。"""
        deep = node = {}
        for _ in range(50):
            node["n"] = {}
            node = node["n"]

        fp = _light_msg_fingerprint({"content": deep})
        assert fp is not None

    def test_huge_list_bounded(self):
        """超长 list 不得让指纹成本失控（节点预算生效）。"""
        big = {"content": [{"text": str(i)} for i in range(5000)]}
        fp = _light_msg_fingerprint(big)
        assert fp is not None

    def test_non_dict_message_does_not_raise(self):
        """末条不是 dict（str/None）时不得抛异常。"""
        for val in ("plain string", None, 42, ["list"]):
            assert _light_msg_fingerprint(val) is not None

    def test_bytes_content_handled(self):
        """bytes 内容走长度+首尾哈希分支。"""
        a = _light_msg_fingerprint({"content": b"x" * 100})
        b = _light_msg_fingerprint({"content": b"x" * 100})
        c = _light_msg_fingerprint({"content": b"y" * 100})
        assert a == b
        assert a != c


class TestTruncatedFingerprintSensitivity:
    """降级摘要（depth>6 / nodes>2000）对同长度不同内容的敏感性。

    旧实现只取 `str(val)[:1024]`：`"a"*2000 + "x"` 与 `"a"*2000 + "y"`
    的降级摘要相同 → save() 误判「未变」→ 跳过写盘（崩溃窗口丢数据）。
    现实现改长度 + 头/中/尾各 1KB 采样，尾部差异可区分。
    """

    @staticmethod
    def _deep(value, layers=8):
        for _ in range(layers):
            value = {"k": value}
        return value

    def test_same_length_tail_differs_is_distinguished(self):
        """≤3KB 走整串哈希：尾部差异必然区分（分支覆盖：短值路径）。"""
        from app.core.store.session_repository import _fp_hash_value

        a = _fp_hash_value(self._deep("a" * 2000 + "x"), 0, [10 ** 9])
        b = _fp_hash_value(self._deep("a" * 2000 + "y"), 0, [10 ** 9])
        assert a != b, "同长度、前 1KB 相同的尾部差异必须被短值整串哈希区分"

    def test_three_window_catches_head_and_tail_diff(self):
        """A) >3KB 走头/中/尾三窗：头窗或尾窗内的差异必须区分。

        5000 字节 > 3×1024，强制进入三窗采样分支（短值整串哈希覆盖不到）。
        """
        from app.core.store.session_repository import _fp_hash_value

        head_a = _fp_hash_value(self._deep("x" + "a" * 4999), 0, [10 ** 9])
        head_b = _fp_hash_value(self._deep("y" + "a" * 4999), 0, [10 ** 9])
        assert head_a != head_b, "头窗内的差异必须被三窗采样区分"

        tail_a = _fp_hash_value(self._deep("a" * 4999 + "x"), 0, [10 ** 9])
        tail_b = _fp_hash_value(self._deep("a" * 4999 + "y"), 0, [10 ** 9])
        assert tail_a != tail_b, "尾窗内的差异必须被三窗采样区分"

    def test_known_gap_collision_between_windows(self):
        """B) 已知边界：差异落在三窗之间的间隙 → 仍碰撞，断言「相同」。

        三窗 = 头 [0,1024) / 中 [mid-512, mid+512) / 尾 [-1024,)。5000 字节时
        中窗约 [1988,3012)，把 50 字节变化放在 1100~1150 就落在窗间间隙。
        这是**已知且接受**的碰撞面（非 bug）：碰撞只会让一次 save 被跳过，
        后续内容变化会整篇补写。改动采样策略（如加窗）必须同步更新本用例。
        """
        from app.core.store.session_repository import _fp_hash_value

        gap_a = _fp_hash_value(self._deep("a" * 1100 + "x" * 50 + "a" * 3850), 0, [10 ** 9])
        gap_b = _fp_hash_value(self._deep("a" * 1100 + "y" * 50 + "a" * 3850), 0, [10 ** 9])
        assert gap_a == gap_b, "窗间间隙内的差异按设计仍碰撞（已知边界）"

    def test_truncated_fingerprint_stable(self):
        from app.core.store.session_repository import _fp_hash_value

        val = self._deep("a" * 4999 + "x")
        prints = {_fp_hash_value(val, 0, [10 ** 9]) for _ in range(5)}
        assert len(prints) == 1, "同一结构重复计算必须稳定"
