# -*- coding: utf-8 -*-
"""Team Template 单元测试。

覆盖范围：
1. Template dataclass：序列化/反序列化、字段校验、agent_name 唯一性
2. TemplateManager：save / load / list / delete / exists / 错误处理
3. 非法模板名（含路径分隔符、特殊字符、空字符串）拒绝
4. YAML 格式错误友好报错
5. 示例默认模板（default-team.yaml）可正常加载
6. 回归：join_team 不销毁 mailbox（review 修复 1）
7. 回归：description 计数（review 修复 3）
8. 回归：--load QMessageBox 确认（review 修复 2）
9. 回归：300ms 类常量（review 修复 4）

设计说明：
- 使用 tmp_path + monkeypatch 隔离文件操作，不污染真实项目目录
- 每次测试前重置 TemplateManager 单例（_instance = None）避免残留
- 回归测试用 AST 静态校验源码，防止修复被无意回退
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from app.core.team.template_manager import TemplateManager
from app.core.team.template_schema import (
    SUPPORTED_SCHEMA_VERSIONS,
    Template,
    TemplateAgent,
    TemplateError,
)

# 工具插件化：TeamTools 类已删除，team_list_members 迁移为
# plugins/system/tools/subagent_tools.py 模块级函数（tool_ctx 签名）。
# 复用 _load_module 模式加载插件模块（plugins/ 非 Python 包）。
_PLUGIN_TOOLS = Path(__file__).resolve().parent.parent.parent / "plugins" / "system" / "tools"


def _load_subagent_tools():
    mod_name = "_team_template_subagent_tools"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_TOOLS / "subagent_tools.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════


@pytest.fixture
def fresh_tm(monkeypatch, tmp_path):
    """每次返回指向 tmp_path 的全新 TemplateManager 实例。"""
    TemplateManager._instance = None
    tm = TemplateManager.get_instance()
    # 重定向 user 目录到 tmp_path（save/load/delete 实际使用 _get_user_dir），
    # system 目录指向 tmp_path/system 子目录，避免真实系统模板干扰列表断言
    monkeypatch.setattr(tm, "_get_user_dir", lambda: tmp_path)
    monkeypatch.setattr(tm, "_system_dir", tmp_path / "system")
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
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

    def test_agent_description_roundtrip(self):
        """角色描述应随 to_dict/from_dict 保留；空描述不输出（兼容旧格式）。"""
        original = Template(
            template_name="t1",
            description="desc",
            agents=[TemplateAgent("leader", "统筹团队任务拆解/分发/汇总"), TemplateAgent("build")],
        )
        d = original.to_dict()
        # 有描述的条目输出 description，无描述的保持旧格式
        assert d["agents"][0] == {"agent_name": "leader", "description": "统筹团队任务拆解/分发/汇总"}
        assert d["agents"][1] == {"agent_name": "build"}

        restored = Template.from_dict(d)
        assert restored.agents[0].description == "统筹团队任务拆解/分发/汇总"
        assert restored.agents[1].description == ""

    def test_agent_description_from_dict_accepts_missing(self):
        """旧模板 agents 条目无 description 时默认为空字符串，不报错。"""
        t = Template.from_dict(
            {
                "template_name": "legacy",
                "agents": [{"agent_name": "build"}],
            }
        )
        assert t.agents[0].description == ""

    def test_agent_description_non_string_rejected(self):
        """description 非字符串必须抛 TemplateError。"""
        with pytest.raises(TemplateError, match="description 必须是字符串"):
            Template.from_dict(
                {
                    "template_name": "x",
                    "agents": [{"agent_name": "build", "description": 123}],
                }
            )

    def test_agent_description_whitespace_stripped(self):
        """description 首尾空白应被去除。"""
        t = Template.from_dict(
            {
                "template_name": "x",
                "agents": [{"agent_name": "build", "description": "  负责编码实现  "}],
            }
        )
        assert t.agents[0].description == "负责编码实现"

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

    def test_save_load_with_agent_descriptions(self, fresh_tm):
        """带角色描述的模板保存/加载应完整保留 description。"""
        t = Template(
            template_name="desc-team",
            description="带角色描述的团队",
            agents=[
                TemplateAgent("leader", "统筹团队任务拆解/分发/汇总"),
                TemplateAgent("build", "负责编码实现与验证"),
            ],
        )
        fresh_tm.save(t)
        loaded = fresh_tm.load("desc-team")
        assert [a.agent_name for a in loaded.agents] == ["leader", "build"]
        assert [a.description for a in loaded.agents] == ["统筹团队任务拆解/分发/汇总", "负责编码实现与验证"]

    def test_save_with_description_omits_empty_in_yaml(self, fresh_tm, tmp_path):
        """agent 描述为空时 YAML 的 agents 条目不应包含 description 键（保持旧模板简洁）。"""
        fresh_tm.save(
            Template(
                template_name="no-desc",
                agents=[TemplateAgent("build")],
            )
        )
        text = (tmp_path / "no-desc.yaml").read_text(encoding="utf-8")
        # agents 条目不应携带空 description 键（顶层 Template.description 允许为空串）
        agents_block = text.split("agents:", 1)[1]
        assert "- agent_name: build\n" in agents_block
        assert "description" not in agents_block

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


class TestUserTemplatesWithoutPluginRegistration:
    """回归：user-custom 插件未注册时，用户模板列表仍应可见。

    修复前 _get_user_dir() 依赖 PluginManager.get_plugin("user-custom")：
    - user-custom 插件的 manifest（.drifox-plugin/plugin.json）是按需创建的
      （添加 MCP / 保存快捷键时才生成），用户仅保存过团队模板时插件未注册，
      get_plugin() 返回 None → 模板列表缺失；
    - 首次解析若发生在 PluginManager 未初始化时，还会缓存 None 导致
      后续永远解析失败（缓存毒化）。
    修复后直接基于应用数据目录解析，不再依赖插件注册状态。
    """

    def test_user_dir_ignores_plugin_registration(self, tmp_path, monkeypatch):
        """user-custom 插件未注册时，_get_user_dir 仍应解析出模板目录。

        修复前依赖 PluginManager.get_plugin("user-custom")，未注册时返回 None；
        修复后直接基于应用数据目录解析，与插件注册状态、初始化时机无关。
        """
        import app.utils.utils as utils_mod

        # 重定向应用数据目录到 tmp_path
        monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: tmp_path)

        TemplateManager._instance = None
        tm = TemplateManager.get_instance()
        user_dir = tm.user_dir
        assert user_dir is not None
        assert user_dir == tmp_path / "plugins" / "user-custom" / "team_templates"
        assert user_dir.exists()  # 目录被创建

    def test_list_includes_user_templates_without_registration(self, tmp_path, monkeypatch):
        """插件未注册时，list_templates 应包含 user-custom 目录中的模板。"""
        import app.utils.utils as utils_mod

        monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: tmp_path)

        # user-custom 模板目录（先于 PluginManager 扫描存在，如云端恢复场景）
        user_tpl_dir = tmp_path / "plugins" / "user-custom" / "team_templates"
        user_tpl_dir.mkdir(parents=True, exist_ok=True)
        (user_tpl_dir / "cloud-team.yaml").write_text(
            "schema_version: 1\n"
            "template_name: cloud-team\n"
            "description: 云端恢复的模板\n"
            "agents:\n"
            "  - agent_name: build\n"
            "  - agent_name: review\n",
            encoding="utf-8",
        )

        TemplateManager._instance = None
        tm = TemplateManager.get_instance()
        # 隔离系统/插件来源，只验证 user 来源
        monkeypatch.setattr(tm, "_get_plugin_template_dirs", lambda: [])
        monkeypatch.setattr(tm, "_system_dir", tmp_path / "system")

        results = tm.list_templates()
        names = [r["name"] for r in results]
        assert "cloud-team" in names
        cloud = next(r for r in results if r["name"] == "cloud-team")
        assert cloud["source"] == tm.SOURCE_USER
        assert cloud["agent_count"] == 2


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

    def test_non_ascii_name_allowed(self, fresh_tm):
        """中文名应被允许（放宽支持 Unicode 名称）。"""
        path = fresh_tm.save(
            Template(
                template_name="中文模板",
                agents=[TemplateAgent("build")],
            )
        )
        assert path.exists()
        assert path.stem == "中文模板"
        # 往返：保存后能从文件名正确加载回来（本次 bug 的核心场景）
        reloaded = fresh_tm.load("中文模板")
        assert reloaded.template_name == "中文模板"
        assert reloaded.agents[0].agent_name == "build"

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


# ══════════════════════════════════════════════════════════
# 9. 回归：join_team 不应删除 mailbox（修复 review 问题 1）
# ══════════════════════════════════════════════════════════


class TestJoinTeamPreservesMailbox:
    """修复说明：_handle_team_load 在 review 后改为只 join_team，不再 leave_team。

    验证前提：TeamManager.join_team 不会触发 rmtree(mailbox_dir)，
    反复 join 同一 window_id 也不应丢失已有邮件。
    """

    def test_join_team_keeps_existing_mail_files(self, tmp_path, monkeypatch):
        """多次 join 同一 window_id 不会清空 mailbox 目录。

        注意：join_team 内部会通过 _get_team_data → _cleanup_stale_members 触发清理逻辑，
        但仅清理「不在活跃窗口集合中的 stale 成员」。只要主窗口正常调用
        set_active_window_ids() 同步活跃集合，已 join 窗口不会被误判为 stale。
        """
        # 重定向 TeamManager 的数据目录到 tmp_path（不污染真实 ~/.drifox/）
        from app.core import team_manager as tm_mod

        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path))
        tm_mod.TeamManager._instance = None
        tm = tm_mod.TeamManager.get_instance()

        # 关键：设置 win_01 为活跃窗口（模拟主窗口的 set_active_window_ids）
        tm.set_active_window_ids({"win_01"})

        # 第一次 join
        tm.join_team(window_id="win_01", agent_name="build")
        # 模拟有邮件写入
        mailbox_dir = tm._mailbox_dir(tm.DEFAULT_TEAM, "win_01")
        mail_file = mailbox_dir / "mail_test_001.json"
        mail_file.write_text('{"id":"mail_test_001","body":"hello"}', encoding="utf-8")
        assert mail_file.exists()

        # 第二次 join 同 window（不 leave，模拟 load 时的覆盖语义）
        tm.join_team(window_id="win_01", agent_name="review")
        # 关键断言：mail 文件不能被销毁
        assert mail_file.exists(), "join_team 重复调用不应清空 mailbox"
        assert "hello" in mail_file.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════
# 9b. 回归：活跃窗口集合未知/为空时，绝不允许清空成员与邮箱
# ══════════════════════════════════════════════════════════


class TestStaleCleanupSafety:
    """线上故障：所有成员邮箱目录被整体删除，QFileSystemWatcher 报
    `FindNextChangeNotification failed ... (拒绝访问)`，成员随后无法交互。

    根因：_get_active_windows() 在活跃集合未同步时返回空集，
    _cleanup_stale_members() 把「空集」当成「没有窗口活着」，
    于是把全部在册成员判为 stale 并 rmtree 掉它们的邮箱目录。
    """

    @staticmethod
    def _fresh_tm(tmp_path, monkeypatch):
        from app.core import team_manager as tm_mod

        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path))
        tm_mod.TeamManager._instance = None
        return tm_mod.TeamManager.get_instance()

    def test_never_wipes_when_active_set_unknown(self, tmp_path, monkeypatch):
        """从未同步过活跃窗口 → 清理必须整体跳过。"""
        tm = self._fresh_tm(tmp_path, monkeypatch)
        tm.set_active_window_ids({"win_01"})
        tm.join_team(window_id="win_01", agent_name="build")
        mail = tm._mailbox_dir(tm.DEFAULT_TEAM, "win_01") / "mail_x.json"
        mail.write_text("{}", encoding="utf-8")

        # 模拟单例被重建（进程内 reset / 首次在 worker 线程访问）
        tm2 = self._fresh_tm(tmp_path, monkeypatch)
        assert tm2._get_active_windows() is None, "活跃集合未知时必须返回 None 而非空集"
        assert mail.exists(), "活跃窗口未知时不得删除任何邮箱目录"
        assert tm2.is_team_member("win_01"), "活跃窗口未知时不得移除成员记录"

    def test_empty_sync_does_not_clear_known_active_set(self, tmp_path, monkeypatch):
        """空集合同步（窗口 __init__ 时序竞态）不得覆盖已知集合，也不得触发清理。"""
        tm = self._fresh_tm(tmp_path, monkeypatch)
        tm.set_active_window_ids({"win_01"})
        tm.join_team(window_id="win_01", agent_name="build")
        mail = tm._mailbox_dir(tm.DEFAULT_TEAM, "win_01") / "mail_x.json"
        mail.write_text("{}", encoding="utf-8")

        tm.set_active_window_ids(set())

        assert tm._get_active_windows() == {"win_01"}
        assert mail.exists(), "空集合同步不应删除在用邮箱目录"
        assert tm.is_team_member("win_01")

    def test_read_paths_do_not_trigger_cleanup(self, tmp_path, monkeypatch):
        """check_team_member 等读取路径（可能在 worker 线程）不得触发删除。"""
        tm = self._fresh_tm(tmp_path, monkeypatch)
        tm.set_active_window_ids({"win_01"})
        tm.join_team(window_id="win_01", agent_name="build")
        tm.join_team(window_id="win_02", agent_name="review")  # 尚未同步进活跃集
        mail2 = tm._mailbox_dir(tm.DEFAULT_TEAM, "win_02") / "mail_y.json"
        mail2.write_text("{}", encoding="utf-8")

        # 大量读取不应产生任何副作用
        for _ in range(5):
            tm.is_team_member("win_01")
            tm.get_members()
            tm.get_template()

        assert mail2.exists(), "读取路径不得清理成员邮箱"
        assert tm.is_team_member("win_02")

    def test_orphan_sweep_spares_active_windows(self, tmp_path, monkeypatch):
        """孤立目录清理必须同时满足『无 member 记录』且『窗口不活跃』。"""
        tm = self._fresh_tm(tmp_path, monkeypatch)
        tm.set_active_window_ids({"win_01"})
        tm.join_team(window_id="win_01", agent_name="build")

        # win_02 已建目录但 member 记录还没落库（join 中间态），且它是活跃窗口
        live_dir = tm._mailbox_dir(tm.DEFAULT_TEAM, "win_02")
        live_dir.mkdir(parents=True, exist_ok=True)
        # win_99 既无记录也不活跃 —— 真正的孤儿
        dead_dir = tm._mailbox_dir(tm.DEFAULT_TEAM, "win_99")
        dead_dir.mkdir(parents=True, exist_ok=True)

        tm.set_active_window_ids({"win_01", "win_02"})

        assert live_dir.exists(), "活跃窗口的邮箱目录不得被当作孤儿删除"
        assert not dead_dir.exists(), "既无记录又不活跃的目录应被清理"

    def test_stale_member_still_cleaned_when_active_known(self, tmp_path, monkeypatch):
        """正常场景不能被削弱：活跃集合明确时，失效成员仍要清理。"""
        tm = self._fresh_tm(tmp_path, monkeypatch)
        tm.set_active_window_ids({"win_01", "win_02"})
        tm.join_team(window_id="win_01", agent_name="build")
        tm.join_team(window_id="win_02", agent_name="review")

        tm.set_active_window_ids({"win_01"})

        assert tm.is_team_member("win_01")
        assert not tm.is_team_member("win_02")
        assert not tm._mailbox_dir(tm.DEFAULT_TEAM, "win_02").exists()


# ══════════════════════════════════════════════════════════
# 10. 回归：description 计数（修复 review 问题 3）
# ══════════════════════════════════════════════════════════


class TestDescriptionActiveCount:
    """修复说明：_handle_team_save 的 description 之前用 len(instances)（含已销毁窗口），

    现在改为「实际非已关闭窗口数」。本测试用 AST 静态校验源码保证修复不被回退。
    """

    def test_handle_team_save_uses_active_count_not_len_instances(self):
        """源码静态检查：_handle_team_save 不能再用 len(_instances) 写 description。"""
        import ast

        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))

        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_team_save":
                target_func = node
                break
        assert target_func is not None, "未找到 _handle_team_save 方法"

        # 序列化整个函数源码，检查关键短语
        import textwrap

        func_src = textwrap.dedent(ast.unparse(target_func))
        # 修复后应包含 active_count 变量
        assert "active_count" in func_src, "description 计数应使用 active_count 变量（实际非已关闭窗口数）"
        # 不应再直接用 len(instances) 拼 description
        bad_pattern = "len(instances)"
        assert bad_pattern not in func_src, f"description 不应再用 {bad_pattern}（包含已销毁窗口会偏大）"


# ══════════════════════════════════════════════════════════
# 11. 回归：--load 强制走 ConfirmDialog 确认（修复 review 问题 2）
# ══════════════════════════════════════════════════════════


class TestLoadConfirmationDialog:
    """修复说明：_handle_team_load 开头加了 ConfirmDialog 确认。

    源码静态检查：使用 ConfirmDialog + _confirmed 回调模式，用户取消时 return。
    """

    def test_handle_team_load_uses_confirm_dialog(self):
        import ast
        import textwrap

        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))

        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_team_load":
                target_func = node
                break
        assert target_func is not None, "未找到 _handle_team_load 方法"

        func_src = textwrap.dedent(ast.unparse(target_func))
        # 必须从 common_dialogs 导入 ConfirmDialog
        assert "from app.widgets.common_dialogs import ConfirmDialog" in func_src, (
            "_handle_team_load 应从 common_dialogs 导入 ConfirmDialog"
        )
        # 必须使用 _confirmed list 回调模式
        assert "_confirmed" in func_src, "_handle_team_load 应使用 _confirmed 回调变量"
        # 必须连接 confirmed 信号
        assert ".confirmed.connect(" in func_src, "_handle_team_load 应连接 confirmed 信号"
        # 用户取消时必须 return
        assert "if not _confirmed[0]:" in func_src or "if not _confirmed :" in func_src, (
            "_handle_team_load 应在用户取消时直接 return"
        )


# ══════════════════════════════════════════════════════════
# 12. 回归：300ms 提取为类常量（修复 review 问题 4）
# ══════════════════════════════════════════════════════════


class TestTemplateJoinDelayConstant:
    """修复说明：300ms magic number → 类常量 _TEMPLATE_JOIN_DELAY_MS。"""

    def test_template_join_delay_constant_defined(self):
        """OpenAIChatToolWindow 类必须有 _TEMPLATE_JOIN_DELAY_MS 类属性。"""
        import ast

        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "OpenAIChatToolWindow":
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        if stmt.target.id == "_TEMPLATE_JOIN_DELAY_MS":
                            found = True
                            # 应是整数类型注解
                            if stmt.annotation is not None:
                                assert ast.unparse(stmt.annotation) == "int"
                            # 值应为正整数
                            if isinstance(stmt.value, ast.Constant):
                                assert isinstance(stmt.value.value, int)
                                assert stmt.value.value > 0
                break
        assert found, "OpenAIChatToolWindow 缺少 _TEMPLATE_JOIN_DELAY_MS 类属性"

    def test_handle_team_load_uses_constant_not_magic_number(self):
        """创建链路应使用 self._TEMPLATE_JOIN_DELAY_MS，不再出现裸 300。

        T5 重构：延迟 join 迁至 _spawn_team_member_window（_handle_team_load
        委托 _spawn_team_members），常量引用随创建链路迁移。
        """
        import ast
        import textwrap

        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))

        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_spawn_team_member_window":
                target_func = node
                break
        assert target_func is not None, "缺少 _spawn_team_member_window 公共创建方法"

        func_src = textwrap.dedent(ast.unparse(target_func))
        # 必须引用类常量
        assert "self._TEMPLATE_JOIN_DELAY_MS" in func_src
        # 不应再出现裸 300（QTimer.singleShot 的第一参数）
        # 排除注释、字符串等：直接搜 "300," 或 "300\n" 这种数字字面量
        import re

        # 简单粗暴：函数体源码里不应有 "300," 这种数字字面量
        assert not re.search(r"\b300\b", func_src), (
            "_spawn_team_member_window 中应使用 self._TEMPLATE_JOIN_DELAY_MS 替代裸 300"
        )


# ══════════════════════════════════════════════════════════
# 13. 角色描述注入：inject_team_context hook（按成员各自注入）
# ══════════════════════════════════════════════════════════


class TestInjectTeamContext:
    """SessionStart hook 按成员注入模板描述 + 该成员自己的角色描述。"""

    def _load_hook(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "inject_team_context_test",
            Path(__file__).resolve().parent.parent.parent / "plugins" / "system" / "hooks" / "inject_team_context.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.hook

    def test_non_member_returns_empty(self, tmp_path):
        hook = self._load_hook()
        assert hook("SessionStart", {"is_team_member": False}) == ""

    def test_no_template_returns_empty(self, tmp_path, monkeypatch):
        from app.core import team_manager as tm_mod

        class _FakeTM:
            def get_template(self):
                return None

            def get_members(self):
                return []

        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: _FakeTM()))
        hook = self._load_hook()
        assert hook("SessionStart", {"is_team_member": True}) == ""

    def test_meaningless_description_not_injected(self, tmp_path, monkeypatch):
        """用户自建模板的自动生成描述（由 N 个活跃窗口保存...）不注入。"""
        from app.core import team_manager as tm_mod

        class _FakeTM:
            def get_template(self):
                return {"name": "t", "description": "由 3 个活跃窗口保存（去重 2 个角色）", "agents": []}

            def get_members(self):
                return []

        import app.core.team_manager as tm_module

        # 必须用 monkeypatch：直接赋值会永久污染 TeamManager，导致同一 pytest
        # 会话中后续用例（如 main_widget smoke）拿到 _FakeTM 而报 AttributeError。
        monkeypatch.setattr(tm_module.TeamManager, "get_instance", staticmethod(lambda: _FakeTM()))
        hook = self._load_hook()
        assert hook("SessionStart", {"is_team_member": True}) == ""

    def test_injects_template_desc_only_for_member_without_role_desc(self, tmp_path, monkeypatch):
        """成员无角色描述（旧模板）时只注入模板描述，不追加角色段落。"""
        from app.core import team_manager as tm_mod

        class _FakeTM:
            def get_template(self):
                return {
                    "name": "t",
                    "description": "经典团队",
                    "agents": [{"agent_name": "build", "description": ""}],
                }

            def get_members(self):
                return [{"window_id": "win_01", "agent_name": "build"}]

        import app.core.team_manager as tm_module

        monkeypatch.setattr(tm_module.TeamManager, "get_instance", staticmethod(lambda: _FakeTM()))
        hook = self._load_hook()
        out = hook("SessionStart", {"is_team_member": True, "window_id": "win_01"})
        assert "团队「t」协作上下文" in out
        assert "经典团队" in out
        assert "你的角色" not in out

    def test_injects_template_desc_plus_own_role_desc(self, tmp_path, monkeypatch):
        """成员应收到模板描述 + 自己角色的描述（按成员各自注入）。"""
        from app.core import team_manager as tm_mod

        class _FakeTM:
            def get_template(self):
                return {
                    "name": "t",
                    "description": "经典团队",
                    "agents": [
                        {"agent_name": "leader", "description": "统筹团队任务"},
                        {"agent_name": "build", "description": "负责编码实现"},
                    ],
                }

            def get_members(self):
                return [
                    {"window_id": "win_01", "agent_name": "leader"},
                    {"window_id": "win_02", "agent_name": "build"},
                ]

        import app.core.team_manager as tm_module

        monkeypatch.setattr(tm_module.TeamManager, "get_instance", staticmethod(lambda: _FakeTM()))
        hook = self._load_hook()

        # leader 窗口：只收到自己的角色描述
        out_leader = hook("SessionStart", {"is_team_member": True, "window_id": "win_01"})
        assert "团队「t」协作上下文" in out_leader
        assert "经典团队" in out_leader
        assert "你的角色「leader」：统筹团队任务" in out_leader
        assert "负责编码实现" not in out_leader, "不应注入其他成员的角色描述"

        # build 窗口：只收到 build 的角色描述
        out_build = hook("SessionStart", {"is_team_member": True, "window_id": "win_02"})
        assert "你的角色「build」：负责编码实现" in out_build
        assert "统筹团队任务" not in out_build, "不应注入其他成员的角色描述"

    def test_member_not_found_returns_empty(self, tmp_path, monkeypatch):
        """window_id 在成员列表中找不到时（不应发生）返回空，不崩溃。"""
        from app.core import team_manager as tm_mod

        class _FakeTM:
            def get_template(self):
                return {"name": "t", "description": "经典团队", "agents": []}

            def get_members(self):
                return [{"window_id": "win_01", "agent_name": "build"}]

        import app.core.team_manager as tm_module

        monkeypatch.setattr(tm_module.TeamManager, "get_instance", staticmethod(lambda: _FakeTM()))
        hook = self._load_hook()
        # 描述有实际内容 → 注入模板描述；找不到角色 → 无角色段落
        out = hook("SessionStart", {"is_team_member": True, "window_id": "win_99"})
        assert "经典团队" in out
        assert "你的角色" not in out

    def test_legacy_string_agents_supported(self, tmp_path):
        """旧模板 agents 为纯字符串列表时兼容：不崩溃、角色描述为空。"""
        from app.core import team_manager as tm_mod

        class _FakeTM:
            def get_template(self):
                return {
                    "name": "legacy",
                    "description": "旧格式团队",
                    "agents": ["leader", "build"],
                }

            def get_members(self):
                return [{"window_id": "win_01", "agent_name": "build"}]

        import app.core.team_manager as tm_module

        tm_module.TeamManager.get_instance = staticmethod(lambda: _FakeTM())
        hook = self._load_hook()
        out = hook("SessionStart", {"is_team_member": True, "window_id": "win_01"})
        assert "旧格式团队" in out
        assert "你的角色" not in out


# ══════════════════════════════════════════════════════════
# 14. team_list_members 角色描述显示
# ══════════════════════════════════════════════════════════


class TestTeamListMembersRoleDesc:
    """team_list_members 工具应显示成员的角色描述（来自模板上下文，无描述时兼容省略）。"""

    def _make_builtin_tools(self, window_id="win_01", agent_name="build"):
        """构造插件 impl 的 tool_ctx（含团队窗口上下文）"""
        return {"team_window_id": window_id, "team_agent_name": agent_name}

    def _make_tm(self, template=None, members=None):
        from app.core import team_manager as tm_mod

        class _FakeTM:
            def __init__(self, template, members):
                self._template = template
                self._members = members

            def get_template(self):
                return self._template

            def get_members(self):
                return self._members

            def get_member_busy_status(self, window_id):
                return "idle"

            def get_running_tasks(self, window_id):
                return []

            def get_pending_tasks(self, window_id):
                return []

        tm_mod.TeamManager.get_instance = staticmethod(lambda: _FakeTM(template, members))
        return tm_mod.TeamManager.get_instance()

    def test_members_with_role_desc_shown(self, tmp_path):
        """模板上下文含角色描述时，成员行下方显示角色描述。"""
        subagent = _load_subagent_tools()

        tm = self._make_tm(
            template={
                "name": "t",
                "description": "经典团队",
                "agents": [
                    {"agent_name": "leader", "description": "统筹团队任务"},
                    {"agent_name": "build", "description": "负责编码实现"},
                ],
            },
            members=[
                {"window_id": "win_01", "agent_name": "leader"},
                {"window_id": "win_02", "agent_name": "build"},
            ],
        )
        result = subagent._team_list_members(self._make_builtin_tools(window_id="win_01", agent_name="leader"))
        assert result.success
        content = result.content
        assert "leader@win_01" in content
        assert "统筹团队任务" in content
        assert "build@win_02" in content
        assert "负责编码实现" in content

    def test_members_without_role_desc_omitted(self, tmp_path):
        """无角色描述（手动加入成员/旧模板）时不显示角色行，兼容。"""
        subagent = _load_subagent_tools()

        tm = self._make_tm(
            template={
                "name": "t",
                "description": "旧团队",
                "agents": [{"agent_name": "build", "description": ""}],
            },
            members=[{"window_id": "win_01", "agent_name": "build"}],
        )
        result = subagent._team_list_members(self._make_builtin_tools())
        assert result.success
        content = result.content
        assert "build@win_01" in content
        assert "角色:" not in content

    def test_no_template_still_lists_members(self, tmp_path):
        """未加载模板（手动加入团队）时仍能列出成员，不显示角色描述。"""
        subagent = _load_subagent_tools()

        tm = self._make_tm(
            template=None,
            members=[
                {"window_id": "win_01", "agent_name": "build"},
                {"window_id": "win_02", "agent_name": "plan"},
            ],
        )
        result = subagent._team_list_members(self._make_builtin_tools())
        assert result.success
        assert "build@win_01" in result.content
        assert "plan@win_02" in result.content
        assert "角色:" not in result.content


# ══════════════════════════════════════════════════════════
# 15. /team --load 缺失角色降级为 prompt_sections 注入
# ══════════════════════════════════════════════════════════


class TestLoadMissingDegradation:
    """修复说明：原 `_handle_team_load` 缺失分支弹 InfoBar.error 中止，改为降级到

    `prompt_sections` 注入补全流程（极简方案：统一走 `CommandNeedDegrade` 异常）：
    1. `_handle_team_load` 缺失分支抛 `CommandNeedDegrade("team", ...)`（不再弹 InfoBar）
    2. `_execute_command` 捕获异常 → select_prompt 按参数匹配 `<!-- section:xxx -->` 段，
       写 `session.metadata["_pending_command"]`，置 `_team_load_degraded=True`
    3. `_on_send_clicked` FUNCTION 分支统一走 handler（`_execute_command`），
       之后检查 `_team_load_degraded` 决定是否继续 send_message
    4. 已删除 `_prompt_matched` 整段计算（不再硬编码 --load=/--create=）
    5. `plugins/system/commands/team.md` frontmatter 保持 `prompt_sections` 单字段
    """

    @staticmethod
    def _main_widget_src() -> tuple[Path, str]:
        src_path = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        return src_path, src_path.read_text(encoding="utf-8")

    @staticmethod
    def _team_md_path() -> Path:
        return Path(__file__).resolve().parent.parent.parent / "plugins" / "system" / "commands" / "team.md"

    @staticmethod
    def _find_function(tree: ast.Module, name: str):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def test_handle_team_load_missing_raises_command_need_degrade(self):
        """`_handle_team_load` 缺失分支必须抛 `CommandNeedDegrade`，不再弹 InfoBar。

        极简方案回归：缺失成员时不再调 `_degrade_team_load_to_prompt`（方法已删），
        改为抛 `CommandNeedDegrade("team", "--load=<name> 缺失角色: ...")`，
        由 `_execute_command` 统一捕获走 prompt 注入。
        """
        import ast as _ast
        import re as _re
        import textwrap as _tw

        _src_path, src = self._main_widget_src()
        _tree = _ast.parse(src)

        target = self._find_function(_tree, "_handle_team_load")
        assert target is not None, "未找到 _handle_team_load 方法"

        func_src = _tw.dedent(_ast.unparse(target))

        # 缺失分支必须抛 CommandNeedDegrade（含 command_name="team" + remainder 含 --load=）
        assert "CommandNeedDegrade" in func_src, "_handle_team_load 必须抛 CommandNeedDegrade 异常"
        assert _re.search(r"CommandNeedDegrade\s*\(\s*['\"]team['\"]\s*,\s*f?['\"]--load=", func_src), (
            "必须抛 CommandNeedDegrade('team', '--load=<name> 缺失角色: ...')"
        )
        assert "缺失角色" in func_src, "降级 remainder 必须包含缺失角色名单"

        # 不再依赖已删除的 _degrade_team_load_to_prompt 方法
        assert "_degrade_team_load_to_prompt" not in func_src, (
            "_handle_team_load 不应再调用已删除的 _degrade_team_load_to_prompt 方法"
        )

        # 不再弹原 InfoBar 报错
        assert "模板角色缺失" not in func_src, "_handle_team_load 不应再弹 InfoBar.error('模板角色缺失', ...) 报错文案"
        assert "加载中止" not in func_src, "_handle_team_load 不应再出现「加载中止」报错文案（已降级到 prompt 补全）"

    def test_command_need_degrade_exception_exists(self):
        """`app/core/command_manager.py` 必须定义 `CommandNeedDegrade` 异常类。"""
        import ast as _ast

        src_path = Path(__file__).resolve().parent.parent.parent / "app" / "core" / "command_manager.py"
        _tree = _ast.parse(src_path.read_text(encoding="utf-8"))

        found = False
        for node in _ast.walk(_tree):
            if isinstance(node, ast.ClassDef) and node.name == "CommandNeedDegrade":
                # 必须是 Exception 子类
                bases = [_ast.unparse(b) for b in node.bases]
                assert any("Exception" in b for b in bases), (
                    f"CommandNeedDegrade 必须是 Exception 子类，实际 bases={bases}"
                )
                # 构造器必须接收 command_name / remainder / degrade_section
                init = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
                assert init is not None, "CommandNeedDegrade 必须定义 __init__"
                init_src = _ast.unparse(init)
                assert "command_name" in init_src and "remainder" in init_src, (
                    "CommandNeedDegrade.__init__ 必须接收 command_name 和 remainder"
                )
                found = True
                break
        assert found, "command_manager.py 必须定义 CommandNeedDegrade 异常类"

    def test_handle_team_load_keeps_confirm_dialog_for_normal_path(self):
        """`_handle_team_load` 仍保留 ConfirmDialog 确认（不破坏既有回归）。"""
        import ast as _ast
        import textwrap as _tw

        _src_path, src = self._main_widget_src()
        _tree = _ast.parse(src)

        target = self._find_function(_tree, "_handle_team_load")
        assert target is not None, "未找到 _handle_team_load 方法"

        func_src = _tw.dedent(_ast.unparse(target))

        assert "from app.widgets.common_dialogs import ConfirmDialog" in func_src, (
            "应保留 ConfirmDialog import（正常加载路径不受影响）"
        )
        assert "_confirmed" in func_src, "应保留 _confirmed 回调变量"
        assert ".confirmed.connect(" in func_src, "应保留 confirmed 信号连接"
        assert "if not _confirmed[0]:" in func_src or "if not _confirmed :" in func_src, (
            "用户取消时仍应 return（不进入建窗口流程）"
        )

    def test_handle_team_load_resets_team_project_from_source_tab(self):
        """🐛 回归：/team --load 构建团队时，团队级项目必须重置为
        「执行加载的标签页」当前项目。

        背景：团队级项目（team.json 顶层 project）是持久化的，任一成员切项目
        即写入。若 _handle_team_load 不重置，新团队会沿用上次构建残留的旧项目，
        而不是继承执行 /team --load 那个标签页的项目（用户报告"没有变化"）。

        修复：start_team_run 之后、_spawn_team_members 之前，无条件
        set_team_project(源标签页 _current_project)。
        """
        import ast as _ast
        import re as _re
        import textwrap as _tw

        _src_path, src = self._main_widget_src()
        _tree = _ast.parse(src)

        target = self._find_function(_tree, "_handle_team_load")
        assert target is not None, "未找到 _handle_team_load 方法"

        func_src = _tw.dedent(_ast.unparse(target))

        # 必须调用 set_team_project 重置团队级项目
        assert "set_team_project" in func_src, "_handle_team_load 必须调用 set_team_project"
        # 顺序：start_team_run 之后、_spawn_team_members 之前
        _run_idx = func_src.find("start_team_run")
        _spawn_idx = func_src.find("_spawn_team_members")
        _set_idx = func_src.find("set_team_project")
        assert -1 not in (_run_idx, _spawn_idx, _set_idx), "缺少关键调用"
        assert _run_idx < _set_idx < _spawn_idx, (
            "set_team_project 必须在 start_team_run 之后、_spawn_team_members 之前"
        )
        # 数据源为源标签页当前项目（self._current_project）
        assert "_current_project" in func_src, "必须读取源标签页 _current_project"

    def test_execute_command_catches_degrade_and_injects_prompt(self):
        """`_execute_command` 必须捕获 `CommandNeedDegrade` → select_prompt + 写 _pending_command + 置标记。

        极简方案核心：业务/故障降级统一在 `_execute_command` 完成（捕获异常后
        select_prompt 取 prompt_sections 对应段、写 `_pending_command`、置
        `_team_load_degraded=True` 供 `_on_send_clicked` 继续 send_message）。
        """
        import ast as _ast
        import re as _re
        import textwrap as _tw

        _src_path, src = self._main_widget_src()
        _tree = _ast.parse(src)

        target = self._find_function(_tree, "_execute_command")
        assert target is not None, "未找到 _execute_command 方法"

        func_src = _tw.dedent(_ast.unparse(target))

        # 1) 必须捕获 CommandNeedDegrade 异常
        assert _re.search(r"except\s+CommandNeedDegrade", func_src), (
            "_execute_command 必须捕获 CommandNeedDegrade 异常（except 子句）"
        )
        assert "CommandNeedDegrade" in func_src, "_execute_command 必须引用 CommandNeedDegrade"

        # 2) 捕获后必须调 select_prompt（按 remainder 匹配 prompt_sections 段）
        assert _re.search(r"select_prompt\s*\(", func_src), (
            "_execute_command 降级分支必须调用 cmd_mgr.select_prompt 取提示词段"
        )

        # 3) 必须写 session.metadata['_pending_command']（供 inject_command_prompt hook 注入）
        assert "_pending_command" in func_src, "_execute_command 降级分支必须写入 session.metadata['_pending_command']"

        # 4) 必须置 _team_load_degraded = True（供 _on_send_clicked 识别降级）
        assert _re.search(r"_team_load_degraded\s*=\s*True", func_src), (
            "_execute_command 降级分支必须置 `self._team_load_degraded = True`"
        )

    def test_on_send_clicked_recognizes_degraded_flag(self):
        """`_on_send_clicked` FUNCTION 分支必须检查 `_team_load_degraded` 标记。"""
        import ast as _ast
        import re as _re
        import textwrap as _tw

        _src_path, src = self._main_widget_src()
        _tree = _ast.parse(src)

        target = self._find_function(_tree, "_on_send_clicked")
        assert target is not None, "未找到 _on_send_clicked 方法"

        func_src = _tw.dedent(_ast.unparse(target))

        # FUNCTION 分支必须检查 _team_load_degraded（降级时继续 send_message，否则 return）
        assert _re.search(r"_team_load_degraded", func_src), (
            "_on_send_clicked 必须识别 _team_load_degraded 标记（降级时继续走 send_message）"
        )

    def test_prompt_matched_removed_from_on_send_clicked(self):
        """`_on_send_clicked` 必须不再存在 `_prompt_matched` 计算（硬编码已彻底删除）。"""
        import ast as _ast
        import re as _re
        import textwrap as _tw

        _src_path, src = self._main_widget_src()
        _tree = _ast.parse(src)

        target = self._find_function(_tree, "_on_send_clicked")
        assert target is not None, "未找到 _on_send_clicked 方法"

        func_src = _tw.dedent(_ast.unparse(target))

        # 极简方案：不再有 _prompt_matched 变量 / any(...) 计算 / 排除分支
        assert "_prompt_matched" not in func_src, (
            "_on_send_clicked 不应再存在 _prompt_matched 计算（已由 CommandNeedDegrade 机制替代）"
        )
        # 不再有 `'--load=' in _active` 硬编码排除
        assert not _re.search(r"""['"]--load=['"]\s+in\s+_active""", func_src), (
            "_on_send_clicked 不应再存在 --load= 硬编码排除分支（已由异常机制替代）"
        )

    def test_execute_command_path_not_depend_on_prompt_matched(self):
        """`_execute_command` 路径不依赖 `_prompt_matched`（全链路不再硬编码）。"""
        import ast as _ast
        import textwrap as _tw

        _src_path, src = self._main_widget_src()
        _tree = _ast.parse(src)

        target = self._find_function(_tree, "_execute_command")
        assert target is not None, "未找到 _execute_command 方法"
        func_src = _tw.dedent(_ast.unparse(target))
        assert "_prompt_matched" not in func_src, "_execute_command 不应依赖 _prompt_matched"

    def test_team_md_load_missing_section_and_frontmatter(self):
        """`plugins/system/commands/team.md` 必须含 load_missing section + prompt_sections 映射 + 提示流程。

        极简方案：frontmatter 保持 `prompt_sections` 单字段（含 --create= 与 --load=），
        不拆分 prompt_degrade_sections。
        """
        team_md_path = self._team_md_path()
        team_md = team_md_path.read_text(encoding="utf-8")

        # frontmatter 必须含 --create=: "create" 与 --load=: "load_missing" 映射
        assert "--create=:" in team_md and '"create"' in team_md, (
            'frontmatter prompt_sections 必须含 `--create=: "create"` 映射'
        )
        assert "--load=:" in team_md and '"load_missing"' in team_md, (
            'frontmatter prompt_sections 必须含 `--load=: "load_missing"` 映射（不拆分字段）'
        )
        # 不应出现 prompt_degrade_sections 拆分字段
        assert "prompt_degrade_sections" not in team_md, "极简方案下 team.md 不应引入 prompt_degrade_sections 字段"

        # section 起始标记必须独占一行且唯一
        section_markers = re.findall(r"^<!-- section:load_missing -->$", team_md, re.MULTILINE)
        assert len(section_markers) == 1, (
            f"`<!-- section:load_missing -->` 起始标记必须独占一行且唯一，实际 {len(section_markers)} 次"
        )

        # section 自成一体：含 question 工具 + 骨架写入 + 重新 /team --load= 提示
        # 用整行作为 section_start，保证我们的索引对齐真正独立段的开始
        section_start = re.search(r"^<!-- section:load_missing -->$", team_md, re.MULTILINE).start()
        section_end = team_md.index("\n<!-- end -->", section_start)
        section_body = team_md[section_start:section_end]
        assert "question" in section_body, "load_missing section 应指导 AI 使用 question 工具逐项确认"
        assert "write" in section_body, "load_missing section 应包含 write 工具写入骨架指令"
        assert "/team --load=" in section_body, "load_missing section 应提示用户重新执行 `/team --load=<name>` 完成加载"
        assert "user-custom/agents/" in section_body, (
            "load_missing section 应说明写到 `~/.drifox/plugins/user-custom/agents/<role>.md`"
        )

        # 🆕 公共规范必须位于 section 之外（create 段之前），供两个 section 共享
        create_start = team_md.index("<!-- section:create -->")
        common_part = team_md[:create_start]
        for kw in ("子智能体创建规范", "权限推导规则", "骨架模板", "mkdir", "文件路径约定", "加载时机"):
            assert kw in common_part, f"「子智能体创建规范」公共区必须含 {kw}（位于 section 之外）"
        # 公共区不得包含 section 起始标记（避免 select_prompt 误解析）
        assert "<!-- section:" not in common_part.split("<!--")[0], "公共区不应出现 section 起始标记"

        # 公共规范不允许在 load_missing section 内重复内嵌（已提取到公共区）
        assert "### 1. 完整骨架模板" not in section_body, (
            "load_missing section 不应再内嵌完整骨架模板（已提取到公共区，重复会导致注入膨胀）"
        )
        assert "### 3. 权限推导规则" not in section_body, (
            "load_missing section 不应再内嵌权限推导规则（已提取到公共区，由公共区共享）"
        )

        # 🆕 select_prompt 过滤后公共规范始终保留（与命令报错回退兜底联动）
        from app.core.builtin_commands import _load_command_file
        from app.core.command_manager import CommandManager, CommandType

        _loaded = _load_command_file(team_md_path)
        _cm = CommandManager.get_instance()
        _cm.register(
            name="team",
            command_type=CommandType.FUNCTION,
            description="t",
            prompt_text=_loaded["prompt_text"],
            parameters=_loaded["parameters"],
            prompt_sections=_loaded["prompt_sections"],
        )
        for _marker, _sec in (
            ("--create=x", "任务：创建 DriFox 团队模板"),
            ("--load=x 缺失角色: a", "任务：补全 /team --load 缺失的子智能体"),
        ):
            _out = _cm.select_prompt("team", _marker) or ""
            assert "子智能体创建规范" in _out, f"select_prompt({_marker!r}) 必须保留公共规范"
            assert _sec in _out, f"select_prompt({_marker!r}) 必须包含对应 section"
        # 无匹配参数时（命令报错回退场景）至少返回公共规范，而非空字符串
        _fallback = _cm.select_prompt("team", "--unknown") or ""
        assert _fallback.strip(), "select_prompt 无匹配参数时必须返回公共规范（不能为空字符串）"
        assert "子智能体创建规范" in _fallback, "无匹配参数时回退内容必须含公共规范"


# ══════════════════════════════════════════════════════════
# 16. 历史会话恢复重新登记团队成员（leader 可见性修复）
# ══════════════════════════════════════════════════════════


class TestHistorySessionRestoreRegistersTeamMember:
    """修复说明：`_load_session_from_record` 恢复团队会话（team_run_id 非空）时
    原本只设置 UI 标记（_team_run_id/_team_name/_team_agent_name），未调用
    `TeamManager.join_team()` 重新登记成员 → 成员表缺失，leader 的
    `team_list_members` 查不到该窗口，发任务报"未找到目标"。

    修复：record_run_id 分支内补 3 步：join_team + _start_team_watcher +
    _sync_active_windows_to_team_manager（仅 window_id 与 agent_name 均非空时）。
    """

    @staticmethod
    def _main_widget_src() -> tuple[Path, str]:
        src_path = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        return src_path, src_path.read_text(encoding="utf-8")

    @staticmethod
    def _find_function(tree: ast.Module, name: str):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def test_team_branch_calls_join_watcher_sync(self):
        """AST：`_sync_team_markers_from_record` 团队分支必须 join_team + watcher + 活跃同步。

        回归防护：若有人删掉注册 3 步（例如改回仅 UI 标记），本测试立即失败。

        注：F4 逻辑已于 2026-08-04 提取为公共方法 _sync_team_markers_from_record
        （_load_session_from_record 与 _switch_to_session_by_id 共用），
        断言目标随之迁移。
        """
        import textwrap

        _src_path, src = self._main_widget_src()
        tree = ast.parse(src)

        # ① 公共方法必须存在且两个加载路径都调用它
        sync_fn = self._find_function(tree, "_sync_team_markers_from_record")
        assert sync_fn is not None, "未找到 _sync_team_markers_from_record 公共方法"
        load_fn = self._find_function(tree, "_load_session_from_record")
        switch_fn = self._find_function(tree, "_switch_to_session_by_id")
        assert load_fn is not None and switch_fn is not None
        load_src = ast.unparse(load_fn)
        switch_src = ast.unparse(switch_fn)
        assert "self._sync_team_markers_from_record(session_record)" in load_src, (
            "_load_session_from_record 必须调用 _sync_team_markers_from_record"
        )
        assert "self._sync_team_markers_from_record(session_record)" in switch_src, (
            "_switch_to_session_by_id 必须调用 _sync_team_markers_from_record"
        )

        func_src = textwrap.dedent(ast.unparse(sync_fn))

        # ② 保留原 3 行 UI 标记（不破坏既有语义）
        # 注：ast.unparse 固定输出单引号字符串字面量，正则按单引号匹配
        assert "self._team_run_id = record_run_id" in func_src, "团队分支必须保留 _team_run_id 赋值"
        assert re.search(
            r"self\._team_name = \(session_record\.get\('team_name'\) or ''\)\.strip\(\)",
            func_src,
        ), "团队分支必须保留 _team_name 赋值"
        assert re.search(
            r"self\._team_agent_name = \(session_record\.get\('agent_name'\) or ''\)\.strip\(\)",
            func_src,
        ), "团队分支必须保留 _team_agent_name 赋值"

        # ③ 注册三连必须同时出现（leader 可见性修复核心）
        assert re.search(
            r"join_team\s*\(\s*window_id\s*=\s*self\._window_id\s*,\s*agent_name\s*=\s*self\._team_agent_name\s*\)",
            func_src,
        ), "必须调用 tm.join_team(window_id=..., agent_name=...) 重新登记成员"
        assert re.search(r"self\._start_team_watcher\s*\(\s*\)", func_src), "必须启动邮箱 watcher"
        assert re.search(r"self\._sync_active_windows_to_team_manager\s*\(\s*\)", func_src), (
            "必须同步活跃窗口（防 _cleanup_stale_members 误清）"
        )

        # ④ join_team 必须出现在 agent_name 赋值之后（同在 record_run_id 团队分支内）
        agent_assign = re.search(
            r"self\._team_agent_name = \(session_record\.get\('agent_name'\) or ''\)\.strip\(\)",
            func_src,
        )
        assert agent_assign, "必须存在 _team_agent_name 赋值"
        after_marker = func_src[agent_assign.end() :]
        assert re.search(r"join_team", after_marker), "join_team 必须出现在 agent_name 赋值之后（团队分支内）"

        # ⑤ 注册受 window_id + agent_name 守卫（普通会话 / 无 agent 时不注册）
        assert re.search(r"if\s+self\._window_id\s+and\s+self\._team_agent_name", func_src), (
            "注册必须受 `if self._window_id and self._team_agent_name` 守卫"
        )

    @staticmethod
    def _make_window_stub():
        """构造轻量窗口实例（__new__ 绕过 __init__），仅提供 _load_session_from_record 依赖。"""
        from unittest.mock import MagicMock

        import app.main_widget as mw

        inst = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
        inst._is_streaming = False
        inst._auto_save_current_session = MagicMock()
        inst.backend = MagicMock()
        inst.history_manager = MagicMock()
        inst.history_manager.get_session_messages.return_value = [{"role": "user", "content": "hi"}]
        inst._project_label = MagicMock()
        inst._refresh_project_branch_style = MagicMock()
        inst.cfg = MagicMock()
        inst.cfg.enable_tab_manager.value = False
        inst._get_current_worktree_path = MagicMock(return_value="")
        inst._display_current_session = MagicMock()
        inst._release_inactive_session_messages = MagicMock()
        inst._history_card = None
        inst._start_team_watcher = MagicMock()
        inst._sync_active_windows_to_team_manager = MagicMock()
        return inst

    @staticmethod
    def _isolate_main_widget_deps(monkeypatch, tmp_path):
        """隔离 TeamManager 数据目录 + _load_session_from_record 的模块级依赖。"""
        from unittest.mock import MagicMock

        from app.core import team_manager as tm_mod

        # 🛡️ test_legacy_string_agents_supported 直接赋值污染 TeamManager.get_instance
        # （非 monkeypatch，同一 pytest 会话内永不恢复）。此处显式恢复真实单例逻辑，
        # 否则 _load_session_from_record 里 TeamManager.get_instance() 会拿到 _FakeTM
        # （无 is_team_member / join_team 方法）而 AttributeError。
        def _real_get_instance(cls):
            if cls._instance is None:
                with cls._lock:
                    if cls._instance is None:
                        cls._instance = cls()
            return cls._instance

        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", classmethod(_real_get_instance))
        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path))
        tm_mod.TeamManager._instance = None

        import app.main_widget as mw

        monkeypatch.setattr(mw, "_cleanup_global_lru_caches", lambda: None)
        monkeypatch.setattr(mw, "create_session_from_record", lambda *a, **k: MagicMock())
        monkeypatch.setattr(mw, "init_after_loading_session", lambda *a, **k: None)

        import app.core.command_manager as cm_mod
        import app.plugins.registries.ui_plugin_registry as uipr_mod

        monkeypatch.setattr(cm_mod.CommandManager, "get_instance", staticmethod(lambda: MagicMock()))
        monkeypatch.setattr(uipr_mod.UIPluginRegistry, "get_instance", staticmethod(lambda: MagicMock()))
        return mw, tm_mod

    def test_end_to_end_team_session_restore_registers_member(self, tmp_path, monkeypatch):
        """行为级：恢复团队会话 → 窗口被登记为成员（leader 可见）+ watcher/同步被调用。"""
        try:
            import app.main_widget as mw
        except Exception as _e:  # noqa: BLE001
            pytest.skip(f"main_widget 导入失败，跳过端到端: {_e!r}")

        mw, tm_mod = self._isolate_main_widget_deps(monkeypatch, tmp_path)
        inst = self._make_window_stub()
        inst._window_id = "win_restore_001"

        inst._load_session_from_record(
            {
                "session_id": "sess_restore_001",
                "title": "团队会话",
                "team_run_id": "team-run-001",
                "team_name": "default",
                "agent_name": "build",
                "project": "默认项目",
            }
        )

        tm = tm_mod.TeamManager.get_instance()
        assert tm.is_team_member("win_restore_001"), "恢复团队会话后窗口必须登记为团队成员（leader 可见）"
        assert inst._start_team_watcher.called, "必须启动邮箱 watcher"
        assert inst._sync_active_windows_to_team_manager.called, "必须同步活跃窗口"
        assert inst._team_run_id == "team-run-001", "UI 标记必须保留"
        assert inst._team_agent_name == "build", "UI 标记必须保留"

    def test_end_to_end_plain_session_not_registered(self, tmp_path, monkeypatch):
        """行为级：普通会话（无 team_run_id）加载不触发 join_team（语义不被破坏）。"""
        try:
            import app.main_widget as mw
        except Exception as _e:  # noqa: BLE001
            pytest.skip(f"main_widget 导入失败，跳过端到端: {_e!r}")

        mw, tm_mod = self._isolate_main_widget_deps(monkeypatch, tmp_path)
        inst = self._make_window_stub()
        inst._window_id = "win_plain_001"

        inst._load_session_from_record({"session_id": "sess_plain_001", "title": "普通会话"})

        tm = tm_mod.TeamManager.get_instance()
        assert not tm.is_team_member("win_plain_001"), "普通会话加载不得登记团队成员"
        assert not inst._start_team_watcher.called, "普通会话不得启动 watcher"
        assert not inst._sync_active_windows_to_team_manager.called, "普通会话不得同步活跃窗口"
        assert inst._team_run_id == "", "普通会话必须清空团队标记"

    def test_record_run_id_with_empty_agent_name_skips_register(self, tmp_path, monkeypatch):
        """行为级：team_run_id 非空但 agent_name 为空/纯空白 → 守卫跳过，不 join。

        review#13-#2 补充：agent_name 缺失时（异常数据）不得盲目登记，
        否则 leader 成员表会出现无 agent 的幽灵成员。
        """
        try:
            import app.main_widget as mw
        except Exception as _e:  # noqa: BLE001
            pytest.skip(f"main_widget 导入失败，跳过端到端: {_e!r}")

        mw, tm_mod = self._isolate_main_widget_deps(monkeypatch, tmp_path)
        inst = self._make_window_stub()
        inst._window_id = "win_empty_agent_001"

        inst._load_session_from_record(
            {
                "session_id": "sess_empty_agent_001",
                "title": "团队会话（无 agent）",
                "team_run_id": "team-run-002",
                "team_name": "default",
                "agent_name": "   ",  # 纯空白 → strip() 后为空，守卫拦截
                "project": "默认项目",
            }
        )

        tm = tm_mod.TeamManager.get_instance()
        assert not tm.is_team_member("win_empty_agent_001"), "agent_name 为空时不得登记团队成员"
        assert not inst._start_team_watcher.called, "agent_name 为空时不得启动 watcher"
        assert not inst._sync_active_windows_to_team_manager.called, "agent_name 为空时不得同步活跃窗口"

    def test_registered_member_with_different_agent_name_triggers_rejoin(self, tmp_path, monkeypatch):
        """行为级：窗口已注册为 build，恢复会话 agent_name 是 review → rejoin 覆盖（防漂移）。

        review#13-#1 核心场景：`is_team_member` 守卫下 agent_name 漂移（UI 显示 review
        而成员表仍是 build）。修复后必须触发 join_team 覆盖。
        """
        try:
            import app.main_widget as mw
        except Exception as _e:  # noqa: BLE001
            pytest.skip(f"main_widget 导入失败，跳过端到端: {_e!r}")

        mw, tm_mod = self._isolate_main_widget_deps(monkeypatch, tmp_path)
        tm = tm_mod.TeamManager.get_instance()

        # 预注册：窗口原本是 build
        tm.join_team(window_id="win_agent_001", agent_name="build")

        inst = self._make_window_stub()
        inst._window_id = "win_agent_001"
        inst._load_session_from_record(
            {
                "session_id": "sess_agent_001",
                "title": "团队会话",
                "team_run_id": "team-run-003",
                "team_name": "default",
                "agent_name": "review",  # 恢复的会话 agent 已变
                "project": "默认项目",
            }
        )

        # agent_name 已被覆盖为新值（leader team_list_members 不再漂移）
        members = {m["window_id"]: m for m in tm.get_members()}
        assert members["win_agent_001"]["agent_name"] == "review", "agent_name 不一致时必须 rejoin 覆盖"
        assert inst._start_team_watcher.called, "rejoin 后必须启动 watcher"
        assert inst._sync_active_windows_to_team_manager.called, "rejoin 后必须同步活跃窗口"

    def test_existing_window_member_skips_redundant_watcher_sync(self, tmp_path, monkeypatch):
        """行为级：已是成员且 agent_name 一致 → 跳过 join/watcher/同步（避免不必要写盘）。

        review#13-#2 补充：成员身份与 agent 均未变化时三连全跳过，
        保持「恢复会话」为轻量操作。
        """
        try:
            import app.main_widget as mw
        except Exception as _e:  # noqa: BLE001
            pytest.skip(f"main_widget 导入失败，跳过端到端: {_e!r}")

        mw, tm_mod = self._isolate_main_widget_deps(monkeypatch, tmp_path)
        tm = tm_mod.TeamManager.get_instance()

        # 预注册：window 已是 review（与将恢复的会话 agent 一致）
        tm.join_team(window_id="win_agent_002", agent_name="review")

        inst = self._make_window_stub()
        inst._window_id = "win_agent_002"
        inst._load_session_from_record(
            {
                "session_id": "sess_agent_002",
                "title": "团队会话",
                "team_run_id": "team-run-004",
                "team_name": "default",
                "agent_name": "review",
                "project": "默认项目",
            }
        )

        assert tm.is_team_member("win_agent_002"), "成员身份必须保留"
        members = {m["window_id"]: m for m in tm.get_members()}
        assert members["win_agent_002"]["agent_name"] == "review", "agent_name 不得被改写"
        assert not inst._start_team_watcher.called, "已注册且 agent 一致时不得重复启动 watcher"
        assert not inst._sync_active_windows_to_team_manager.called, "已注册且 agent 一致时不得重复同步"


# ══════════════════════════════════════════════════════════
# 15. 回归：团队邮件发件人角色（build 固化 bug）
# ══════════════════════════════════════════════════════════


class TestTeamMailSenderRoleGuard:
    """修复：团队窗口邮件发件人恒为 build（ChatEngine 400ms 延迟创建竞态）。

    线上症状：任务邮件第一行「📨 来自 [build@win_206]」不显示实际成员角色。
    根因链：
    1. _join_new_window_for_template（300ms 回调）→ _on_agent_changed 有
       `not self.backend.chat_engine` 守卫，而 ChatEngine 是 400ms 延迟创建，
       回调时必然未就绪 → 提前 return → switch_agent/set_team_context 未写入正确角色；
    2. _deferred_create_tool_executor 默认 agent = list_agents()[0]（恰为 build）；
    3. _load_agent_list 无按钮组分支用默认 _current_agent（"build"）二次覆盖。
    修复：① join 轮询补查 chat_engine；② _load_agent_list 团队窗口优先取
    _team_agent_name（AST 静态校验防回退，与文件既有风格一致）。
    """

    def _read_main_widget_src(self) -> str:
        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        return src.read_text(encoding="utf-8")

    def test_join_poll_waits_chat_engine(self):
        """_join_new_window_for_template 的 C4 轮询条件必须包含 chat_engine 检查。"""
        import ast

        tree = ast.parse(self._read_main_widget_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_join_new_window_for_template":
                target = node
                break
        assert target is not None, "缺少 _join_new_window_for_template"
        src = ast.unparse(target)
        # 就绪条件必须同时覆盖 agent_manager 与 chat_engine（缺一即重试）
        assert "win.backend.agent_manager" in src
        assert "win.backend.chat_engine" in src
        # 重试分支必须保留（未就绪不直接放弃）
        assert "_TEAM_JOIN_MAX_RETRIES" in src

    def test_load_agent_list_prefers_team_agent(self):
        """_load_agent_list 无按钮组分支：团队窗口优先用 _team_agent_name，禁止回退 build。"""
        import ast

        tree = ast.parse(self._read_main_widget_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_load_agent_list":
                target = node
                break
        assert target is not None, "缺少 _load_agent_list"
        src = ast.unparse(target)
        # 团队角色必须是 _current_agent 的优先来源
        assert "_team_agent_name" in src
        # 仍保留 build 兜底（非团队窗口），但只能作为最后回退
        assert '"build"' in src or "'build'" in src

    def test_on_agent_changed_guard_kept(self):
        """_on_agent_changed 的 chat_engine 守卫必须保留（错误 agent 不得提前写入状态）。"""
        import ast

        tree = ast.parse(self._read_main_widget_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_on_agent_changed":
                target = node
                break
        assert target is not None, "缺少 _on_agent_changed"
        src = ast.unparse(target)
        assert "chat_engine" in src
        assert "switch_agent" in src

