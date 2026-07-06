# -*- coding: utf-8 -*-
"""Team Template 单元测试。

覆盖范围：
1. Template dataclass：序列化/反序列化、字段校验、agent_name 唯一性
2. TemplateManager：save / load / list / delete / exists / 错误处理
3. 非法模板名（含路径分隔符、特殊字符、空字符串）拒绝
4. YAML 格式错误友好报错
5. 示例默认模板（default-team.yaml）可正常加载

设计说明：
- 使用 tmp_path + monkeypatch 隔离文件操作，不污染真实项目目录
- 每次测试前重置 TemplateManager 单例（_instance = None）避免残留
"""

from __future__ import annotations

import pytest

from app.core.team.template_manager import TemplateManager
from app.core.team.template_schema import (
    SUPPORTED_SCHEMA_VERSIONS,
    Template,
    TemplateAgent,
    TemplateError,
)


# ══════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════


@pytest.fixture
def fresh_tm(monkeypatch, tmp_path):
    """每次返回指向 tmp_path 的全新 TemplateManager 实例。"""
    TemplateManager._instance = None
    tm = TemplateManager.get_instance()
    # 重定向目录到 tmp_path，避免污染真实 plugins/system/team_templates/
    tm._templates_dir = tmp_path
    tm._templates_dir.mkdir(parents=True, exist_ok=True)
    return tm


@pytest.fixture
def basic_template() -> Template:
    """构造一个合法模板。"""
    return Template(
        template_name="basic",
        description="basic team",
        agents=[TemplateAgent("build"), TemplateAgent("review")],
    )


# ══════════════════════════════════════════════════════════
# 1. Template dataclass：基础序列化
# ══════════════════════════════════════════════════════════


class TestTemplateDataclass:
    def test_to_dict_roundtrip(self):
        """to_dict → from_dict 应保持语义一致。"""
        original = Template(
            template_name="t1",
            description="desc",
            agents=[TemplateAgent("build"), TemplateAgent("review")],
        )
        d = original.to_dict()
        assert d["schema_version"] == 1
        assert d["template_name"] == "t1"
        assert d["agents"] == [{"agent_name": "build"}, {"agent_name": "review"}]

        restored = Template.from_dict(d)
        assert restored.template_name == original.template_name
        assert restored.description == original.description
        assert len(restored.agents) == len(original.agents)
        assert [a.agent_name for a in restored.agents] == [a.agent_name for a in original.agents]

    def test_default_schema_version(self):
        """未指定 schema_version 时默认为 1。"""
        t = Template.from_dict(
            {
                "template_name": "x",
                "agents": [{"agent_name": "build"}],
            }
        )
        assert t.schema_version == 1

    def test_unsupported_schema_version_rejected(self):
        """不支持的 schema_version 必须抛 TemplateError。"""
        with pytest.raises(TemplateError, match="不支持的 schema_version"):
            Template.from_dict(
                {
                    "schema_version": 99,
                    "template_name": "x",
                    "agents": [{"agent_name": "build"}],
                }
            )

    def test_empty_agents_rejected(self):
        """agents 列表不能为空。"""
        with pytest.raises(TemplateError, match="agents 列表不能为空"):
            Template.from_dict(
                {
                    "template_name": "x",
                    "agents": [],
                }
            )

    def test_missing_agents_rejected(self):
        """agents 字段缺失时视为空列表，应抛错。"""
        with pytest.raises(TemplateError):
            Template.from_dict({"template_name": "x"})

    def test_duplicate_agent_names_rejected(self):
        """agent_name 重复必须抛错。"""
        with pytest.raises(TemplateError, match="agent_name 重复"):
            Template.from_dict(
                {
                    "template_name": "x",
                    "agents": [
                        {"agent_name": "build"},
                        {"agent_name": "build"},
                    ],
                }
            )

    def test_blank_agent_name_rejected(self):
        """agent_name 为空白字符串必须抛错。"""
        with pytest.raises(TemplateError, match="agent_name 必须是非空字符串"):
            Template.from_dict(
                {
                    "template_name": "x",
                    "agents": [{"agent_name": "  "}],
                }
            )

    def test_non_string_template_name_rejected(self):
        """template_name 必须是字符串。"""
        with pytest.raises(TemplateError, match="template_name 必须是字符串"):
            Template.from_dict(
                {
                    "template_name": 123,
                    "agents": [{"agent_name": "build"}],
                }
            )

    def test_top_level_must_be_dict(self):
        """顶层必须是 dict。"""
        with pytest.raises(TemplateError, match="顶层必须是对象"):
            Template.from_dict([1, 2, 3])  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════
# 2. Template.validate_agent_names
# ══════════════════════════════════════════════════════════


class TestValidateAgentNames:
    def test_returns_empty_when_all_known(self):
        t = Template(
            template_name="t",
            agents=[TemplateAgent("build"), TemplateAgent("review")],
        )
        missing = t.validate_agent_names(["build", "review", "plan"])
        assert missing == []

    def test_returns_unknown_names(self):
        t = Template(
            template_name="t",
            agents=[TemplateAgent("build"), TemplateAgent("ghost")],
        )
        missing = t.validate_agent_names(["build", "plan"])
        assert missing == ["ghost"]

    def test_none_means_skip(self):
        """available_agent_names 为 None 时跳过校验。"""
        t = Template(template_name="t", agents=[TemplateAgent("anything")])
        assert t.validate_agent_names(None) == []


# ══════════════════════════════════════════════════════════
# 3. TemplateManager.save / load
# ══════════════════════════════════════════════════════════


class TestSaveLoad:
    def test_save_writes_yaml(self, fresh_tm, basic_template, tmp_path):
        path = fresh_tm.save(basic_template)
        assert path.exists()
        assert path.parent == tmp_path
        assert path.name == "basic.yaml"

    def test_load_returns_template(self, fresh_tm, basic_template):
        fresh_tm.save(basic_template)
        loaded = fresh_tm.load("basic")
        assert loaded.template_name == "basic"
        assert loaded.description == "basic team"
        assert [a.agent_name for a in loaded.agents] == ["build", "review"]

    def test_load_missing_raises(self, fresh_tm):
        with pytest.raises(TemplateError, match="模板不存在"):
            fresh_tm.load("does_not_exist")

    def test_load_corrupt_yaml_raises(self, fresh_tm, tmp_path):
        """YAML 语法错误应友好报错（不抛 yaml.YAMLError 原始异常）。"""
        (tmp_path / "broken.yaml").write_text(
            "schema_version: 1\ntemplate_name: broken\nagents: [\n",
            encoding="utf-8",
        )
        with pytest.raises(TemplateError, match="YAML 解析失败"):
            fresh_tm.load("broken")

    def test_load_empty_file_raises(self, fresh_tm, tmp_path):
        (tmp_path / "empty.yaml").write_text("", encoding="utf-8")
        with pytest.raises(TemplateError, match="模板文件为空"):
            fresh_tm.load("empty")

    def test_load_non_dict_top_level_raises(self, fresh_tm, tmp_path):
        (tmp_path / "list_top.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
        with pytest.raises(TemplateError, match="顶层必须是对象"):
            fresh_tm.load("list_top")

    def test_save_overwrites_existing(self, fresh_tm):
        t1 = Template(
            template_name="t",
            description="v1",
            agents=[TemplateAgent("build")],
        )
        fresh_tm.save(t1)
        t2 = Template(
            template_name="t",
            description="v2",
            agents=[TemplateAgent("review")],
        )
        fresh_tm.save(t2)
        loaded = fresh_tm.load("t")
        assert loaded.description == "v2"
        assert [a.agent_name for a in loaded.agents] == ["review"]


# ══════════════════════════════════════════════════════════
# 4. TemplateManager.list_templates
# ══════════════════════════════════════════════════════════


class TestListTemplates:
    def test_empty_directory(self, fresh_tm):
        assert fresh_tm.list_templates() == []

    def test_lists_multiple(self, fresh_tm):
        fresh_tm.save(
            Template(
                template_name="a",
                agents=[TemplateAgent("build")],
            )
        )
        fresh_tm.save(
            Template(
                template_name="b",
                description="B team",
                agents=[TemplateAgent("build"), TemplateAgent("review")],
            )
        )
        results = fresh_tm.list_templates()
        names = [r["name"] for r in results]
        assert names == ["a", "b"]  # sorted

        b = next(r for r in results if r["name"] == "b")
        assert b["description"] == "B team"
        assert b["agent_count"] == 2
        assert b["agent_names"] == ["build", "review"]

    def test_skips_corrupt_files(self, fresh_tm, tmp_path):
        """损坏的 YAML 不应让整个列表失败，而是被跳过 + 警告。"""
        fresh_tm.save(
            Template(
                template_name="good",
                agents=[TemplateAgent("build")],
            )
        )
        (tmp_path / "bad.yaml").write_text("this is: : invalid", encoding="utf-8")
        results = fresh_tm.list_templates()
        names = [r["name"] for r in results]
        assert "good" in names
        assert "bad" not in names


# ══════════════════════════════════════════════════════════
# 5. TemplateManager.delete
# ══════════════════════════════════════════════════════════


class TestDelete:
    def test_delete_existing(self, fresh_tm, basic_template, tmp_path):
        fresh_tm.save(basic_template)
        assert fresh_tm.delete("basic") is True
        assert not (tmp_path / "basic.yaml").exists()
        assert fresh_tm.exists("basic") is False

    def test_delete_missing_returns_false(self, fresh_tm):
        """删除不存在的模板应返回 False 而非抛错。"""
        assert fresh_tm.delete("never_existed") is False


# ══════════════════════════════════════════════════════════
# 6. TemplateManager.exists
# ══════════════════════════════════════════════════════════


class TestExists:
    def test_exists_true(self, fresh_tm, basic_template):
        fresh_tm.save(basic_template)
        assert fresh_tm.exists("basic") is True

    def test_exists_false(self, fresh_tm):
        assert fresh_tm.exists("nope") is False

    def test_exists_with_invalid_name_returns_false(self, fresh_tm):
        """非法名称（如包含路径分隔符）应返回 False 而非抛错。"""
        assert fresh_tm.exists("../etc/passwd") is False
        assert fresh_tm.exists("") is False


# ══════════════════════════════════════════════════════════
# 7. 非法名称校验
# ══════════════════════════════════════════════════════════


class TestInvalidName:
    def test_empty_name_rejected(self, fresh_tm):
        with pytest.raises(TemplateError, match="模板名不能为空"):
            fresh_tm.save(Template(template_name="", agents=[TemplateAgent("build")]))

    def test_path_traversal_rejected(self, fresh_tm):
        """'../bad' 必须被拒绝，避免写入到项目外。"""
        with pytest.raises(TemplateError, match="模板名非法"):
            fresh_tm.save(
                Template(
                    template_name="../bad",
                    agents=[TemplateAgent("build")],
                )
            )

    def test_non_ascii_name_rejected(self, fresh_tm):
        """中文名应被拒绝（保持跨平台稳定性）。"""
        with pytest.raises(TemplateError, match="模板名非法"):
            fresh_tm.save(
                Template(
                    template_name="中文模板",
                    agents=[TemplateAgent("build")],
                )
            )

    def test_name_with_dot_rejected(self, fresh_tm):
        """包含 . 的名称应被拒绝（避免和扩展名冲突）。"""
        with pytest.raises(TemplateError, match="模板名非法"):
            fresh_tm.save(
                Template(
                    template_name="my.template",
                    agents=[TemplateAgent("build")],
                )
            )

    def test_name_too_long_rejected(self, fresh_tm):
        with pytest.raises(TemplateError, match="模板名非法"):
            fresh_tm.save(
                Template(
                    template_name="a" * 65,
                    agents=[TemplateAgent("build")],
                )
            )

    def test_hyphen_and_underscore_allowed(self, fresh_tm):
        """合法的 hyphen/underscore 应通过。"""
        path = fresh_tm.save(
            Template(
                template_name="my-team_v2",
                agents=[TemplateAgent("build")],
            )
        )
        assert path.exists()


# ══════════════════════════════════════════════════════════
# 8. 示例默认模板（plugins/system/team_templates/default-team.yaml）
# ══════════════════════════════════════════════════════════


class TestBundledDefaultTemplate:
    """验证项目内置的 default-team.yaml 模板能被正常加载。

    注意：本测试不写入 tmp_path，直接读取项目内的示例。
    """

    def test_default_team_template_loads(self):
        """默认模板必须可加载，且 schema_version 在支持列表中。"""
        from app.core.team.template_manager import TemplateManager as RealTM

        # 用真实单例（指向项目内目录）
        RealTM._instance = None
        tm = RealTM.get_instance()
        if not tm.exists("default-team"):
            pytest.skip("默认模板未找到（可能未安装）")

        template = tm.load("default-team")
        assert template.schema_version in SUPPORTED_SCHEMA_VERSIONS
        assert len(template.agents) >= 1
        for a in template.agents:
            assert a.agent_name
            assert isinstance(a.agent_name, str)
