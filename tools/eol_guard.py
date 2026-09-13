# -*- coding: utf-8 -*-
"""行尾（EOL）守卫：写文件保持原行尾 + 提交前拦截整文件行尾翻转。

背景
----
Windows 上用 `open(path, "w")` 写文本会把 `\\n` 静默翻成 `\\r\\n`。patch 脚本
一次误写 = 整文件行尾翻转 = 声明改动上千行、实质改动几十行，真实 diff 被淹没
且 git blame 全毁（2026-09-06 一次评审连中 3 个文件）。

本仓库行尾现状：**混用**（CRLF 与 LF 各占一部分），因此不能全仓归一化，
只能「按文件保持原样」+「拦截噪声提交」。

用法
----
    python tools/eol_guard.py check                 # 检查工作区（或 --cached 检查暂存区）
    python tools/eol_guard.py fix <file>            # 按 HEAD 版本风格还原行尾（保留内容改动）
    python tools/eol_guard.py --eol lf normalize    # dry-run；加 --apply 落地

作为库使用：
    from tools.eol_guard import write_text_keep_eol
    write_text_keep_eol(path, text)   # 按文件原主流行尾写回
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def detect_eol(raw: bytes, default: str = "\n") -> str:
    """按字节统计判定主流行尾。"""
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf > lf:
        return "\r\n"
    if lf > 0:
        return "\n"
    return default


def write_text_keep_eol(path: str | Path, text: str, default: str = "\n") -> str:
    """按文件原主流行尾写回文本，返回实际使用的行尾。

    内部以二进制写（`write_bytes`），彻底规避 Windows 文本模式的 \\n→\\r\\n 转换。
    """
    p = Path(path)
    eol = detect_eol(p.read_bytes(), default) if p.exists() else default
    p.write_bytes(text.replace("\r\n", "\n").replace("\n", eol).encode("utf-8"))
    return eol


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True, errors="replace")


def _numstat(extra: list[str]) -> dict[str, tuple[int, int]]:
    out = _git("diff", *extra, "--numstat")
    res: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d = parts[0], parts[1]
        if a == "-" or d == "-":
            continue  # 二进制
        res[parts[2]] = (int(a), int(d))
    return res


def _head_bytes(path: str) -> bytes | None:
    try:
        return subprocess.check_output(["git", "show", f"HEAD:{path}"], cwd=REPO_ROOT)
    except subprocess.CalledProcessError:
        return None


def fix_file(path: str, forced: str | None = None) -> str:
    """把工作区文件行尾还原为 HEAD 版本风格（保留内容改动）。"""
    p = REPO_ROOT / path
    if not p.exists():
        raise FileNotFoundError(path)

    if forced:
        target = "\r\n" if forced == "crlf" else "\n"
    else:
        hb = _head_bytes(path)
        target = detect_eol(hb) if hb else detect_eol(p.read_bytes())

    cur = p.read_bytes()
    body = cur.replace(b"\r\n", b"\n")
    if target == "\r\n":
        body = body.replace(b"\n", b"\r\n")
    if body == cur:
        return "unchanged"
    p.write_bytes(body)
    return "fixed"


def cmd_check(args: argparse.Namespace) -> int:
    extra = ["--cached"] if args.cached else []
    min_lines = args.min_lines
    noise_pct = args.noise_pct

    stat = _numstat(extra)
    wstat = _numstat([*extra, "-w"])
    offenders = []
    for path, (a, d) in stat.items():
        total = a + d
        if total < min_lines:
            continue
        wa, wd = wstat.get(path, (0, 0))
        noise = total - wa - wd
        if noise <= 0:
            continue
        if noise * 100 >= total * noise_pct:
            offenders.append((path, total, noise, wa + wd))

    if not offenders:
        print(f"[eol-guard] {'暂存区' if args.cached else '工作区'}：未发现行尾翻转噪声（阈值 >={noise_pct}%）")
        return 0

    print(f"[eol-guard] {'暂存区' if args.cached else '工作区'}：{len(offenders)} 个文件存在行尾翻转噪声")
    print("")
    print(f"  {'文件':<60}{'声明':>8}{'噪声':>8}{'实质':>8}")
    for path, total, noise, real in sorted(offenders, key=lambda x: -x[2]):
        print(f"  {path:<60}{total:>8}{noise:>8}{real:>8}")
    print("")
    print("  还原：python tools/eol_guard.py fix <file>")
    return 1


def cmd_fix(args: argparse.Namespace) -> int:
    for path in args.files:
        try:
            print(f"  [{fix_file(path, args.eol)}] {path}")
        except FileNotFoundError:
            print(f"  [missing] {path}")
    print("\n  下一步：git diff -w -- <file> 复核实质改动，然后 git add <file>")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="行尾守卫")
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="检查行尾翻转噪声")
    c.add_argument("--cached", action="store_true")
    c.add_argument("--min-lines", type=int, default=20)
    c.add_argument("--noise-pct", type=int, default=80)
    c.set_defaults(func=cmd_check)

    f = sub.add_parser("fix", help="按 HEAD 风格还原行尾")
    f.add_argument("files", nargs="+")
    f.add_argument("--eol", choices=["crlf", "lf"], default=None)
    f.set_defaults(func=cmd_fix)

    ns = ap.parse_args(argv)
    if not getattr(ns, "func", None):
        ap.print_help()
        return 0
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
