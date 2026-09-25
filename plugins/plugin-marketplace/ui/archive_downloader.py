# -*- coding: utf-8 -*-
"""GitHub 仓库归档下载通道 — 无 git 环境下安装插件的替代路径

背景：市场插件绝大多数托管在 github.com，原安装链路强依赖本机 git 可执行文件
（``git clone --sparse``）。用户机器没装 git 时必然失败，只能弹一句提示让用户
自己去装，装不顺就得来回折腾。本模块用纯标准库（urllib + tarfile）从 codeload
端点取仓库 tarball，把 git 从"必需"降级为"可选兜底"，绝大多数用户全程无感。

正确性（缓存绝不返回旧内容的关键）：
    缓存 key 用**内容寻址的 commit sha**，不是分支名。分支名会随时间指向新提交，
    sha 不会。下载前先向 GitHub API 解析 ref → sha：
    - 解析成功 → 同名缓存命中即用（零网络），miss 则下载并按 sha 落缓存
    - 解析失败（限流 / 网络不通 / 代理不转 API）→ 完全绕过缓存直连下载，
      下载完从 tarball 顶层目录名反解 sha 再落缓存
    即：宁可每次重下，也绝不拿分支名当 key 返回过期内容。

依赖：仅标准库。不走 httpx（安装线程依赖面越小越好）；代理按 ProxyConfig 模式
分别处理 —— prefix/selfhost 走 URL 改写，http 走 urllib ProxyHandler。
"""

import io
import json
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import quote

from loguru import logger

_ARCHIVE_HOST = "codeload.github.com"
_API_BASE = "https://api.github.com"
_UA = "DriFox/0.5 (+https://github.com/martin98-afk/drifox-plugins)"

# 单次下载超时（秒）：市场插件仓库普遍很小，30s 足够；半开连接不能把安装
# 任务永久挂在「安装中…」
_HTTP_TIMEOUT = 30.0
# sha 解析是"加速可选步骤"，超时要短，不能拖慢安装
_SHA_TIMEOUT = 6.0
# 归档缓存总容量上限（字节），超出按 mtime 由旧到新淘汰
_CACHE_MAX_BYTES = 300 * 1024 * 1024
# 进程内 sha 缓存 TTL（秒）：同一轮装多个同仓插件时复用，避免打爆 API 限流。
# 刻意压到 60s —— 仓库推送新提交后，同一会话内装插件最多延迟 60s 拿到新版，
# 避免长 TTL 让用户装到明显过期的快照。
_SHA_MEM_TTL = 60.0

# 进程内 sha 缓存：{(owner, repo, ref): (ts, sha)}。仅缓存成功解析的结果，
# 失败不写 —— 限流期间若缓存 None 会一直走不到快路径
_mem_sha: Dict[Tuple[str, str, str], Tuple[float, str]] = {}


class ArchiveFetchError(RuntimeError):
    """归档通道下载/解压失败（网络、限流、响应非 tar 等）

    调用方捕获后回退 git 通道或写入 last_error。消息即可读文案。
    """


def parse_github_repo(url: str) -> Optional[Tuple[str, str]]:
    """从 git URL 解析 GitHub (owner, repo)；非 github.com 返回 None

    兼容 ``https://github.com/owner/repo``、``.../repo.git``、``.../repo/tree/branch``
    与加速站前缀后跟随的 github.com 路径。ssh / git 协议与其余 host 一律返回 None
    （那些只能走 git 通道）。
    """
    if not url:
        return None
    marker = "/github.com/"
    lower = url.lower()
    idx = lower.find(marker)
    if idx < 0:
        return None
    rest = url[idx + len(marker):]
    parts = [p for p in rest.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    return owner, repo


def _build_opener(proxy):
    """按代理模式构造 urllib opener（http 模式走 ProxyHandler，其余直通）"""
    if proxy is not None and getattr(proxy, "enabled", False) and getattr(proxy, "mode", "") == "http":
        addr = (getattr(proxy, "address", "") or "").strip()
        if addr:
            return urllib.request.build_opener(urllib.request.ProxyHandler({"http": addr, "https": addr}))
    return urllib.request.build_opener()


def _http_get_bytes(url: str, opener, timeout: float, *, accept: str = "*/*") -> bytes:
    """GET 取字节；HTTP 非 2xx 由 urllib 抛 HTTPError，交由调用方处理"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": accept})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def resolve_commit_sha(owner: str, repo: str, ref: str, proxy=None) -> Optional[str]:
    """解析 ref → commit sha（40 位）；失败返回 None（调用方须绕过缓存）

    未认证 GitHub API 限流 60 次/小时，因此只作为加速步骤：失败不影响安装，
    仅意味着本次不走缓存快路径。
    """
    key = (owner, repo, ref)
    now = time.time()
    hit = _mem_sha.get(key)
    if hit is not None and now - hit[0] < _SHA_MEM_TTL:
        return hit[1]
    url = f"{_API_BASE}/repos/{owner}/{repo}/commits/{quote(ref, safe='')}"
    try:
        raw = _http_get_bytes(
            url, _build_opener(proxy), _SHA_TIMEOUT, accept="application/vnd.github+json"
        )
        sha = json.loads(raw.decode("utf-8")).get("sha")
        if isinstance(sha, str) and len(sha) >= 7:
            _mem_sha[key] = (now, sha)
            return sha
    except Exception as e:
        logger.debug(f"[Installer] 解析 commit sha 失败（绕过缓存直下）: {owner}/{repo}@{ref}: {e}")
    return None


def _archive_candidates(proxy, owner: str, repo: str, ref: str) -> list:
    """codeload URL 候选（加速站改写优先，直连兜底）"""
    direct = f"https://{_ARCHIVE_HOST}/{owner}/{repo}/tar.gz/{quote(ref, safe='')}"
    urls: list = []
    if proxy is not None and getattr(proxy, "enabled", False) and getattr(proxy, "mode", "") != "http":
        rewritten = proxy.rewrite_url(direct)
        if rewritten != direct:
            urls.append(rewritten)
    urls.append(direct)
    return urls


def _sha_from_dirname(name: str) -> Optional[str]:
    """从 GitHub tarball 顶层目录名 ``{repo}-{sha[:7]}`` 反解短 sha

    接受 7~40 位十六进制尾部（GitHub 现行规则是短 sha 7 位，放宽上限以防其
    改用完整 sha；只取前 7 位作缓存 key）。匹配不到返回 None —— 调用方据此
    跳过落缓存，功能不受影响。
    """
    tail = name.rsplit("-", 1)[-1]
    if 7 <= len(tail) <= 40 and all(c in "0123456789abcdef" for c in tail.lower()):
        return tail[:7]
    return None


def _write_cache(path: Path, data: bytes) -> None:
    """原子写缓存（tmp + replace），避免并发/中断留下半文件被误读"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as e:
        logger.warning(f"[Installer] 归档缓存写入失败（忽略）: {path}: {e}")


def _enforce_cache_limit(root: Path, max_bytes: int = _CACHE_MAX_BYTES) -> None:
    """按 mtime 由旧到新淘汰，直到总量回到上限内"""
    try:
        infos = []
        total = 0
        for p in root.glob("*.tar.gz"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            infos.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if total <= max_bytes:
            return
        infos.sort()
        for _, size, p in infos:
            if total <= max_bytes:
                break
            try:
                p.unlink()
                total -= size
            except OSError:
                pass
    except OSError as e:
        logger.debug(f"[Installer] 归档缓存容量清理跳过: {e}")


def _extract_tarball(data: bytes, dest: Path) -> Path:
    """解压到 dest，返回唯一顶层目录（GitHub tarball 恒为单顶层目录）

    ``filter="data"`` 屏蔽绝对路径与 ``..`` 穿越成员，防止恶意仓库写穿目录。
    """
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        tf.extractall(path=dest, filter="data")
    entries = list(dest.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        raise ArchiveFetchError(f"归档结构异常（顶层条目 {len(entries)} 个），无法定位仓库根目录")
    return entries[0]


def fetch_github_repo(
    owner: str,
    repo: str,
    ref: str,
    *,
    cache_root: Path,
    dest_dir: Path,
    proxy=None,
) -> Path:
    """下载并解压仓库到 dest_dir，返回解压后的仓库根目录路径

    Args:
        owner/repo: GitHub 仓库坐标
        ref: 分支 / 标签 / commit
        cache_root: 归档缓存目录（按 sha 内容寻址，跨插件复用）
        dest_dir: 解压落点（调用方负责清理）
        proxy: ProxyConfig 实例，可为 None

    Raises:
        ArchiveFetchError: 下载或解压失败
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    opener = _build_opener(proxy)

    sha = resolve_commit_sha(owner, repo, ref, proxy)
    short_sha = sha[:7] if sha else ""
    cache_file = cache_root / f"{owner}__{repo}__{short_sha}.tar.gz" if sha else None

    data: Optional[bytes] = None
    if cache_file is not None and cache_file.is_file():
        try:
            data = cache_file.read_bytes()
            logger.info(f"[Installer] 归档缓存命中: {owner}/{repo}@{short_sha}")
        except OSError as e:
            logger.warning(f"[Installer] 归档缓存读取失败，改直连: {e}")

    if data is None:
        last: Optional[Exception] = None
        for u in _archive_candidates(proxy, owner, repo, ref):
            try:
                raw = _http_get_bytes(u, opener, _HTTP_TIMEOUT)
            except Exception as e:
                last = e
                logger.warning(f"[Installer] 归档下载失败（{u}）: {e}")
                continue
            if raw[:2] != b"\x1f\x8b":
                last = ArchiveFetchError(f"响应非 gzip（前2字节 {raw[:2]!r}），加速站可能未代理 codeload")
                logger.warning(f"[Installer] {last}")
                continue
            data = raw
            break
        if data is None:
            raise ArchiveFetchError(f"归档下载失败: {last}")

        top = _extract_tarball(data, dest_dir)
        # 解析不到 sha 时从顶层目录名反解（内容寻址，安全），两种路径都能落缓存
        if cache_file is None:
            got = _sha_from_dirname(top.name)
            if got:
                _write_cache(cache_root / f"{owner}__{repo}__{got}.tar.gz", data)
        else:
            _write_cache(cache_file, data)
        _enforce_cache_limit(cache_root)
        return top

    # 缓存命中路径：缓存内容即 sha 对应的快照，解压即可
    try:
        return _extract_tarball(data, dest_dir)
    except tarfile.TarError:
        # 缓存文件损坏（写入中断/磁盘错误）：弃用后直连重下
        broken = cache_file
        logger.warning(f"[Installer] 归档缓存损坏，删除后重下: {broken}")
        if broken is not None:
            try:
                broken.unlink()
            except OSError:
                pass
        return fetch_github_repo(
            owner, repo, ref, cache_root=cache_root, dest_dir=dest_dir, proxy=proxy
        )
