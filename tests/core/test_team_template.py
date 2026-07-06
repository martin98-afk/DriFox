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

from pathlib import Path

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
# 11. 回归：--load 强制走 QMessageBox 确认（修复 review 问题 2）
# ══════════════════════════════════════════════════════════


class TestLoadConfirmationDialog:
    """修复说明：_handle_team_load 开头加了 QMessageBox.question 确认。

    源码静态检查：函数体前 30 行必须出现 QMessageBox.question 调用，且用户选 No 时 return。
    """

    def test_handle_team_load_starts_with_qmessagebox(self):
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
        # 必须包含 QMessageBox.question 调用
        assert "QMessageBox.question" in func_src, "_handle_team_load 缺少 QMessageBox.question 确认弹窗"
        # 必须在用户选 No 时 return
        assert "!= QMessageBox.Yes" in func_src, "_handle_team_load 应在用户选 No 时直接 return"
        # 默认按钮应是 No（防止误触）
        assert "QMessageBox.No" in func_src, "QMessageBox.question 的 default button 应为 No"


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
        """_handle_team_load 应使用 self._TEMPLATE_JOIN_DELAY_MS，不再出现裸 300。"""
        import ast
        import textwrap

        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))

        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_team_load":
                target_func = node
                break
        assert target_func is not None

        func_src = textwrap.dedent(ast.unparse(target_func))
        # 必须引用类常量
        assert "self._TEMPLATE_JOIN_DELAY_MS" in func_src
        # 不应再出现裸 300（QTimer.singleShot 的第一参数）
        # 排除注释、字符串等：直接搜 "300," 或 "300\n" 这种数字字面量
        import re

        # 简单粗暴：函数体源码里不应有 "300," 这种数字字面量
        assert not re.search(r"\b300\b", func_src), "_handle_team_load 中应使用 self._TEMPLATE_JOIN_DELAY_MS 替代裸 300"
