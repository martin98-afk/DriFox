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
    """watched dir 内新增 SKILL.md：TTL 窗口内不自动出现（[E7-I] 折衷），
    invalidate_skills_cache() 后立即可见（插件变更钩子同款路径）。"""
    from app.utils.utils import get_local_skills, invalidate_skills_cache

    tmp, _ = contrib_env
    s1 = get_local_skills(project_dir=str(tmp))
    assert all(s["name"] != "new-skill" for s in s1)
    d = tmp / ".drifox" / "skills" / "new-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: new-skill\ndescription: n\n---\nx", encoding="utf-8")
    # 30s TTL 窗口内：动态段缓存命中，不重算 → 新技能暂不可见（用户已接受折衷）
    s2 = get_local_skills(project_dir=str(tmp))
    assert all(s["name"] != "new-skill" for s in s2), "TTL 窗口内不应重算动态段"
    # invalidate 钩子清动态段缓存 → 立即可见
    invalidate_skills_cache()
    s3 = get_local_skills(project_dir=str(tmp))
    assert any(s["name"] == "new-skill" for s in s3)

def test_contrib_key_ttl_cache_hit(contrib_env, monkeypatch):
    """[E7-I] 动态段 TTL 命中：同 project_dir 二次计算不再现算 iterdir+stat。"""
    import time as time_mod

    from app.utils import utils as u

    tmp, _ = contrib_env
    calls = {"n": 0}
    real = u._scan_contrib_parts

    def counting(pd):
        calls["n"] += 1
        return real(pd)

    monkeypatch.setattr(u, "_scan_contrib_parts", counting)
    k1 = u._compute_skills_cache_key(str(tmp))
    k2 = u._compute_skills_cache_key(str(tmp))
    assert calls["n"] == 1, f"TTL 窗口内应命中缓存，实测现算 {calls['n']} 次"
    assert k1 == k2

    # 过期（把缓存时刻拨回 31s 前）→ 重算
    u._contrib_key_cached_at = time_mod.monotonic() - 31.0
    u._compute_skills_cache_key(str(tmp))
    assert calls["n"] == 2, "TTL 过期后应重算动态段"

def test_invalidate_clears_contrib_cache(contrib_env, monkeypatch):
    """[E7-I] invalidate_skills_cache 清动态段缓存：失效后强制现算。"""
    from app.utils import utils as u

    tmp, _ = contrib_env
    calls = {"n": 0}
    real = u._scan_contrib_parts

    def counting(pd):
        calls["n"] += 1
        return real(pd)

    monkeypatch.setattr(u, "_scan_contrib_parts", counting)
    u._compute_skills_cache_key(str(tmp))
    assert calls["n"] == 1
    u._compute_skills_cache_key(str(tmp))
    assert calls["n"] == 1, "窗口内应命中"
    u.invalidate_skills_cache()
    u._compute_skills_cache_key(str(tmp))
    assert calls["n"] == 2, "invalidate 后应强制现算"

def test_contrib_cache_recomputes_for_other_project(contrib_env):
    """[E7-I] project_dir 变化 → 单槽键不匹配 → 重算（切项目不串味）。"""
    from app.utils import utils as u

    tmp, _ = contrib_env
    other = Path(tempfile.mkdtemp(prefix="skills_other2_"))
    k1 = u._compute_skills_cache_key(str(tmp))
    k2 = u._compute_skills_cache_key(str(other))
    assert u._contrib_key_cache is not None
    assert u._contrib_key_cache[0] == str(other)
    assert k1[0] == str(tmp) and k2[0] == str(other)
