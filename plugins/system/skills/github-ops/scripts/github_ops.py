"""
GitHub Operations Script
GitHub API + Local Git 操作工具集

Usage:
    py -3 github_ops.py <action> [options]

Actions:
    # 本地 Git 操作
    status        [path]                               查看工作区状态
    diff          [path]                               查看文件差异
    diff-staged   [path]                               查看暂存区差异
    commit        <path> <message>                      提交文件
    discard       <path>                               撤销文件修改
    branch-log    <from_branch> <to_branch>            查看分支差异

    # GitHub Issue 操作
    get-issue     <owner> <repo> <issue_num>            查看 Issue 详情
    get-comments  <owner> <repo> <issue_num>            查看 Issue 评论
    reply-issue   <owner> <repo> <issue_num> <body>    回复 Issue
    create-issue  <owner> <repo> <title> <body> [labels] 创建 Issue（支持文件路径或直接文本）

    # GitHub PR 操作
    create-pr     <owner> <repo> <title> <body> <head> <base>  创建 PR（支持文件路径或直接文本）

    # 仓库信息
    get-repo      <owner> <repo>                        查看仓库信息
"""

import subprocess
import requests
import sys
import os
import re

# ============================================================
# GitHub API 配置
# ============================================================

def get_github_token() -> str:
    """从环境变量或命令行参数获取 GitHub Token"""
    # 优先从环境变量获取
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        return token
    
    raise ValueError(
        "GitHub Token not found. Please set environment variable:\n"
        "  set GITHUB_TOKEN=your_token_here  (Windows)\n"
        "Or use parameter:\n"
        "  $env:GITHUB_TOKEN='your_token'; python script.py ..."
    )

# 延迟获取 token，在需要时再获取
def get_headers():
    return {
        'Authorization': f'token {get_github_token()}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json'
    }

BASE_URL = 'https://api.github.com'

# 初始化 headers (延迟获取 token)
def _init_headers():
    global HEADERS
    HEADERS = get_headers()
    return HEADERS

HEADERS = _init_headers()


# ============================================================
# 本地 Git 操作
# ============================================================

def run_git(cmd: str, cwd: str = None) -> tuple:
    """执行 git 命令，返回 (returncode, stdout, stderr)"""
    try:
        import shlex
        args = shlex.split(cmd)
        result = subprocess.run(
            args, shell=False, cwd=cwd,
            capture_output=True, text=True, encoding='utf-8'
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return 1, '', str(e)


def status(path: str = '.') -> bool:
    """查看工作区状态"""
    returncode, stdout, stderr = run_git('git status --porcelain', cwd=path)
    
    if returncode != 0:
        print(f"[Error] Not a git repository or git not found")
        return False
    
    lines = stdout.strip().split('\n') if stdout.strip() else []
    
    if not lines or all(l == '' for l in lines):
        print("Working tree clean ✓")
        return True
    
    print("=== Working Tree Status ===")
    for line in lines:
        if not line:
            continue
        # 解析状态：XY path
        if len(line) >= 3:
            index_status = line[0]
            worktree_status = line[1]
            filepath = line[3:].strip()
            
            # 状态标签
            status_map = {
                'M': '修改', 'A': '新增', 'D': '删除',
                'R': '重命名', 'C': '复制', '??': '未跟踪'
            }
            idx_label = status_map.get(index_status, index_status)
            wts_label = status_map.get(worktree_status, worktree_status)
            
            if worktree_status == '?':
                print(f"  ?? {filepath}  (未跟踪)")
            else:
                print(f"  {idx_label}/{wts_label} {filepath}")
    
    return True


def show_diff(path: str = '.', staged: bool = False) -> bool:
    """查看文件差异"""
    cmd = 'git diff --staged' if staged else 'git diff'
    returncode, stdout, stderr = run_git(cmd, cwd=path)
    
    if returncode != 0:
        print(f"[Error] {stderr or 'Failed to get diff'}")
        return False
    
    if not stdout.strip():
        msg = "Staged changes clean ✓" if staged else "Working tree clean ✓"
        print(msg)
        return True
    
    if staged:
        print("=== Staged Changes ===")
    else:
        print("=== Working Tree Changes ===")
    
    print(stdout)
    return True


def commit(path: str, message: str, files: str = None) -> bool:
    """提交文件变更"""
    # 先暂存文件
    if files:
        returncode, _, stderr = run_git(f'git add {files}', cwd=path)
        if returncode != 0:
            print(f"[Error] Failed to stage files: {stderr}")
            return False
    else:
        returncode, _, stderr = run_git('git add .', cwd=path)
        if returncode != 0:
            print(f"[Error] Failed to stage files: {stderr}")
            return False
    
    # 执行提交
    returncode, stdout, stderr = run_git(f'git commit -m "{message}"', cwd=path)
    
    if returncode != 0:
        print(f"[Error] Commit failed: {stderr}")
        return False
    
    # 解析 commit hash
    commit_match = re.search(r'\[([^\s]+)\s+([0-9a-f]+)\]', stdout)
    if commit_match:
        branch = commit_match.group(1)
        commit_hash = commit_match.group(2)[:8]
        print(f"✓ Committed to {branch} ({commit_hash})")
        print(f"  Message: {message}")
    else:
        print(f"✓ Commit successful")
        print(f"  {stdout.strip()}")
    
    return True


def discard(path: str) -> bool:
    """撤销文件修改"""
    returncode, _, stderr = run_git(f'git checkout -- "{path}"', cwd=os.path.dirname(path) or '.')
    
    if returncode != 0:
        # 可能是未跟踪文件，尝试删除
        full_path = os.path.join(os.path.dirname(path) or '.', os.path.basename(path))
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
                print(f"✓ Deleted untracked file: {path}")
                return True
            except Exception as e:
                print(f"[Error] Failed to discard: {e}")
                return False
        print(f"[Error] Discard failed: {stderr}")
        return False
    
    print(f"✓ Discarded changes: {path}")
    return True


def branch_log(from_branch: str, to_branch: str, path: str = '.') -> bool:
    """查看两个分支之间的差异"""
    returncode, stdout, stderr = run_git(
        f'git log {to_branch}..{from_branch} --oneline', cwd=path
    )
    
    if returncode != 0:
        print(f"[Error] {stderr or 'Failed to get branch log'}")
        return False
    
    commits = stdout.strip().split('\n') if stdout.strip() else []
    
    print(f"=== {from_branch} → {to_branch} ({len(commits)} commits) ===")
    
    if not commits:
        print(f"{from_branch} is up to date with {to_branch}")
        return True
    
    for commit in commits:
        print(f"  {commit}")
    
    return True


def push(branch: str, path: str = '.', upstream: bool = False) -> bool:
    """推送分支到远程"""
    cmd = f'git push {"-u " if upstream else ""}origin {branch}'
    returncode, stdout, stderr = run_git(cmd, cwd=path)
    
    if returncode != 0:
        print(f"[Error] Push failed: {stderr}")
        return False
    
    print(f"✓ Pushed {branch} to origin")
    if stdout.strip():
        print(f"  {stdout.strip()}")
    return True


def get_issue(owner: str, repo: str, issue_num: str) -> bool:
    """获取 Issue 详情"""
    url = f'{BASE_URL}/repos/{owner}/{repo}/issues/{issue_num}'
    resp = requests.get(url, headers=HEADERS)
    
    if resp.status_code == 200:
        issue = resp.json()
        print(f"=== Issue #{issue_num} ===")
        print(f"Title: {issue['title']}")
        print(f"State: {issue['state']}")
        print(f"Author: {issue['user']['login']}")
        print(f"Labels: {[l['name'] for l in issue.get('labels', [])]}")
        print(f"Comments: {issue['comments']}")
        print(f"URL: {issue['html_url']}")
        print(f"\n--- Body ---")
        print(issue.get('body', '(empty)')[:1000])
        return True
    else:
        print(f"[Error] {resp.status_code}: {resp.text}")
        return False


def create_issue(owner: str, repo: str, title: str, body: str, labels: list = None) -> bool:
    """创建 Issue"""
    # 支持文件路径或直接文本
    if os.path.exists(title):
        with open(title, 'r', encoding='utf-8') as f:
            title = f.read().strip()
    
    if os.path.exists(body):
        with open(body, 'r', encoding='utf-8') as f:
            body = f.read()
    
    url = f'{BASE_URL}/repos/{owner}/{repo}/issues'
    data = {
        'title': title,
        'body': body,
    }
    if labels:
        data['labels'] = labels
    
    resp = requests.post(url, headers=HEADERS, json=data)
    
    if resp.status_code == 201:
        issue = resp.json()
        print(f"✓ Issue created successfully")
        print(f"Number: #{issue['number']}")
        print(f"URL: {issue['html_url']}")
        return True
    else:
        print(f"[Error] {resp.status_code}: {resp.text}")
        return False


def get_comments(owner: str, repo: str, issue_num: str) -> bool:
    """获取 Issue 评论列表"""
    url = f'{BASE_URL}/repos/{owner}/{repo}/issues/{issue_num}/comments'
    resp = requests.get(url, headers=HEADERS)
    
    if resp.status_code == 200:
        comments = resp.json()
        print(f"=== Comments on Issue #{issue_num} ({len(comments)} total) ===\n")
        
        for i, c in enumerate(comments, 1):
            print(f"[{i}] {c['user']['login']} - {c['created_at']}")
            print(c['body'][:500])
            if len(c['body']) > 500:
                print('... (truncated)')
            print()
        return True
    else:
        print(f"[Error] {resp.status_code}: {resp.text}")
        return False


def reply_issue(owner: str, repo: str, issue_num: str, body_file: str) -> bool:
    """回复 Issue"""
    if not os.path.exists(body_file):
        print(f"[Error] Body file not found: {body_file}")
        return False
    
    with open(body_file, 'r', encoding='utf-8') as f:
        body = f.read().strip()
    
    if not body:
        print("[Error] Body is empty")
        return False
    
    url = f'{BASE_URL}/repos/{owner}/{repo}/issues/{issue_num}/comments'
    data = {'body': body}
    resp = requests.post(url, headers=HEADERS, json=data)
    
    if resp.status_code == 201:
        comment = resp.json()
        print(f"✓ Comment posted successfully")
        print(f"URL: {comment['html_url']}")
        return True
    else:
        print(f"[Error] {resp.status_code}: {resp.text}")
        return False


def create_pr(owner: str, repo: str, title_file: str, body_file: str, head: str, base: str) -> bool:
    """创建 Pull Request"""
    # 读取标题
    if os.path.exists(title_file):
        with open(title_file, 'r', encoding='utf-8') as f:
            title = f.read().strip()
    else:
        title = title_file  # 直接传入字符串
    
    # 读取 body
    if os.path.exists(body_file):
        with open(body_file, 'r', encoding='utf-8') as f:
            body = f.read()
    else:
        body = body_file  # 直接传入字符串
    
    if not title:
        print("[Error] Title is empty")
        return False
    
    url = f'{BASE_URL}/repos/{owner}/{repo}/pulls'
    data = {
        'title': title,
        'body': body,
        'head': head,
        'base': base
    }
    resp = requests.post(url, headers=HEADERS, json=data)
    
    if resp.status_code == 201:
        pr = resp.json()
        print(f"✓ PR created successfully")
        print(f"Number: #{pr['number']}")
        print(f"URL: {pr['html_url']}")
        return True
    else:
        print(f"[Error] {resp.status_code}: {resp.text}")
        return False


def get_repo(owner: str, repo: str) -> bool:
    """获取仓库信息"""
    url = f'{BASE_URL}/repos/{owner}/{repo}'
    resp = requests.get(url, headers=HEADERS)
    
    if resp.status_code == 200:
        repo_info = resp.json()
        print(f"=== {owner}/{repo} ===")
        print(f"Name: {repo_info['name']}")
        print(f"Full Name: {repo_info['full_name']}")
        print(f"Description: {repo_info.get('description', 'N/A')}")
        print(f"Default Branch: {repo_info['default_branch']}")
        print(f"Open Issues: {repo_info['open_issues_count']}")
        print(f"URL: {repo_info['html_url']}")
        return True
    else:
        print(f"[Error] {resp.status_code}: {resp.text}")
        return False


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    action = sys.argv[1].lower()
    
    # 本地 Git 操作
    if action == 'status' and 2 <= len(sys.argv) <= 3:
        sys.exit(0 if status(sys.argv[2] if len(sys.argv) == 3 else '.') else 1)
    
    elif action == 'diff-staged' and 2 <= len(sys.argv) <= 3:
        sys.exit(0 if show_diff(sys.argv[2] if len(sys.argv) == 3 else '.', staged=True) else 1)
    
    elif action == 'diff' and 2 <= len(sys.argv) <= 3:
        sys.exit(0 if show_diff(sys.argv[2] if len(sys.argv) == 3 else '.', staged=False) else 1)
    
    elif action == 'commit' and len(sys.argv) >= 4:
        # commit <path> <message> [files]
        path = sys.argv[2]
        message = sys.argv[3]
        files = sys.argv[4] if len(sys.argv) >= 5 else None
        sys.exit(0 if commit(path, message, files) else 1)
    
    elif action == 'discard' and len(sys.argv) == 3:
        sys.exit(0 if discard(sys.argv[2]) else 1)
    
    elif action == 'branch-log' and 4 <= len(sys.argv) <= 5:
        from_branch = sys.argv[2]
        to_branch = sys.argv[3]
        path = sys.argv[4] if len(sys.argv) == 5 else '.'
        sys.exit(0 if branch_log(from_branch, to_branch, path) else 1)
    
    elif action == 'push' and 3 <= len(sys.argv) <= 4:
        branch = sys.argv[2]
        path = sys.argv[3] if len(sys.argv) == 4 else '.'
        sys.exit(0 if push(branch, path) else 1)
    
    elif action == 'create-issue' and len(sys.argv) >= 5:
        # create-issue <owner> <repo> <title> <body> [labels...]
        owner = sys.argv[2]
        repo = sys.argv[3]
        title = sys.argv[4]
        body = sys.argv[5]
        labels = sys.argv[6].split(',') if len(sys.argv) >= 7 else None
        sys.exit(0 if create_issue(owner, repo, title, body, labels) else 1)
    
    # GitHub Issue 操作
    elif action == 'get-issue' and len(sys.argv) == 5:
        sys.exit(0 if get_issue(sys.argv[2], sys.argv[3], sys.argv[4]) else 1)
    
    elif action == 'get-comments' and len(sys.argv) == 5:
        sys.exit(0 if get_comments(sys.argv[2], sys.argv[3], sys.argv[4]) else 1)
    
    elif action == 'reply-issue' and len(sys.argv) == 6:
        sys.exit(0 if reply_issue(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]) else 1)
    
    # GitHub PR 操作
    elif action == 'create-pr' and len(sys.argv) == 7:
        sys.exit(0 if create_pr(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7]) else 1)
    
    # 仓库操作
    elif action == 'get-repo' and len(sys.argv) == 4:
        sys.exit(0 if get_repo(sys.argv[2], sys.argv[3]) else 1)
    
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()