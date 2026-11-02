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
CONFIG_REMOTE = "drifox/app.config"


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
        # __new__ 跳过了 __init__：PyQt5 未初始化 wrapper 上 getattr 缺失属性
        # 会抛 RuntimeError（super-class __init__ was never called），
        # 这里显式置空，让 getattr(..., None) 的防御真正生效。
        svc._meta_recorder = None
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

    def upload_config(self) -> bool:
        import httpx

        orig = cs.get_app_data_dir
        cs.get_app_data_dir = lambda: self.appdata  # type: ignore[assignment]
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
                return self.svc._upload_unit("app.config", CONFIG_REMOTE, self.svc._config_path, client)
        finally:
            cs.get_app_data_dir = orig  # type: ignore[assignment]

    def download_config(self) -> bool:
        orig = cs.get_app_data_dir
        cs.get_app_data_dir = lambda: self.appdata  # type: ignore[assignment]
        try:
            return self.svc._download_config_merge()
        finally:
            cs.get_app_data_dir = orig  # type: ignore[assignment]

    def upload_config_raw(self) -> bool:
        """并发用：不碰模块级 get_app_data_dir，由调用方统一接管。

        `upload_config` 会临时改写 cs.get_app_data_dir，多线程并发时互相踩，
        这里只做上传本身。
        """
        import httpx

        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            return self.svc._upload_unit("app.config", CONFIG_REMOTE, self.svc._config_path, client)

    def store_config(self, data: dict) -> None:
        self.svc._config_path.parent.mkdir(parents=True, exist_ok=True)
        self.svc._config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def config(self) -> dict:
        return json.loads(self.svc._config_path.read_text(encoding="utf-8"))

    def simulate_unbind(self, restored: dict) -> None:
        """解绑：配置回滚到 pre_bind 快照，base 与 SHA 缓存全部清空（同 disable()）"""
        self.store_config(restored)
        self.svc._config_remote_sha = None
        self.svc._custom_remote_sha = None
        self.svc._records_remote_sha = None
        self.svc._ext_remote_sha = {}
        self.svc._content_hashes = {}
        if self.svc._sha_cache_path.exists():
            self.svc._sha_cache_path.unlink()

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
    """云端业务 zip 内的文件（不含归档目录与同步元数据）"""
    info = store.get_file(REMOTE)
    if not info:
        return {}
    import base64
    import io

    raw = base64.b64decode(info["content"])
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return {
            n: zf.read(n).decode("utf-8")
            for n in sorted(zf.namelist())
            if not n.endswith(".__drifox_sync__.json")
        }


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


def _cfg_with_meta(value: str, t: int, extra: dict | None = None, pre_bind: bool = False) -> dict:
    """构造 app.config。

    ``pre_bind=True`` 模拟首次绑定前的快照：文件里有配置值，但没有任何同步
    元数据（entries 空）。真实事故就发生在这个形态：seed_missing 会给所有
    没条目的键打上高于云端的时间戳，本地旧值反压云端。
    """
    cfg: dict = {"UI": {"FontSize": value}}
    if extra:
        for group, items in extra.items():
            cfg.setdefault(group, {}).update(items)
    entries = {} if pre_bind else {"UI.FontSize": {"h": value, "t": t}}
    cfg["System"] = {"SyncMeta": json.dumps(_meta(entries, 0 if pre_bind else t))}
    return cfg

def _meta(entries: dict, clock: int) -> dict:
    return {"version": 1, "clock": clock, "entries": entries}

def scenario_rebind_cloud_wins(tmp: Path, base_url: str) -> tuple[bool, List[str]]:
    """解绑把本地回滚成旧配置 + 重绑 → 云端新版不被旧快照覆盖（真实事故回归）"""
    import base64

    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    notes: List[str] = []
    try:
        a = Device("A", tmp / "rebind" / "A", base)
        old_cfg = _cfg_with_meta("12", 1, pre_bind=True)
        a.store_config(old_cfg)
        assert a.upload_config(), "首次上传失败"

        new_cfg = _cfg_with_meta("16", 2)
        a.store_config(new_cfg)
        assert a.upload_config(), "第二次上传失败"

        # 解绑：本地回滚到 pre_bind（12），base 与 SHA 缓存清空
        a.simulate_unbind(old_cfg)
        assert a.download_config(), "重绑下载失败"
        got = a.config()["UI"]["FontSize"]
        notes.append(f"解绑回滚 12 → 重绑合并后 {got}")

        # 再推一次，云端必须仍是 16
        a.upload_config()
        remote_raw = base64.b64decode(store.get_file(CONFIG_REMOTE)["content"])
        remote_value = json.loads(remote_raw)["UI"]["FontSize"]
        notes.append(f"上传后云端 FontSize={remote_value}")
        return got == "16" and remote_value == "16", notes
    finally:
        server.shutdown()

def scenario_rebind_keeps_local_only(tmp: Path, base_url: str) -> tuple[bool, List[str]]:
    """云端权威只裁决冲突项：重绑时本地独有的键仍保留"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    notes: List[str] = []
    try:
        a = Device("A", tmp / "rebind-only" / "A", base)
        a.store_config(_cfg_with_meta("16", 2))
        assert a.upload_config(), "上传失败"

        b = Device("B", tmp / "rebind-only" / "B", base)
        local_cfg = _cfg_with_meta("12", 1, extra={"UI": {"OnlyLocal": 1}}, pre_bind=True)
        b.simulate_unbind(local_cfg)
        assert b.download_config(), "重绑下载失败"
        data = b.config()
        notes.append(f"B 合并后: {data['UI']}")
        return data["UI"]["FontSize"] == "16" and data["UI"].get("OnlyLocal") == 1, notes
    finally:
        server.shutdown()

def scenario_rebind_tombstone(tmp: Path, base_url: str) -> tuple[bool, List[str]]:
    """解绑回滚让已删服务商复活？重绑后墓碑必须继续生效"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    notes: List[str] = []
    try:
        a = Device("A", tmp / "rebind-tomb" / "A", base)
        a.store_config(
            {
                "LLM": {"SavedProviders": {"p1": {"name": "a"}}},
                "System": {
                    "SyncMeta": json.dumps(
                        _meta(
                            {
                                "LLM.SavedProviders.p1": {"h": "h1", "t": 1},
                                "LLM.SavedProviders.p2": {"h": "h2", "t": 9, "d": True},
                            },
                            9,
                        )
                    )
                },
            }
        )
        assert a.upload_config(), "上传失败"

        b = Device("B", tmp / "rebind-tomb" / "B", base)
        b.simulate_unbind(
            {
                "LLM": {"SavedProviders": {"p1": {"name": "a"}, "p2": {"name": "b"}}},
                "System": {"SyncMeta": json.dumps(_meta({}, 0))},
            }
        )
        assert b.download_config(), "重绑下载失败"
        providers = set(b.config()["LLM"]["SavedProviders"])
        notes.append(f"B 合并后服务商: {sorted(providers)}")
        return providers == {"p1"}, notes
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


def _concurrent_upload_unit(
    devices: List[Device], tmp: Path, unit: str, remote: str, local_of
) -> int:
    """并发上传指定单元，返回失败数（Gitee sha 乐观锁会返回 409）。

    每线程通过线程局部把 get_app_data_dir 指向自己的设备目录：合并用的临时
    目录（cache/sync_base/_merge_*）是固定名，若所有线程共用一个根就会互相
    rmtree，表现为随机丢文件。
    """
    import threading

    import httpx
    from concurrent.futures import ThreadPoolExecutor

    shared = tmp / "_shared_backup"
    shared.mkdir(parents=True, exist_ok=True)
    tls = threading.local()
    orig = cs.get_app_data_dir
    cs.get_app_data_dir = lambda: getattr(tls, "root", shared)  # type: ignore[assignment]

    def work(d: Device) -> bool:
        tls.root = d.appdata
        try:
            with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
                return d.svc._upload_unit(unit, remote, local_of(d), client)
        finally:
            tls.root = None

    try:
        with ThreadPoolExecutor(max_workers=len(devices)) as pool:
            results = list(pool.map(work, devices))
    finally:
        cs.get_app_data_dir = orig  # type: ignore[assignment]
    return sum(1 for r in results if not r)

def _concurrent_upload(devices: List[Device], tmp: Path) -> int:
    """并发上传 app.config"""
    return _concurrent_upload_unit(devices, tmp, "app.config", CONFIG_REMOTE, lambda d: d.svc._config_path)

def _converge(devices: List[Device], store: FakeGiteeStore, rounds: int = 8) -> tuple[bool, List[dict]]:
    """串行同步直到所有设备与云端一致，返回 (是否收敛, 最终状态列表)"""
    states: List[dict] = []
    for _ in range(rounds):
        for d in devices:
            d.upload_config()
        for d in devices:
            d.download_config()
        states = [_fuzz_local(d) for d in devices] + [_fuzz_cloud(store)]
        if all(s == states[0] for s in states):
            return True, states
    return False, states

def scenario_stress(n: int = 10) -> tuple[bool, List[str]]:
    """N 端并发修改 + 并发上传：乐观锁冲突后必须收敛，不丢键、不产生非法值"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    tmp = Path(tempfile.mkdtemp(prefix="drifox-stress-"))
    notes: List[str] = []
    ok = True
    try:
        devices = [Device(f"D{i}", tmp / f"d{i}", base) for i in range(n)]

        # ── 阶段 1：各端写自己的键，串行上传建立基线 ──
        keys = [f"K{i}" for i in range(n)]
        for i, d in enumerate(devices):
            _fuzz_apply(d, keys[i], "set", f"base-{i}")
            if not d.upload_config():
                ok = False
                notes.append(f"D{i} 基线上传失败")
        for d in devices:
            d.download_config()
        notes.append(f"基线: {len(_fuzz_cloud(store))} 个键")

        # ── 阶段 2：各端改自己的键 → 并发上传 ──
        for i, d in enumerate(devices):
            _fuzz_apply(d, keys[i], "set", f"v{i}")
        fails = _concurrent_upload(devices, tmp)
        notes.append(f"并发上传（各改各键）: 失败 {fails}/{n}（sha 乐观锁 409）")

        converged, states = _converge(devices, store)
        cloud = states[-1]
        notes.append(f"收敛={converged} cloud_keys={len(cloud)}")
        if not converged:
            ok = False
            notes.append(f"  未收敛: {[sorted(s) for s in states[:3]]}...")
        for i, k in enumerate(keys):
            want = f"v{i}"
            got = [s.get(k) for s in states]
            if any(g != want for g in got):
                ok = False
                notes.append(f"  丢键/回退 {k}: 期望 {want}，实际 {set(got)}")

        # ── 阶段 3：N 端同时写同一个键 → 并发上传 ──
        for i, d in enumerate(devices):
            _fuzz_apply(d, "FontSize", "set", f"same-{i}")
        fails2 = _concurrent_upload(devices, tmp)
        notes.append(f"并发上传（同写 FontSize）: 失败 {fails2}/{n}")
        converged2, states2 = _converge(devices, store)
        values = {s.get("FontSize") for s in states2}
        notes.append(f"收敛={converged2} FontSize 取值={sorted(str(v) for v in values)}")
        if not converged2:
            ok = False
        if len(values) != 1 or not str(next(iter(values))).startswith("same-"):
            ok = False
            notes.append("  同一键并发写后未归一，或出现非法值")

        # ── 阶段 4：一端删除，其余端并发上传，删除不得复活 ──
        _fuzz_apply(devices[0], "K0", "del", "")
        devices[0].upload_config()
        fails3 = _concurrent_upload(devices[1:], tmp)
        notes.append(f"删除后其余端并发上传: 失败 {fails3}/{n - 1}")
        converged3 = False
        states3: List[dict] = []
        for _r in range(8):
            for d in devices:
                d.upload_config()
            for d in devices:
                d.download_config()
            states3 = [_fuzz_local(d) for d in devices] + [_fuzz_cloud(store)]
            if all(s == states3[0] for s in states3):
                converged3 = True
                break
        if not converged3:
            ok = False
            notes.append(f"  删除后未收敛: {sorted(states3[0])} vs {sorted(states3[-1])}")
        alive = [f"D{i}" if i < n else "cloud" for i, s in enumerate(states3) if "K0" in s]
        if alive:
            ok = False
            notes.append(f"  K0 删除后复活于: {alive}")

        return ok, notes
    finally:
        server.shutdown()
        shutil.rmtree(str(tmp), ignore_errors=True)

def scenario_stress_fault(n: int = 10) -> tuple[bool, List[str]]:
    """N 端并发 + 服务端故障（5xx / 写成功但响应丢失）：恢复后必须收敛且不丢键"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    tmp = Path(tempfile.mkdtemp(prefix="drifox-fault-"))
    notes: List[str] = []
    ok = True
    try:
        devices = [Device(f"D{i}", tmp / f"d{i}", base) for i in range(n)]
        keys = [f"K{i}" for i in range(n)]
        for i, d in enumerate(devices):
            _fuzz_apply(d, keys[i], "set", f"base-{i}")
            assert d.upload_config(), f"D{i} 基线上传失败"
        for d in devices:
            d.download_config()

        store.fail_rate = 0.3
        store.drop_response_rate = 0.2
        for i, d in enumerate(devices):
            _fuzz_apply(d, keys[i], "set", f"v{i}")
        fails = _concurrent_upload(devices, tmp)
        notes.append(f"故障期并发上传: 失败 {fails}/{n}")

        store.fail_rate = 0.0
        store.drop_response_rate = 0.0
        converged, states = _converge(devices, store, rounds=12)
        notes.append(f"恢复后收敛={converged} cloud_keys={len(states[-1])}")
        if not converged:
            ok = False
            notes.append(f"  未收敛 cloud={sorted(states[-1])}")
        for i, k in enumerate(keys):
            got = states[i].get(k)
            if got != f"v{i}":
                ok = False
                notes.append(f"  丢键/回退 {k}: 期望 v{i} 实际 {got}")
        return ok, notes
    finally:
        server.shutdown()
        shutil.rmtree(str(tmp), ignore_errors=True)

def scenario_stress_dir(n: int = 10) -> tuple[bool, List[str]]:
    """N 端各加一个 user-custom 文件 + 并发上传目录单元：全部保留"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    tmp = Path(tempfile.mkdtemp(prefix="drifox-dir-"))
    notes: List[str] = []
    ok = True
    try:
        devices = [Device(f"D{i}", tmp / f"d{i}", base) for i in range(n)]
        want = {f"f{i}.md" for i in range(n)} | {"seed.md"}
        for d in devices:
            (d.user_custom / "seed.md").write_text("seed", encoding="utf-8")
            d.svc._dir_sync_meta("user-custom", d.user_custom)
        devices[0].upload()
        for d in devices[1:]:
            d.download()

        for i, d in enumerate(devices):
            (d.user_custom / f"f{i}.md").write_text(f"v{i}", encoding="utf-8")
            d.svc._dir_sync_meta("user-custom", d.user_custom)
        fails = _concurrent_upload_unit(devices, tmp, "user-custom", REMOTE, lambda d: d.user_custom)
        notes.append(f"目录并发上传: 失败 {fails}/{n}")

        for _ in range(8):
            for d in devices:
                d.upload()
            for d in devices:
                d.download()
            if all(want <= set(d.files()) for d in devices) and want <= set(cloud_files(store)):
                break

        missing_cloud = want - set(cloud_files(store))
        if missing_cloud:
            ok = False
            notes.append(f"  云端缺文件: {sorted(missing_cloud)}")
        for i, d in enumerate(devices):
            missing = {f"f{i}.md"} - set(d.files())
            if missing:
                ok = False
                _meta = d.svc._load_dir_meta("user-custom")
                _entries = _meta.get("entries") or {}
                notes.append(
                    f"  D{i} 缺 {sorted(missing)} 条目={_entries.get(f'f{i}.md')} "
                    f"clock={_meta.get('clock')} files={sorted(d.files())}"
                )
        notes.append(f"云端文件数={len(cloud_files(store))}")
        return ok, notes
    finally:
        server.shutdown()
        shutil.rmtree(str(tmp), ignore_errors=True)

def _fuzz_provider(device: Device, pid: str, value: str) -> None:
    """改集合型配置项里的一个服务商（模拟 _diff_dict 打条目）"""
    cfg = (
        device.config()
        if device.svc._config_path.exists()
        else {"LLM": {"SavedProviders": {}}, "System": {"SyncMeta": json.dumps(_meta({}, 0))}}
    )
    providers = cfg.setdefault("LLM", {}).setdefault("SavedProviders", {})
    providers[pid] = {"name": value}
    _fuzz_record(cfg, f"LLM.SavedProviders.{pid}", value, device=device.name)
    device.store_config(cfg)

def _fuzz_meta_clock(device: Device) -> tuple:
    """(clock, 条目数)，诊断用"""
    try:
        meta = json.loads((device.config().get("System") or {}).get("SyncMeta") or "{}")
        return int(meta.get("clock") or 0), len(meta.get("entries") or {})
    except Exception:
        return -1, -1

def _fuzz_cloud_meta(store: FakeGiteeStore) -> tuple:
    """云端 app.config 的 (clock, 条目数)，诊断用"""
    import base64

    info = store.get_file(CONFIG_REMOTE)
    if not info:
        return None
    try:
        data = json.loads(base64.b64decode(info["content"]))
        meta = json.loads((data.get("System") or {}).get("SyncMeta") or "{}")
        return int(meta.get("clock") or 0), len(meta.get("entries") or {})
    except Exception:
        return None

def _fuzz_providers(device: Device) -> dict:
    cfg = device.config() if device.svc._config_path.exists() else {}
    return dict((cfg.get("LLM") or {}).get("SavedProviders") or {})

def _fuzz_cloud_providers(store: FakeGiteeStore) -> dict:
    import base64

    info = store.get_file(CONFIG_REMOTE)
    if not info:
        return {}
    data = json.loads(base64.b64decode(info["content"]))
    return dict((data.get("LLM") or {}).get("SavedProviders") or {})

def scenario_stress_large(n: int = 10, providers: int = 200) -> tuple[bool, List[str]]:
    """大集合配置（providers 个服务商）× N 端并发改各自那一批：全部保留且收敛"""
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    tmp = Path(tempfile.mkdtemp(prefix="drifox-large-"))
    notes: List[str] = []
    ok = True
    try:
        devices = [Device(f"D{i}", tmp / f"d{i}", base) for i in range(n)]
        d0 = devices[0]
        cfg: dict = {"LLM": {"SavedProviders": {}}, "System": {"SyncMeta": json.dumps(_meta({}, 0))}}
        for i in range(providers):
            cfg["LLM"]["SavedProviders"][f"p{i}"] = {"name": f"base{i}"}
            _fuzz_record(cfg, f"LLM.SavedProviders.p{i}", f"base{i}", device="D0")
        d0.store_config(cfg)
        assert d0.upload_config(), "基线上传失败"
        for d in devices[1:]:
            d.download_config()
        notes.append(
            f"基线: {len(_fuzz_cloud_providers(store))} 个服务商 "
            f"cloud_meta={_fuzz_cloud_meta(store)} D9={_fuzz_meta_clock(devices[9])}"
        )

        per = providers // n
        expect: dict = {}
        for di, d in enumerate(devices):
            for j in range(per):
                pid = f"p{di * per + j}"
                value = f"v{di}-{j}"
                _fuzz_provider(d, pid, value)
                expect[pid] = value

        fails = _concurrent_upload(devices, tmp)
        notes.append(f"大配置并发上传: 失败 {fails}/{n}")

        converged = False
        states: List[dict] = []
        for _ in range(12):
            for d in devices:
                d.upload_config()
            for d in devices:
                d.download_config()
            states = [_fuzz_providers(d) for d in devices] + [_fuzz_cloud_providers(store)]
            if all(s == states[0] for s in states):
                converged = True
                break
        notes.append(f"收敛={converged} cloud={len(states[-1])}/{providers}")
        if not converged:
            ok = False
        cloud = states[-1]
        if len(cloud) != providers:
            ok = False
            notes.append(f"  服务商数量不符: {len(cloud)} != {providers}")
        bad = [pid for pid, want in expect.items() if (cloud.get(pid) or {}).get("name") != want]
        if bad:
            ok = False
            _pid = bad[0]
            notes.append(
                f"  值不符 {len(bad)} 项，示例 {_pid}: cloud={cloud.get(_pid)} "
                f"expect={expect.get(_pid)}"
            )
        return ok, notes
    finally:
        server.shutdown()
        shutil.rmtree(str(tmp), ignore_errors=True)

def _patch_qt_signals() -> None:
    """仿真设备用 __new__ 构造（无 QObject 初始化），pyqtSignal 属性不可访问。

    用 MagicMock 顶替 `_reloadSettings`，让合并后的「主线程重载」请求可被记录。
    """
    from unittest.mock import MagicMock

    if not isinstance(cs.ConfigSyncService._reloadSettings, MagicMock):
        cs.ConfigSyncService._reloadSettings = MagicMock()  # type: ignore[assignment]

# ─────────────────────────────────────────────────────────────
# 随机序列 fuzz：不列场景，钉住不变量
#
# 四条不变量（每轮随机操作结束后检查）：
#   1. 收敛   —— 两台设备与云端最终内容一致
#   2. 不回退 —— 最后一次 set 的键，收敛后各处都是那个值
#   3. 不复活 —— 最后一次 del 的键，收敛后各处都不存在
#   4. 不丢键 —— 收敛后不出现模型外的键
#
# 键固定归属一台设备（单写者），保证写入有全序，末态才可断言。
# ─────────────────────────────────────────────────────────────

FUZZ_KEYS = ["FontSize", "FontFamily", "ThemeMode", "DpiScale", "Zoom", "Lang"]

def _fuzz_record(config: dict, path: str, value: str, deleted: bool = False, device: str = "") -> None:
    """模拟 SyncMetaRecorder：写入/删除时打一个新的 Lamport 计数"""
    meta = json.loads((config.get("System") or {}).get("SyncMeta") or "{}")
    if not isinstance(meta, dict) or not isinstance(meta.get("entries"), dict):
        meta = _meta({}, 0)
    clock = int(meta.get("clock") or 0) + 1
    entry: dict = {"h": "del" if deleted else value, "t": clock}
    if device:
        entry["dv"] = device
    if deleted:
        entry["d"] = True
    meta["entries"][path] = entry
    meta["clock"] = clock
    config.setdefault("System", {})["SyncMeta"] = json.dumps(meta, ensure_ascii=False, sort_keys=True)

def _fuzz_apply(device: Device, key: str, mode: str, value: str) -> None:
    cfg = device.config() if device.svc._config_path.exists() else {"UI": {}}
    ui = cfg.setdefault("UI", {})
    path = f"UI.{key}"
    if mode == "del":
        ui.pop(key, None)
        _fuzz_record(cfg, path, "", deleted=True, device=device.name)
    else:
        ui[key] = value
        _fuzz_record(cfg, path, value, device=device.name)
    device.store_config(cfg)

def _fuzz_local(device: Device) -> dict:
    cfg = device.config() if device.svc._config_path.exists() else {}
    return dict(cfg.get("UI") or {})

def _fuzz_entry(device: Device, key: str) -> dict:
    """读某设备上某键的同步条目（调试用）"""
    try:
        cfg = device.config()
        meta = json.loads((cfg.get("System") or {}).get("SyncMeta") or "{}")
        return dict((meta.get("entries") or {}).get(f"UI.{key}") or {})
    except Exception:
        return {}

def _fuzz_cloud(store: FakeGiteeStore) -> dict:
    import base64

    info = store.get_file(CONFIG_REMOTE)
    if not info:
        return {}
    data = json.loads(base64.b64decode(info["content"]))
    return dict(data.get("UI") or {})

def scenario_fuzz(seed: int, steps: int = 30) -> tuple[bool, List[str]]:
    """随机操作序列：解绑/重绑/删除/覆盖混在一起，收敛后必须满足四条不变量"""
    import random

    rng = random.Random(seed)
    notes: List[str] = []
    store = FakeGiteeStore()
    server, base = start_server(store)
    cs.GITEE_FILE_API = api_url_template(base)
    tmp = Path(tempfile.mkdtemp(prefix="drifox-fuzz-"))
    try:
        a = Device("A", tmp / "A", base)
        b = Device("B", tmp / "B", base)
        devices = [a, b]
        owner = {k: rng.choice(devices) for k in FUZZ_KEYS}
        expect: dict = {}
        regress: List[str] = []
        snapshots: dict = {"A": [], "B": []}
        ops: List[str] = []

        def sync_all() -> None:
            for d in devices:
                d.upload_config()
            for d in devices:
                d.download_config()

        def snap(d: Device) -> None:
            if d.svc._config_path.exists():
                snapshots[d.name].append(json.loads(d.svc._config_path.read_text(encoding="utf-8")))
                del snapshots[d.name][:-5]

        sync_all()

        # 预热：两台设备都先写入并上传，确保云端有文件、clock 非 0。
        # 否则设备从 clock=0 开始写入，t 必然低于云端历史，属于「无基准」
        # 语义下的预期结局，不适合当作回归断言。
        for d in devices:
            _fuzz_apply(d, "FontSize", "set", f"seed-{d.name}")
            snap(d)
        sync_all()

        for _ in range(steps):
            key = rng.choice(FUZZ_KEYS)
            dev = owner[key]
            roll = rng.random()
            if roll < 0.55:
                value = f"v{rng.randint(1, 99)}"
                snap(dev)
                _fuzz_apply(dev, key, "set", value)
                expect[key] = ("set", value)
                ops.append(f"{dev.name} set {key}={value}")
            elif roll < 0.8:
                snap(dev)
                _fuzz_apply(dev, key, "del", "")
                expect[key] = ("del",)
                ops.append(f"{dev.name} del {key}")
            elif roll < 0.9:
                dev.upload_config()
                ops.append(f"{dev.name} upload")
            else:
                history = snapshots[dev.name]
                if history:
                    # 解绑：本地回滚到旧快照 → 重绑首次同步拉回云端最新值。
                    # 回滚会覆盖本地未上传的改动，被回滚到的键不再可断言，
                    # 从模型里移除（其余键仍严格检查）。
                    restored = dict(rng.choice(history))
                    # 真实 pre_bind 是首次绑定前的文件：有配置值，没有任何
                    # 同步元数据。剥离 entries 才能复现「本地旧值凭 seed_missing
                    # 打出高时间戳反压云端」的形态。
                    restored["System"] = {"SyncMeta": json.dumps(_meta({}, 0))}
                    cloud_before = _fuzz_cloud(store)
                    # 回滚会把该设备本地的所有键都覆盖回旧快照状态（含未上传
                    # 的改动），它拥有的键末态不再可断言。
                    for k in set(restored.get("UI") or {}) | {k for k, d in owner.items() if d is dev}:
                        expect.pop(k, None)
                    dev.simulate_unbind(restored)
                    dev.download_config()
                    # 重绑后 watcher 会触发一次上传：云端不得被旧快照回退
                    dev.upload_config()
                    cloud_after = _fuzz_cloud(store)
                    for _k, _v in (restored.get("UI") or {}).items():
                        if _k in cloud_before and cloud_before[_k] != _v and cloud_after.get(_k) == _v:
                            regress.append(f"{_k}: 云端 {cloud_before[_k]} → 旧快照 {_v}")
                    ops.append(f"{dev.name} unbind+rebind")
                else:
                    dev.download_config()
                    ops.append(f"{dev.name} download")

        # 强制收敛：反复同步直到一致或超轮数
        converged = False
        for _round in range(10):
            sync_all()
            if _fuzz_local(a) == _fuzz_local(b) == _fuzz_cloud(store):
                converged = True
                break

        sa, sb, sc = _fuzz_local(a), _fuzz_local(b), _fuzz_cloud(store)
        ok = converged
        if regress:
            ok = False
            for _line in regress:
                notes.append(f"  seed={seed} 云端回退: {_line}")
        if not converged:
            notes.append(f"seed={seed} 未收敛: A={sa} B={sb} cloud={sc}")
            for _k in sorted(set(sa) | set(sb)):
                _ea = _fuzz_entry(a, _k)
                _eb = _fuzz_entry(b, _k)
                notes.append(f"    {_k}: A={_ea} B={_eb}")

        for key, spec in expect.items():
            if spec[0] == "set":
                want = spec[1]
                if not (sa.get(key) == sb.get(key) == sc.get(key) == want):
                    ok = False
                    notes.append(f"  seed={seed} 回退 {key}: 期望 {want}，A={sa.get(key)} B={sb.get(key)} cloud={sc.get(key)}")
            else:
                for name, state in (("A", sa), ("B", sb), ("cloud", sc)):
                    if key in state:
                        ok = False
                        notes.append(f"  seed={seed} 复活 {key} 在 {name}={state.get(key)}")

        extra = (set(sa) | set(sb) | set(sc)) - set(expect)
        # 回滚快照里的键已从 expect 移除（值不可断言），但它们合法存在，
        # 只把「从未被任何设备写过」的键视为异常。
        extra -= {k for k in extra if k in FUZZ_KEYS}
        if extra:
            ok = False
            notes.append(f"  seed={seed} 出现未知键: {sorted(extra)}")

        if not ok:
            notes.append(f"  seed={seed} 操作序列: {' → '.join(ops)}")
        return ok, notes
    finally:
        server.shutdown()
        shutil.rmtree(str(tmp), ignore_errors=True)

SCENARIOS = {
    "unchanged": ("内容未变 → 零写请求", scenario_unchanged),
    "single": ("单端修改 → 另一端同步", scenario_single_change),
    "conflict": ("两端改同一文件 → 留档且目录干净", scenario_conflict),
    "add": ("两端各加文件 → 都保留", scenario_two_sided_add),
    "join": ("新设备加入 → 先合并不覆盖", scenario_late_joiner),
    "delete": ("单端删除 → 跟随删除", scenario_remote_delete),
    "rebind": ("解绑回滚 + 重绑 → 云端新版不被旧快照覆盖", scenario_rebind_cloud_wins),
    "rebind-only": ("重绑时本地独有的键保留", scenario_rebind_keeps_local_only),
    "rebind-tomb": ("重绑后墓碑继续生效", scenario_rebind_tombstone),
}


def main() -> int:
    _patch_qt_signals()
    ap = argparse.ArgumentParser(description="多设备云同步仿真")
    ap.add_argument("-s", "--scenario", choices=list(SCENARIOS), help="只跑指定场景")
    ap.add_argument("--fuzz", type=int, default=0, metavar="N", help="跑 N 轮随机序列不变量测试")
    ap.add_argument("--stress", type=int, default=0, metavar="N", help="跑 N 端并发压力测试")
    ap.add_argument(
        "--stress-fault", type=int, default=0, metavar="N", help="跑 N 端并发 + 服务端故障注入"
    )
    ap.add_argument("--stress-dir", type=int, default=0, metavar="N", help="跑 N 端目录单元并发")
    ap.add_argument(
        "--stress-large",
        type=int,
        default=0,
        metavar="N",
        help="跑 N 端大集合配置并发（200 服务商）",
    )
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    if args.stress_dir:
        ok, notes = scenario_stress_dir(args.stress_dir)
        print(f"[{'PASS' if ok else 'FAIL'}] stress-dir: {args.stress_dir} 端目录并发")
        for line in notes:
            print(f"       {line}")
        return 0 if ok else 1

    if args.stress_large:
        ok, notes = scenario_stress_large(args.stress_large)
        print(f"[{'PASS' if ok else 'FAIL'}] stress-large: {args.stress_large} 端大配置并发")
        for line in notes:
            print(f"       {line}")
        return 0 if ok else 1

    if args.stress_fault:
        ok, notes = scenario_stress_fault(args.stress_fault)
        print(f"[{'PASS' if ok else 'FAIL'}] stress-fault: {args.stress_fault} 端并发 + 故障注入")
        for line in notes:
            print(f"       {line}")
        return 0 if ok else 1

    if args.stress:
        ok, notes = scenario_stress(args.stress)
        print(f"[{'PASS' if ok else 'FAIL'}] stress: {args.stress} 端并发")
        for line in notes:
            print(f"       {line}")
        return 0 if ok else 1

    if args.fuzz:
        failures = 0
        for seed in range(args.fuzz):
            ok, notes = scenario_fuzz(seed)
            print(f"[{'PASS' if ok else 'FAIL'}] fuzz seed={seed}")
            if not ok:
                failures += 1
            for line in notes:
                print(f"       {line}")
        print()
        print(f"结果: {args.fuzz - failures}/{args.fuzz} 通过")
        return 0 if failures == 0 else 1

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
