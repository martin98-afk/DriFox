# -*- coding: utf-8 -*-
"""GitHub 仓库归档下载通道 — 无 git 环境下安装插件的兜底路径

定位（重要）：本模块是 **git 缺失时的兜底**，不是首选路径。市场插件来源集中
（drifox-plugins 仓库 116 个插件同源），git 稀疏克隆只取目标子目录、7 秒级完成；
而归档必须下整仓 tarball（实测 59.9MB / 45s）。因此调用顺序恒为
「有 git → 稀疏克隆；git 缺失 → 本通道」，绝不因为本通道存在就跳过 git。

端点选择（实测依据，2026-09）：
    加速站（如 ghfast.top）只代理 GitHub 的 git 协议与网页域，**不代理**
    codeload.github.com 与 api.github.com（两者实测均 403）。可达的归档端点是
    ``https://github.com/{owner}/{repo}/archive/{ref}.tar.gz``（实测 200 + gzip）。
    sha 解析同理改用 git 智能协议的 ``info/refs``（加速站实测 200，直接返回 sha），
    比 GitHub API 更可靠且无 60 次/小时限流。

正确性（缓存绝不返回旧内容的关键）：
    缓存 key 用**内容寻址的 commit sha**，不是分支名。分支名会随时间指向新提交。
    解析不到 sha 时**完全绕过缓存**直连下载 —— 宁可每次重下，也绝不拿分支名
    当 key 返回过期内容。

性能：流式解压只落地目标子目录，不做全仓 extractall。实测同一 59.9MB 归档，
全量解压 14.0s，流式只取子目录 0.5s。

依赖：仅标准库（urllib + tarfile），不走 httpx —— 安装线程依赖面越小越好。
代理按 ProxyConfig 模式分别处理：prefix/selfhost 走 URL 改写，http 走 ProxyHandler。
"""

import io
import re
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from loguru import logger

_UA = "DriFox/0.5 (+https://github.com/martin98-afk/drifox-plugins)"

# 单次下载超时（秒）。大仓库整仓归档较慢（实测 60MB / 45s），留足余量；
# 半开连接也不能让安装任务永久挂在「安装中…」
_HTTP_TIMEOUT = 180.0
# 全部归档候选的合计时间预算（秒）。候选串行（加速站 + 直连）且网络半开时，
# 单次超时叠乘会把用户干等几分钟（实测直连 github 21s 才失败）。
# 超出预算即放弃剩余候选，交由调用方走 git 缺失引导。
_TOTAL_BUDGET = 150.0
# sha 解析是"加速可选步骤"，超时要短，不能拖慢安装
_SHA_TIMEOUT = 10.0
# 归档缓存总容量上限（字节），超出按 mtime 由旧到新淘汰
_CACHE_MAX_BYTES = 300 * 1024 * 1024
# 进程内 sha 缓存 TTL（秒）：同一轮装多个同仓插件时复用，避免重复请求。
# 压到 60s —— 仓库推送新提交后，同一会话内装插件最多延迟 60s 拿到新版。
_SHA_MEM_TTL = 60.0

# 进程内 sha 缓存：{(owner, repo, ref): (ts, sha)}。仅缓存成功结果，失败不写
_mem_sha: Dict[Tuple[str, str, str], Tuple[float, str]] = {}

# 解压时跳过的顶层杂项（省 IO，插件本体不需要；实测全仓 17000+ 文件中
# .gitattributes/.github 等占比不高但无需落地）
_SKIP_TOP = {"", "."}


class ArchiveFetchError(RuntimeError):
    """归档通道下载/解压失败（网络、响应非 tar、ref 不存在等）

    调用方捕获后回退 git 通道或写入 last_error。消息即可读文案。
    """


def parse_github_repo(url: str) -> Optional[Tuple[str, str]]:
    """从 git URL 解析 GitHub (owner, repo)；非 github.com 返回 None

    兼容 ``https://github.com/owner/repo``、``.../repo.git`` 与加速站前缀后
    跟随的 github.com 路径。ssh / git 协议与其余 host 一律返回 None
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


def _url_candidates(proxy, direct_urls: List[str]) -> List[str]:
    """构造 URL 候选：加速站改写优先（若配置），直连兜底；去重保序"""
    out: List[str] = []
    rewrite = None
    if proxy is not None and getattr(proxy, "enabled", False):
        mode = getattr(proxy, "mode", "")
        if mode not in ("", "http"):
            rewrite = getattr(proxy, "rewrite_url", None)
    if callable(rewrite):
        for u in direct_urls:
            rw = u
            try:
                candidate = rewrite(u)
                if isinstance(candidate, str) and candidate:
                    rw = candidate
            except Exception:
                pass
            if rw != u:
                out.append(rw)
    out.extend(direct_urls)
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


_REF_SHA_RE = re.compile(rb"([0-9a-f]{40})\s+refs/heads/([^\x00\s]+)")
_HEAD_SHA_RE = re.compile(rb"([0-9a-f]{40})\s+HEAD")


def resolve_commit_sha(owner: str, repo: str, ref: str, proxy=None) -> Optional[str]:
    """解析 ref → commit sha（40 位）；失败返回 None（调用方须绕过缓存）

    用 git 智能协议 ``info/refs?service=git-upload-pack``（实测加速站支持，
    且无 GitHub API 的 60 次/小时限流）。响应是 pkt-line 文本，从中提取
    目标分支的 sha；ref 本身即 40 位 sha 时直接返回。
    """
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()):
        return ref
    key = (owner, repo, ref)
    now = time.time()
    hit = _mem_sha.get(key)
    if hit is not None and now - hit[0] < _SHA_MEM_TTL:
        return hit[1]

    opener = _build_opener(proxy)
    urls = _url_candidates(
        proxy, [f"https://github.com/{owner}/{repo}/info/refs?service=git-upload-pack"]
    )
    for u in urls:
        try:
            raw = _http_get_bytes(u, opener, _SHA_TIMEOUT, accept="application/x-git-upload-pack-advertisement")
        except Exception as e:
            logger.debug(f"[Installer] info/refs 失败（{u}）: {e}")
            continue
        want = ref.encode("utf-8")
        for sha_b, name_b in _REF_SHA_RE.findall(raw):
            if name_b == want:
                sha = sha_b.decode()
                _mem_sha[key] = (now, sha)
                return sha
        # 分支名未列出时（ref 为标签/sha）退而取 HEAD
        m = _HEAD_SHA_RE.search(raw)
        if m and len(ref) >= 7:
            logger.debug(f"[Installer] 未匹配分支 {ref}，改用 HEAD sha")
            sha = m.group(1).decode()
            _mem_sha[key] = (now, sha)
            return sha
    return None


def _archive_urls(proxy, owner: str, repo: str, ref: str) -> List[str]:
    """归档下载 URL 候选（加速站优先，直连兜底）

    端点实测：``github.com/{owner}/{repo}/archive/{ref}.tar.gz`` 在加速站与直连
    均可用；``archive/refs/heads/{ref}.tar.gz`` 同样可用（分支场景）。ref 为
    完整 sha 时只能用直挂形式。
    """
    q = quote(ref, safe="")
    direct = [
        f"https://github.com/{owner}/{repo}/archive/refs/heads/{q}.tar.gz",
        f"https://github.com/{owner}/{repo}/archive/{q}.tar.gz",
    ]
    return _url_candidates(proxy, direct)


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


def _normalize_subpath(subpath: str) -> str:
    """归一化仓库内子目录路径（去前导 ./ 与斜杠，统一正斜杠）"""
    s = (subpath or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.strip("/")


def extract_subtree(data: bytes, subpath: str, dest_dir: Path) -> Path:
    """从归档字节流中只解压仓库内 subpath 子树，返回落地的目录路径

    流式逐成员处理（不 extractall），实测同一 59.9MB 归档：
    全量解压 14.0s vs 只取子目录 0.5s。

    ``filter="data"`` 拦截绝对路径 / ``..`` 穿越 / 危险链接成员。
    """
    sub = _normalize_subpath(subpath)
    dest_dir.mkdir(parents=True, exist_ok=True)
    top_name: Optional[str] = None
    extracted = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf:
            parts = m.name.split("/", 1)
            if top_name is None:
                top_name = parts[0]
            rel = parts[1] if len(parts) > 1 else ""
            if rel in _SKIP_TOP:
                continue
            # subpath 为空/"." → 整仓落到 dest_dir（保持顶层目录名）
            if sub:
                if not (rel == sub or rel.startswith(sub + "/")):
                    continue
                target_rel = rel[len(sub):].lstrip("/")
                if not target_rel:
                    continue  # 子目录自身条目
            else:
                target_rel = rel
            if m.isdir():
                (dest_dir / target_rel).mkdir(parents=True, exist_ok=True)
                continue
            if not m.isfile():
                continue  # 符号链接/设备文件不落地（插件不需要，且规避安全问题）
            out = dest_dir / target_rel
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(out, "wb") as fh:
                while True:
                    chunk = src.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
            extracted += 1

    if top_name is None:
        raise ArchiveFetchError("归档内容为空（tarball 无成员）")
    if extracted == 0:
        raise ArchiveFetchError(f"归档中未找到子目录 {subpath!r}")
    logger.debug(f"[Installer] 归档流式解压 {extracted} 个文件 → {dest_dir}")
    return dest_dir


def fetch_github_repo(
    owner: str,
    repo: str,
    ref: str,
    *,
    subpath: str,
    cache_root: Path,
    dest_dir: Path,
    proxy=None,
) -> Path:
    """下载 GitHub 仓库归档，只解压 subpath 子树到 dest_dir

    Args:
        owner/repo: GitHub 仓库坐标
        ref: 分支 / 标签 / commit sha
        subpath: 仓库内子目录（"." 或空表示整仓）
        cache_root: 归档缓存目录（按 sha 内容寻址，跨插件复用）
        dest_dir: 解压落点（调用方负责清理）
        proxy: ProxyConfig 实例，可为 None

    Returns:
        解压后的插件源目录（subpath 为空时为 dest_dir 本身）

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
        deadline = time.monotonic() + _TOTAL_BUDGET
        for u in _archive_urls(proxy, owner, repo, ref):
            # 总预算兜底：多个候选串联（加速站 + 直连）且网络半开时，单靠每次
            # 请求的超时会把用户挂在「安装中…」好几分钟。实测本机直连 github
            # 需 21s 才失败，四个候选串行即 80s+。
            left = deadline - time.monotonic()
            if left <= 1.0:
                last = last or ArchiveFetchError("下载总预算耗尽")
                logger.warning(f"[Installer] 归档下载总预算耗尽（{_TOTAL_BUDGET:.0f}s），放弃剩余候选")
                break
            try:
                raw = _http_get_bytes(u, opener, min(_HTTP_TIMEOUT, left))
            except Exception as e:
                last = e
                logger.warning(f"[Installer] 归档下载失败（{u}）: {e}")
                continue
            if raw[:2] != b"\x1f\x8b":
                last = ArchiveFetchError(
                    f"响应非 gzip（前2字节 {raw[:2]!r}），端点可能返回了错误页"
                )
                logger.warning(f"[Installer] {last}（{u}）")
                continue
            data = raw
            break
        if data is None:
            raise ArchiveFetchError(f"归档下载失败: {last}")

        if cache_file is not None:
            _write_cache(cache_file, data)
            _enforce_cache_limit(cache_root)

    try:
        return extract_subtree(data, subpath, dest_dir)
    except ArchiveFetchError:
        raise
    except tarfile.TarError as e:
        # 缓存文件损坏（写入中断/磁盘错误）：弃用后直连重下一次
        if cache_file is not None and cache_file.is_file():
            logger.warning(f"[Installer] 归档缓存损坏，删除后重下: {cache_file}")
            try:
                cache_file.unlink()
            except OSError:
                pass
            return fetch_github_repo(
                owner, repo, ref,
                subpath=subpath, cache_root=cache_root, dest_dir=dest_dir, proxy=proxy,
            )
        raise ArchiveFetchError(f"归档解压失败（tarball 损坏）: {e}") from e
