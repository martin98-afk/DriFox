# -*- coding: utf-8 -*-
"""project-dashboard 数据采集层测试（临时 git 仓库）"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "project-dashboard" / "ui"))
from dashboard import collect_data, find_git_root  # noqa: E402


@pytest.fixture
def git_repo(tmp_path):
    """建一个含 2 个 commit、2 个 .py 文件、1 个 .md 文件的临时 git 仓库"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)
    (repo / "a.py").write_text("x = 1\n" * 10, encoding="utf-8")
    (repo / "b.py").write_text("y = 2\n" * 5, encoding="utf-8")
    (repo / "readme.md").write_text("# hi", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "first"], check=True)
    (repo / "c.py").write_text("z = 3\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "second"], check=True)
    return repo


def test_find_git_root(git_repo):
    sub = git_repo / "sub" / "dir"
    sub.mkdir(parents=True)
    assert os.path.normpath(find_git_root(str(sub))) == os.path.normpath(str(git_repo))


def test_collect_data_basic(git_repo):
    data = collect_data(str(git_repo))
    assert data["error"] is None
    assert data["total_commits"] == 2
    assert data["branch"]  # 非空（master/main）
    assert sum(c for _, c in data["daily_commits"]) == 2
    assert sum(c for _, c in data["contributors"]) == 2
    # 语言统计：py 文件 3 个、md 1 个
    by_name = {name: (files, lines) for name, files, lines in data["languages"]}
    assert by_name["Python"][0] == 3
    assert by_name["Markdown"][0] == 1
    # 文件类型统计
    exts = dict(data["file_types"])
    assert exts[".py"] == 3
    assert exts[".md"] == 1


def test_collect_data_non_git(tmp_path):
    data = collect_data(str(tmp_path))
    assert data["error"] is not None
