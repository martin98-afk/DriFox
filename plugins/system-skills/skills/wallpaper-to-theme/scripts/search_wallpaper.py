# -*- coding: utf-8 -*-
"""创意工坊搜索 → 挑壁纸 → 下载 → 接迁移管线

搜索无需任何 key：拉 steamcommunity.com/workshop/browse 页解析。
下载两条路：用户在 Steam 里订阅（自动进本地 workshop 目录）或 steamcmd 匿名下载。
用法：
    python search_wallpaper.py "rain forest" --type scene --sort week --limit 10
    python search_wallpaper.py "anime girl" --preview            # 打开预览页挑选
    python search_wallpaper.py --download 2963605825             # steamcmd 下载单个
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APPID = 431960
BROWSE = ("https://steamcommunity.com/workshop/browse/"
          "?appid={appid}&searchtext={query}&browseshort={limit}"
          "&numperpage={limit}&order={order}{typefilter}")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch(url: str, retries: int = 5) -> str:
    import os
    proxy = os.environ.get("DRIFOX_STEAM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return opener.open(req, timeout=25).read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(
        f"拉取失败（{retries} 次重试后）: {last}\n"
        "steamcommunity.com 直连不稳定：开加速器后重试，或设环境变量 DRIFOX_STEAM_PROXY=http://127.0.0.1:加速器端口")


def search(query: str, limit: int, order: str, wall_type: str) -> list[dict]:
    url = BROWSE.format(appid=APPID, query=urllib.request.quote(query),
                        limit=limit, order=order, typefilter="")
    html = fetch(url)
    # browse 页是 React 应用，数据嵌在页面 JSON 的 results 数组里（旧版 HTML 条目已废弃）
    # 且整段是转义形态 \"key\":...，先反转义再按字段抓
    html = html.replace('\\"', '"')
    items: list[dict] = []
    parts = re.split(r'\{"publishedfileid":"', html)
    for p in parts[1:]:
        wid = p[:p.find('"')]
        if not wid.isdigit() or wid in {i["id"] for i in items}:
            continue

        def _jstr(key: str) -> str:
            m = re.search(rf'"{key}":"((?:[^"\\]|\\.)*)"', p)
            if not m:
                return ""
            try:
                return json.loads(f'"{m.group(1)}"')
            except Exception:
                return m.group(1)

        title = _jstr("title")
        desc = _jstr("short_description")
        preview = _jstr("preview_url")
        size_m = re.search(r'"file_size":"(\d+)"', p)
        size_mb = int(size_m.group(1)) / 1048576 if size_m else 0
        if wall_type != "any":
            # 页面 JSON 不标类型，用描述与体积粗筛：video 类一般 >5MB 且描述含 video/mp4
            blob = (title + desc).lower()
            is_video = "video" in blob or "mp4" in blob
            if wall_type == "video" and not is_video:
                continue
            if wall_type == "scene" and is_video:
                continue
        items.append({"id": wid, "title": title, "author": desc[:40],
                      "subs": 0, "preview": preview, "size_mb": size_mb})
        if len(items) >= limit:
            break
    return items[:limit]


def item_detail(wid: str) -> dict:
    """filedetails 页拿类型与大小（browse 页不含）"""
    html = fetch(f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}")
    t = re.search(r'<div class="rightDetailsBlock">([\s\S]{0,120}?)</div>', html)
    kind = "?"
    if re.search(r'创意工坊物品类型|Workshop Item.*?Video|<div class="detailsStatRight">Video', html, re.I):
        kind = "video"
    elif "scene" in html.lower()[:20000]:
        kind = "scene?"
    size = re.search(r'([\d.]+)\s*MB', html)
    return {"type": kind, "size_mb": float(size.group(1)) if size else 0}


def steamcmd_download(wid: str, steamcmd: str) -> int:
    cmd = [steamcmd, "+force_install_dir", str(Path.cwd()),
           "+login", "anonymous", "+workshop_download_item", str(APPID), wid,
           "+quit"]
    return subprocess.run(cmd).returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--type", choices=("scene", "video", "any"), default="any")
    ap.add_argument("--sort", choices=("week", "total", "recent", "textmatch"), default="week",
                    help="week=本周热门 total=历史最多 recent=最新")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--download", help="下载指定 workshop id（需 steamcmd）")
    args = ap.parse_args(argv)

    if args.download:
        steamcmd = shutil_which("steamcmd")
        if not steamcmd:
            print("未找到 steamcmd。两种下载方式：\n"
                  "  a) 在 Steam 里订阅该壁纸，等 WE 同步到 workshop/content/431960/<id>/\n"
                  "  b) 装 steamcmd（https://developer.valvesoftware.com/wiki/SteamCMD）后重试")
            return 2
        return steamcmd_download(args.download, steamcmd)

    if not args.query:
        print("给个关键词，如: search_wallpaper.py \"赛博朋克 城市\" --type scene")
        return 2
    items = search(args.query, args.limit, args.sort, args.type)
    if not items:
        print("无结果")
        return 1
    print(f"{'id':<12} {'大小':>8}  标题 / 描述")
    for it in items:
        print(f"{it['id']:<12} {it.get('size_mb', 0):>6.1f}M  {it['title'][:44]} / {it['author'][:24]}")
        print(f"{'':12} {'':>8}  https://steamcommunity.com/sharedfiles/filedetails/?id={it['id']}")
    print(f"\n挑中后: --download <id>  或在 Steam 订阅后走 wallpaper-to-theme 迁移")
    return 0


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


if __name__ == "__main__":
    raise SystemExit(main())
