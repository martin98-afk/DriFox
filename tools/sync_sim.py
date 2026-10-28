# -*- coding: utf-8 -*-
"""多设备云同步仿真（真实 HTTP + 真实文件系统）。

与单测的区别：这里的 httpx 是真的，HTTP 是真的（打到本地假 Gitee），文件读写
是真的，只有云端被替换成本地服务。因此能覆盖单测 mock 不到的部分：JSON 解析、
base64 往返、sha 冲突的 409 处理、真实请求次数。

跑法：
    .venv/bin/python tools/sync_sim.py            # 全部场景
    .venv/bin/python tools/sync_sim.py -s conflict

真机多开（不经过本脚本，直接开两个应用实例）：
    DRIFOX_DATA_DIR=/tmp/drifox-devA .venv/bin/python main.py
    DRIFOX_DATA_DIR=/tmp/drifox-devB .venv/bin/python main.py
单实例开关默认关闭（General.EnableSingleInstance 默认 False），可以直接双开。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.fake_gitee import FakeGiteeStore, api_url_template, start_server  # noqa: E402

from app.core.sync import config_sync as cs  # noqa: E402
from app.core.sync import fingerprint as fp  # noqa: E402

UNIT = "user-custom"
REMOTE = "drifox/user-custom.zip"


class Device:
    """一台"电脑"：独立数据目录 + 独立同步状态"""

    def __init__(self, name: str, root: Path, base_url: str):
        self.name = name
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.user_custom = root / "plugins" / "user-custom"
        self.user_custom.mkdir(parents=True, exist_ok=True)
        self.appdata = root / "appdata"
        self.appdata.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url

        svc = cs.ConfigSyncService.__new__(cs.ConfigSyncService)
        svc._token = "sim_token"
        svc._owner = "sim_owner"
        svc._state = "idle"
        svc._initial_sync_completed = True
        svc._config_remote_sha = None
        svc._custom_remote_sha = None
        svc._records_remote_sha = None
        svc._ext_remote_sha = {}
        svc._ext_dirty = {}
        svc._content_hashes = {}
        svc._conflict_archive = []
        svc._upload_lock = threading.Lock()
        svc._suppress_until = 0.0
        svc._sync_base_dir = self.appdata / "cache" / "sync_base"
        svc._sha_cache_path = self.appdata / ".sync_shas.json"
        svc._device_tag = name
        svc._user_custom_path = self.user_custom
        svc._config_path = self.appdata / "app.config"
        svc._records_path = self.appdata / "share" / "records.json"
        self.svc = svc

    # ── 操作 ──
    def upload(self) -> bool:
        import httpx

        orig = cs.get_app_data_dir
        cs.get_app_data_dir = lambda: self.appdata  # type: ignore[assignment]
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
                return self.svc._upload_unit(UNIT, REMOTE, self.user_custom, client)
        finally:
            cs.get_app_data_dir = orig  # type: ignore[assignment]

    def download(self) -> bool:
        orig = cs.get_app_data_dir
        cs.get_app_data_dir = lambda: self.appdata  # type: ignore[assignment]
        try:
            return self.svc._download_user_custom_merge()
        finally:
            cs.get_app_data_dir = orig  # type: ignore[assignment]

    # ── 观察 ──
    def files(self) -> Dict[str, str]:
        return {
            p.relative_to(self.user_custom).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(self.user_custom.rglob("*"))
            if p.is_file()
        }

    def conflicts(self) -> List[str]:
        root = self.appdata / "conflicts"
        if not root.exists():
            return []
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def cloud_files(store: FakeGiteeStore) -> Dict[str, str]:
    """云端业务 zip 内的文件（不含归档目录）"""
    info = store.get_file(REMOTE)
    if not info:
        return {}
    import base64
    import io

    raw = base64.b64decode(info["content"])
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return {n: zf.read(n).decode("utf-8") for n in sorted(zf.namelist())}


def cloud_archives(store: FakeGiteeStore) -> List[str]:
    return sorted(p for p in store.files if p.startswith("drifox/conflicts/"))


# ─────────────────────────────────────────────────────────────
# 场景
# ─────────────────────────────────────────────────────────────


def scenario_unchanged(tmp: Path, base_url: str) -> tuple[bool, List[str]]:
    """内容没变 → 一次写请求都不该发"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    notes: List[str] = []
    try:
        a = Device("A", tmp / "unchanged" / "A", base)
        (a.user_custom / "n.md").write_text("v1", encoding="utf-8")
        assert a.upload(), "首次上传失败"
        store.reset_requests()

        before = store.write_count()
        a.upload()  # 内容没变
        after = store.write_count()
        notes.append(f"第一次上传后写请求 {before} 次，内容未变再上传后 {after} 次")
        ok = after == 0
        notes.append(f"云端: {cloud_files(store)}")
        return ok, notes
    finally:
        server.shutdown()


def scenario_single_change(tmp: Path, base_url: str) -> tuple[bool, List[str]]:
    """A 改 → B 同步后拿到，无副本"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    notes: List[str] = []
    try:
        a = Device("A", tmp / "single" / "A", base)
        b = Device("B", tmp / "single" / "B", base)
        (a.user_custom / "n.md").write_text("v1", encoding="utf-8")
        a.upload()
        b.upload()  # B 首次（无基准）→ 合并拉取
        notes.append(f"B 首次同步后: {b.files()}")
        (a.user_custom / "n.md").write_text("v2", encoding="utf-8")
        a.upload()
        b.download()
        notes.append(f"A 改后 B 同步: {b.files()}")
        ok = b.files() == {"n.md": "v2"} and not b.conflicts()
        return ok, notes
    finally:
        server.shutdown()


def scenario_conflict(tmp: Path, base_url: str) -> tuple[bool, List[str]]:
    """两端改同一文件 → 业务目录干净 + 双方留档"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    notes: List[str] = []
    try:
        a = Device("A", tmp / "conflict" / "A", base)
        b = Device("B", tmp / "conflict" / "B", base)
        (a.user_custom / "n.md").write_text("v1", encoding="utf-8")
        a.upload()
        b.upload()
        (a.user_custom / "n.md").write_text("v2-by-A", encoding="utf-8")
        (b.user_custom / "n.md").write_text("v3-by-B", encoding="utf-8")
        b.upload()
        a.upload()  # 冲突
        notes.append(f"A 业务目录: {sorted(a.files())}")
        notes.append(f"A 本地冲突留档: {a.conflicts()}")
        notes.append(f"云端业务 zip: {sorted(cloud_files(store))}")
        notes.append(f"云端冲突归档: {cloud_archives(store)}")
        notes.append(f"云端主文件: {cloud_files(store).get('n.md')}")
        ok = (
            sorted(a.files()) == ["n.md"]
            and a.files()["n.md"] == "v2-by-A"
            and bool(a.conflicts())
            and bool(cloud_archives(store))
        )
        return ok, notes
    finally:
        server.shutdown()


def scenario_two_sided_add(tmp: Path, base_url: str) -> tuple[bool, List[str]]:
    """两端各加不同文件 → 都保留"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    notes: List[str] = []
    try:
        a = Device("A", tmp / "add" / "A", base)
        b = Device("B", tmp / "add" / "B", base)
        (a.user_custom / "base.md").write_text("seed", encoding="utf-8")
        a.upload()
        b.upload()
        (a.user_custom / "only_a.md").write_text("A", encoding="utf-8")
        (b.user_custom / "only_b.md").write_text("B", encoding="utf-8")
        b.upload()
        a.upload()
        b.download()
        notes.append(f"A 本地: {sorted(a.files())}")
        notes.append(f"B 本地: {sorted(b.files())}")
        notes.append(f"云端: {sorted(cloud_files(store))}")
        ok = sorted(cloud_files(store)) == ["base.md", "only_a.md", "only_b.md"]
        return ok, notes
    finally:
        server.shutdown()


def scenario_late_joiner(tmp: Path, base_url: str) -> tuple[bool, List[str]]:
    """新设备（无任何基准）加入 → 拉取云端内容，不覆盖"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    notes: List[str] = []
    try:
        a = Device("A", tmp / "join" / "A", base)
        (a.user_custom / "a.md").write_text("from-A", encoding="utf-8")
        a.upload()
        c = Device("C", tmp / "join" / "C", base)
        (c.user_custom / "c.md").write_text("from-C", encoding="utf-8")
        c.upload()  # 无基准 + 云端已有内容 → 必须先合并
        notes.append(f"C 本地: {sorted(c.files())}")
        notes.append(f"云端: {sorted(cloud_files(store))}")
        ok = sorted(cloud_files(store)) == ["a.md", "c.md"]
        return ok, notes
    finally:
        server.shutdown()


def scenario_remote_delete(tmp: Path, base_url: str) -> tuple[bool, List[str]]:
    """一端删除文件 → 另一端跟随删除（本地未改时）"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    notes: List[str] = []
    try:
        a = Device("A", tmp / "del" / "A", base)
        b = Device("B", tmp / "del" / "B", base)
        (a.user_custom / "keep.md").write_text("k", encoding="utf-8")
        (a.user_custom / "drop.md").write_text("d", encoding="utf-8")
        a.upload()
        b.upload()
        (a.user_custom / "drop.md").unlink()
        a.upload()
        b.download()
        notes.append(f"A: {sorted(a.files())}")
        notes.append(f"B: {sorted(b.files())}")
        ok = sorted(b.files()) == ["keep.md"] and sorted(cloud_files(store)) == ["keep.md"]
        return ok, notes
    finally:
        server.shutdown()


SCENARIOS = {
    "unchanged": ("内容未变 → 零写请求", scenario_unchanged),
    "single": ("单端修改 → 另一端同步", scenario_single_change),
    "conflict": ("两端改同一文件 → 留档且目录干净", scenario_conflict),
    "add": ("两端各加文件 → 都保留", scenario_two_sided_add),
    "join": ("新设备加入 → 先合并不覆盖", scenario_late_joiner),
    "delete": ("单端删除 → 跟随删除", scenario_remote_delete),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="多设备云同步仿真")
    ap.add_argument("-s", "--scenario", choices=list(SCENARIOS), help="只跑指定场景")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    names = [args.scenario] if args.scenario else list(SCENARIOS)
    tmp = Path(tempfile.mkdtemp(prefix="drifox-sync-sim-"))
    failures = 0
    try:
        for name in names:
            title, fn = SCENARIOS[name]
            ok, notes = fn(tmp, "")
            mark = "PASS" if ok else "FAIL"
            if not ok:
                failures += 1
            print(f"[{mark}] {name}: {title}")
            for line in notes:
                print(f"       {line}")
        print()
        print(f"结果: {len(names) - failures}/{len(names)} 通过")
        if args.keep:
            print(f"临时目录保留在: {tmp}")
        return 0 if failures == 0 else 1
    finally:
        if not args.keep:
            shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
