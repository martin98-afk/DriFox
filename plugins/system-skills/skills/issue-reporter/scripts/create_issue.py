# -*- coding: utf-8 -*-
"""create_issue.py — 提交 DriFox GitHub Issue（API 直提 + 无 token 浏览器兜底）

优先用 GITHUB_TOKEN 环境变量走 GitHub API 创建 issue；
无 token 或 API 失败时，自动打开浏览器到 GitHub new-issue 预填页兜底。

用法：
    py -3 create_issue.py --title "标题" --body-file body.md [--repo owner/repo]
    py -3 create_issue.py --title "标题" --body "正文" [--repo owner/repo]

- --repo 省略时从当前目录 git remote origin 解析（支持 ssh/https 形式）。
- 成功打印 issue URL（退出码 0）；兜底时打印 BROWSER_FALLBACK（退出码 2）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_GIT_URL_RE = re.compile(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$")
_API_BASE = "https://api.github.com"


def resolve_repo(explicit: str | None) -> tuple[str, str]:
    """解析 owner/repo：显式参数优先，否则读 git remote origin"""
    if explicit:
        m = _GIT_URL_RE.search(explicit)
        if not m:
            raise ValueError(f"--repo 需为 GitHub URL 或 owner/repo 形式，收到: {explicit}")
        return m.group(1), m.group(2)
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    m = _GIT_URL_RE.search(url)
    if not m:
        raise ValueError(f"git remote origin 不是 GitHub 仓库: {url}")
    return m.group(1), m.group(2)


def read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    if args.body:
        return args.body
    raise ValueError("--body-file 与 --body 至少提供一个")


def create_via_api(owner: str, repo: str, title: str, body: str) -> tuple[bool, str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return False, "未设置 GITHUB_TOKEN 环境变量"
    payload = json.dumps({"title": title, "body": body}).encode()
    req = urllib.request.Request(
        f"{_API_BASE}/repos/{owner}/{repo}/issues",
        data=payload,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return True, data.get("html_url", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:500]
        return False, f"GitHub API {e.code}: {detail}"
    except Exception as e:
        return False, f"网络异常: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="提交 DriFox GitHub Issue")
    parser.add_argument("--title", required=True, help="issue 标题")
    parser.add_argument("--body", default="", help="issue 正文（与 --body-file 二选一）")
    parser.add_argument("--body-file", default="", help="issue 正文文件路径（UTF-8）")
    parser.add_argument("--repo", default="", help="owner/repo（省略则从 git remote 解析）")
    parser.add_argument("--no-browser", action="store_true", help="无 token 时不打开浏览器，仅打印预填链接")
    args = parser.parse_args()

    try:
        owner, repo = resolve_repo(args.repo or None)
    except Exception as e:
        print(f"[Error] 解析仓库失败: {e}")
        return 1

    try:
        body = read_body(args)
    except Exception as e:
        print(f"[Error] 读取正文失败: {e}")
        return 1

    ok, result = create_via_api(owner, repo, args.title, body)
    if ok:
        print(f"✓ Issue 创建成功: {result}")
        return 0

    print(f"[Warn] API 提交失败（{result}），降级浏览器预填页")
    prefill = (
        f"https://github.com/{owner}/{repo}/issues/new"
        f"?title={urllib.parse.quote(args.title)}&body={urllib.parse.quote(body)}"
    )
    if len(prefill) > 6000:
        prefill = f"https://github.com/{owner}/{repo}/issues/new?title={urllib.parse.quote(args.title)}"
    print(f"BROWSER_FALLBACK: {prefill}")
    if not args.no_browser:
        import webbrowser

        webbrowser.open(prefill)
        print("已打开浏览器预填页，请检查内容后点 Submit。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
