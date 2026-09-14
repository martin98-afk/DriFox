# -*- coding: utf-8 -*-
"""模型列表纯函数操作测试。

覆盖：增量合并保序/去重/大小写、隐藏过滤、别名解析、隐藏集归一。
"""

from app.utils.model_list_ops import (
    display_name,
    filter_visible,
    merge_fetched,
    normalize_hidden,
)


class TestMergeFetched:
    def test_append_new_only(self):
        """拉取结果只追加新项，不动已有项与顺序"""
        assert merge_fetched(["glm-5", "hy3"], ["hy3", "kimi-k2.5"]) == ["glm-5", "hy3", "kimi-k2.5"]

    def test_preserve_local_edits(self):
        """用户手改的条目在拉取后保留（回归：旧实现直接 clear 覆盖）"""
        current = ["我手加的模型", "glm-5"]
        assert merge_fetched(current, ["glm-5", "new-model"]) == ["我手加的模型", "glm-5", "new-model"]

    def test_case_insensitive_dedup(self):
        """大小写不敏感去重（沿用 models.dev 合并口径）"""
        assert merge_fetched(["GLM-5"], ["glm-5", "hy3"]) == ["GLM-5", "hy3"]

    def test_skip_empty_and_whitespace(self):
        """空串与纯空白项丢弃"""
        assert merge_fetched(["a"], ["", "   ", "b"]) == ["a", "b"]

    def test_strip_fetched_items(self):
        """拉取项首尾空白剥离"""
        assert merge_fetched([], ["  glm-5  "]) == ["glm-5"]

    def test_does_not_mutate_inputs(self):
        """不改入参"""
        current = ["a"]
        fetched = ["b"]
        merge_fetched(current, fetched)
        assert current == ["a"]
        assert fetched == ["b"]

    def test_empty_inputs(self):
        assert merge_fetched([], []) == []
        assert merge_fetched(["a"], []) == ["a"]
        assert merge_fetched([], ["a"]) == ["a"]


class TestFilterVisible:
    def test_remove_hidden(self):
        assert filter_visible(["a", "b", "c"], ["b"]) == ["a", "c"]

    def test_case_insensitive(self):
        assert filter_visible(["GLM-5", "hy3"], ["glm-5"]) == ["hy3"]

    def test_empty_hidden_returns_copy(self):
        models = ["a", "b"]
        result = filter_visible(models, [])
        assert result == ["a", "b"]
        assert result is not models

    def test_none_hidden(self):
        assert filter_visible(["a"], None) == ["a"]


class TestDisplayName:
    def test_alias_hit(self):
        assert display_name("glm-5", {"glm-5": "GLM-5 主力"}) == "GLM-5 主力"

    def test_alias_miss_returns_id(self):
        assert display_name("glm-5", {"other": "x"}) == "glm-5"

    def test_empty_alias_value_falls_back_to_id(self):
        """别名值为空串时视为无别名"""
        assert display_name("glm-5", {"glm-5": ""}) == "glm-5"

    def test_none_aliases(self):
        assert display_name("glm-5", None) == "glm-5"


class TestNormalizeHidden:
    def test_drop_ghost_entries(self):
        """隐藏集中不在模型列表里的条目被剔除"""
        assert normalize_hidden(["a", "b"], ["b", "已删除的模型"]) == ["b"]

    def test_dedup_case_insensitive(self):
        assert normalize_hidden(["a", "b"], ["b", "B"]) == ["b"]

    def test_preserve_models_order(self):
        """按模型列表顺序输出，保证结果稳定"""
        assert normalize_hidden(["a", "b", "c"], ["c", "a"]) == ["a", "c"]

    def test_empty_hidden(self):
        assert normalize_hidden(["a"], []) == []

    def test_none_hidden(self):
        assert normalize_hidden(["a"], None) == []
