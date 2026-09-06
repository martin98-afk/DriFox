# -*- coding: utf-8 -*-
"""P1-4：pip spec 白名单校验（T8.1 行 10 口径）。

六用例：--extra-index-url 注入拒 / git+ 拒 / file:// 拒 / -r 拒 /
合法 requests>=2.31.0 过 / extras 合法形态过。
"""
from app.plugins.deps_loader import resolve_pip_deps


def _resolve(specs):
    return resolve_pip_deps({"dependencies": {"pip": {"default": specs}}})


def test_extra_index_url_injection_rejected():
    """--extra-index-url（index 注入/包投毒）拒收。"""
    specs = ["--extra-index-url https://evil.example.com/simple", "requests>=2.31.0"]
    merged = _resolve(specs)
    assert merged == ["requests>=2.31.0"]


def test_git_plus_rejected():
    """git+ 直装（绕过 PyPI 审计面）拒收。"""
    merged = _resolve(["git+https://github.com/evil/pkg.git", "requests>=2.31.0"])
    assert merged == ["requests>=2.31.0"]


def test_file_url_rejected():
    """file:// 本地路径拒收。"""
    merged = _resolve(["file:///C:/evil/pkg-0.1-py3-none-any.whl"])
    assert merged == []


def test_dash_r_rejected():
    """-r requirements.txt（间接文件注入）拒收。"""
    merged = _resolve(["-r", "requirements.txt", "-e", "."])
    assert merged == []


def test_valid_version_spec_passes():
    """合法：包名+版本约束 → 放行；裸包名（无版本约束）同样合法放行。"""
    assert _resolve(["requests>=2.31.0"]) == ["requests>=2.31.0"]
    assert _resolve(["pywin32==306", "httpx~=0.27", "orjson!=3.9.0"]) == [
        "pywin32==306", "httpx~=0.27", "orjson!=3.9.0",
    ]
    assert _resolve(["httpx", "pywin32"]) == ["httpx", "pywin32"]  # 存量裸包名形态


def test_extras_form_passes():
    """extras 合法形态（包名[extras]+版本约束）放行。"""
    assert _resolve(["requests[socks]>=2.31.0"]) == ["requests[socks]>=2.31.0"]
    assert _resolve(["pydantic[email]>=2.5"]) == ["pydantic[email]>=2.5"]
