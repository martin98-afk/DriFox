# -*- coding: utf-8 -*-
"""动态技能源：get_local_skills 合并贡献条目、缓存按 project_dir 区分、auto_enable 字段

fixture 的 provider 模拟真实语义：按传入 workdir 扫描 <wd>/.drifox/skills/，
只对注册时绑定的项目目录返回条目（与 Task 8 项目技能源行为一致）。
"""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def contrib_env():
    """注册一个基于临时目录的技能源，返回 (root, registry)"""
    from app.plugins.registries.contribution_registry import ContributionRegistry
    from app.utils import utils as u

    tmp = Path(tempfile.mkdtemp(prefix="skills_contrib_"))
    skills_root = tmp / ".drifox" / "skills"
    demo = skills_root / "demo-skill"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text("---\nname: demo-skill\ndescription: 测试项目技能\n---\n\n正文", encoding="utf-8")

    def _provider(wd):
        root = Path(wd) / ".drifox" / "skills"
        if Path(wd) != tmp or not root.is_dir():
            return []
        out = []
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            md = d / "SKILL.md"
            if not md.exists():
                md = d / "skill.md"
            if not md.exists():
                continue
            out.append(
                {
                    "name": d.name,
                    "qualified_name": d.name,
                    "description": f"{d.name} 描述",
                    "path": str(d.resolve()),
                    "source": "project",
                    "auto_enable": True,
                }
            )
        return out

    def _watched(wd):
        return [Path(wd) / ".drifox" / "skills"] if Path(wd) == tmp else []

    reg = ContributionRegistry.get_instance()
    reg.register_skill_source("test_plugin", "tmp_src", provider=_provider, watched_dirs=_watched)
    u.invalidate_skills_cache()
    yield tmp, reg
    reg.unregister_source("test_plugin")
    u.invalidate_skills_cache()


def test_merge_contrib_skills(contrib_env):
    from app.utils.utils import get_local_skills

    tmp, _ = contrib_env
    skills = get_local_skills(project_dir=str(tmp))
    demo = [s for s in skills if s["name"] == "demo-skill"]
    assert len(demo) == 1
    assert demo[0]["source"] == "project"
    assert demo[0]["auto_enable"] is True
    assert demo[0]["path"]


def test_no_project_dir_no_contrib(contrib_env):
    from app.utils.utils import get_local_skills

    tmp, _ = contrib_env
    skills = get_local_skills()  # 不传 project_dir → 不含动态条目
    assert all(s["name"] != "demo-skill" for s in skills)


def test_other_project_dir_no_contrib(contrib_env):
    from app.utils.utils import get_local_skills

    tmp, _ = contrib_env
    other = Path(tempfile.mkdtemp(prefix="skills_other_"))
    skills = get_local_skills(project_dir=str(other))
    assert all(s["name"] != "demo-skill" for s in skills)


def test_get_skill_by_name_and_load_skill(contrib_env):
    from app.utils.utils import get_skill_by_name, load_skill

    tmp, _ = contrib_env
    info = get_skill_by_name("demo-skill", project_dir=str(tmp))
    assert info and info["source"] == "project"
    ok, content, workspace = load_skill("demo-skill", project_dir=str(tmp))
    assert ok
    assert "正文" in content  # frontmatter 已剥离
    assert workspace == str((tmp / ".drifox" / "skills" / "demo-skill").resolve())


def test_skill_dir_content_change_invalidates(contrib_env):
    """watched dir 内新增 SKILL.md → 缓存 key 变化 → 新条目出现"""
    from app.utils.utils import get_local_skills

    tmp, _ = contrib_env
    s1 = get_local_skills(project_dir=str(tmp))
    assert all(s["name"] != "new-skill" for s in s1)
    d = tmp / ".drifox" / "skills" / "new-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: new-skill\ndescription: n\n---\nx", encoding="utf-8")
    s2 = get_local_skills(project_dir=str(tmp))
    assert any(s["name"] == "new-skill" for s in s2)
