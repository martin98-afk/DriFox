# -*- coding: utf-8 -*-
"""
会话仓储模块 - 专门负责会话的持久化

从 SessionStore 中提取的会话 CRUD 逻辑。
"""

import hashlib
from datetime import datetime
from pathlib import Path
from threading import RLock, Thread
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.core.store.serde import deserialize, serialize

# 落库时 first_user_msg 的存储上限。团队首问预览只取前 50 字符，留 500 是为了
# 将来 UI 需要更长预览时不必重新回扫 messages BLOB。
FIRST_USER_MSG_STORE_LIMIT = 500

# ============================================================
# message_extras：UI 态字段剥离（specs/2026-09-07-message-extras-offload-design.md）
# ============================================================
# 三字段不进 API 请求（openai serializer 只取 role/content/tool_calls/
# tool_call_id/name），历史轮次剥离进 session_msg_extras 表，回看时按需读回。
OFFLOAD_FIELDS = ("reasoning_content", "arguments", "diff")
# 保活窗：尾部 N 条消息不剥离（活跃轮次参与 API 请求，DeepSeek thinking
# 连续推理依赖最近上下文的 reasoning_content）。
KEEP_RECENT_ROUNDS = 3
# 剥离哨兵：轻量消息携带的绝对索引（int）。渲染/轨迹懒读据此定位 extras 行；
# 老消息与保活窗内消息无哨兵 → 字段本就在消息里，无需查表。
OFFLOAD_IDX_FIELD = "_x_idx"


def _to_sql_bool_or_null(value: Any) -> Optional[int]:
    """把「API 值是否来自 usage」的三态标记转成 SQLite 存储值。

    True → 1，False → 0，None/缺失 → NULL（老记录语义：未知）。
    ⚠️ 绝不能把 None 落成 0：0 表示"这是本地估算值"，会让历史会话里的
    API 权威值被判定为不可用、退回本地重算（token 显示与真实差几倍）。
    """
    if value is None:
        return None
    return 1 if value else 0


def merge_extras_into(messages: List[Any], extras: Dict[int, Dict[str, Any]]) -> List[Any]:
    """按 msg_idx 把 extras 字段就地合并回消息列表（repo 层共享 helper）。

    get_full_messages 与 history-manager 懒回填共用（T1e 问题项收敛）。
    越界索引与非 dict 消息静默跳过（与既有合并分支语义一致）。
    """
    for i, patch in extras.items():
        if 0 <= i < len(messages) and isinstance(messages[i], dict):
            messages[i].update(patch)
    return messages


# ── vision base64 落盘（方案 1，T1d 底稿）──


# 嵌套结构递归哈希的预算：防止深/大结构把指纹成本跑到 str() 之上
_FP_MAX_DEPTH = 6
_FP_MAX_NODES = 2000
_FP_CHUNK = 2048
# 降级摘要采样窗口（字节）：短于 3×窗口时直接整串哈希（零碰撞）；更长时
# 取头/中/尾各一窗。按 UTF-8 字节切片：1024 字节 ≈ 341 个中文汉字或
# 256 emoji，与旧实现的 `str(val)[:1024]` **字符**口径不等价（实际窗口更小）。
# 三窗合计 3KB，成本可忽略；只看头部会让「同长度、前 1KB 相同」的差异漏判。
_FP_TRUNC_SAMPLE = 1024
# 小消息快速路径阈值（粗估字节）：低于此值直接走 `hash(str(msg))`。
# 分片递归只在「大到 str() 明显吃亏」时才划算 —— 实测拐点约 32KB：
#   8KB 的 content list：str() 2.2ms vs 分片 4.6ms（分片更慢）
#   1MB tool_result：   str() 70ms   vs 分片 1.6ms（分片快 44x）
# 取 32KB：覆盖绝大多数普通消息与中等流式内容，只对真正的大消息启用分片。
_FP_FAST_PATH_BYTES = 32768


def _fp_has_long_child(container: Any, depth: int = 3) -> bool:
    """容器内是否含超阈值的长 str/bytes（递归 depth 层，成本仍很低）。

    只下探一层会漏掉 `{role, content:短, tool_result:{payload:{data:500KB}}}`
    这类常见嵌套：它会误走 small 路径（实测 1.58ms vs 分片 0.02ms，差 79x）。
    下探 3 层能覆盖 tool_result / message / block 三级嵌套的惯用结构，且因为
    每层元素数通常很少，总成本仍在微秒级。
    """
    if depth <= 0:
        return False
    values = container.values() if isinstance(container, dict) else container
    try:
        for item in values:
            if isinstance(item, (str, bytes, bytearray)):
                if len(item) >= _FP_FAST_PATH_BYTES:
                    return True
            elif isinstance(item, (dict, list, tuple)):
                if _fp_has_long_child(item, depth - 1):
                    return True
    except Exception:
        return False
    return False


def _fp_needs_chunking(msg: dict) -> bool:
    """快速判定是否需要分片指纹（浅层扫描，成本 ~1μs）。

    分片只在「内容总量大到 str() 明显吃亏」时才划算。判据按**浅层累加长度**：
    - str/bytes 字段累加其长度
    - 容器字段累加其直接元素的 str/bytes 长度（只下探一层）

    累加超过 `_FP_FAST_PATH_BYTES` 即走分片。普通消息累加只有几百字节 →
    走 `str()` 快速路径，与原实现同速；`{content:短, tool_result:{data:500KB}}`
    浅层就能累出 500KB → 走分片拿 20~50x 收益。

    实现上优先走「单字段快判」（含 3 层下探），避免普通消息为累加付出完整遍历。
    """
    for v in msg.values():
        if isinstance(v, (str, bytes, bytearray)):
            if len(v) >= _FP_FAST_PATH_BYTES:
                return True
        elif isinstance(v, (list, tuple, dict)):
            if len(v) >= 128:  # 元素多到一定程度，str() 开销就值得改用分片
                return True
            if _fp_has_long_child(v):
                return True
    # 单字段都不够大：累加浅层长度，覆盖「多个中等字段凑成大内容」
    total = 0
    for v in msg.values():
        if isinstance(v, (str, bytes, bytearray)):
            total += len(v)
        elif isinstance(v, dict):
            for inner in v.values():
                total += len(inner) if isinstance(inner, (str, bytes, bytearray)) else 16
        elif isinstance(v, (list, tuple)):
            for item in v:
                if isinstance(item, (str, bytes, bytearray)):
                    total += len(item)
                elif isinstance(item, dict):
                    for inner in item.values():
                        total += len(inner) if isinstance(inner, (str, bytes, bytearray)) else 16
                else:
                    total += 16
        else:
            total += 16
        if total >= _FP_FAST_PATH_BYTES:
            return True
    return False


def _fp_hash_value(val: Any, depth: int, budget: list) -> Any:
    """把任意值折叠成内容敏感的哈希片段（递归，带深度与节点预算）。

    **必须内容敏感**：末条 content / tool_calls 常为 list[dict]，流式增长发生在
    中间元素的文本上。只取 `(n, head_type, tail_type)` 会漏判这类变化 → 跳过
    保存 → 崩溃窗口丢流式中间态。

    预算耗尽后降级为**内容敏感**的摘要 `(len, hash(str(val)[:1024]))`：
    只取长度的话，同长度的不同内容会得到相同指纹 = 漏判（review 实测
    `DEPTH_TRUNC_SAME` / `NODES_TRUNC_SAME` 均为 True）。降级只发生在前
    1024 字符也相同且长度相同时，真实消息几乎不会触发。

    边界（非绝对保证）：正常结构下内容变化都能被指纹捕捉。降级摘要对 3KB
    以内的值整串哈希（零碰撞）；更长的值取长度 + 头/中/尾各 1KB 哈希，只有
    三窗与长度都相同、变化恰好落在采样间隙时才可能漏判（概率极低，非零）。
    设计取向是「宁可多写一次」，碰撞发生时仍会少写一次。
    """
    budget[0] -= 1
    if budget[0] <= 0 or depth > _FP_MAX_DEPTH:
        try:
            if isinstance(val, (str, bytes, bytearray, memoryview)):
                if isinstance(val, str):
                    b = val.encode("utf-8", "replace")
                elif isinstance(val, memoryview):
                    b = val.tobytes()
                else:
                    b = bytes(val)
            else:
                b = str(val).encode("utf-8", "replace")
            n = len(b)
            if n <= 3 * _FP_TRUNC_SAMPLE:
                # 短结构整串哈希：与长度的双元组零碰撞，成本同旧实现同量级
                return ("~trunc", n, hashlib.blake2b(b, digest_size=16).hexdigest())
            mid = n // 2
            half = _FP_TRUNC_SAMPLE // 2
            return (
                "~trunc",
                n,
                hashlib.blake2b(b[:_FP_TRUNC_SAMPLE], digest_size=16).hexdigest(),
                hashlib.blake2b(b[mid - half : mid + half], digest_size=16).hexdigest(),
                hashlib.blake2b(b[-_FP_TRUNC_SAMPLE:], digest_size=16).hexdigest(),
            )
        except Exception:
            return ("~trunc", 0, 0)
    if isinstance(val, str):
        b = val.encode("utf-8", "replace")
        return ("s", len(b), hash(b[:_FP_CHUNK]), hash(b[-_FP_CHUNK:]))
    if isinstance(val, (bytes, bytearray)):
        b = bytes(val)
        return ("b", len(b), hash(b[:_FP_CHUNK]), hash(b[-_FP_CHUNK:]))
    if isinstance(val, dict):
        # 逐键递归（键排序保证顺序无关），键本身也进指纹
        return (
            "d",
            len(val),
            tuple((str(k), _fp_hash_value(val[k], depth + 1, budget)) for k in sorted(val, key=str)),
        )
    if isinstance(val, (list, tuple)):
        # 逐元素递归：中间元素文本增长会改变对应片段
        return ("l" if isinstance(val, list) else "t", len(val), tuple(
            _fp_hash_value(v, depth + 1, budget) for v in val
        ))
    if isinstance(val, (int, float, bool, type(None))):
        return ("v", type(val).__name__, val)
    try:
        return ("o", type(val).__name__, str(val)[:64])
    except Exception:
        return ("o", type(val).__name__, "")


def _light_msg_fingerprint(msg: Any) -> tuple:
    """末条消息的轻量内容指纹（避免 str() 全量序列化）。

    原实现 `hash(str(last_msg))` 对末条做**全量**字符串化：末条带大 tool_result
    或 base64 图片时，每次保存都要复制并哈希 1MB 级文本。

    ⚠️ **不能用 `id(msg)` 做分量**：`history_manager` 每次保存都
    `copy.deepcopy(messages)`，末条每次都是新对象、新 id，指纹永不相等 →
    「内容未变则跳过写盘」的短路永久失效（实测：比原实现更慢）。

    故只能用**内容派生量**：字段数 + 每字段递归内容哈希（`_fp_hash_value`）。
    字符串/字节取长度 + 首尾 2KB；list/dict 递归到标量叶子，带深度与节点预算。

    保证（按强度递减）：
    - 内容相同 → 指纹相同（去重能生效）
    - 内容不同 → 指纹几乎必然不同。**已知例外（极端）**：
      1) 嵌套超过 `_FP_MAX_DEPTH`(6) 层或超过 `_FP_MAX_NODES`(2000) 个节点，
         且超出部分长度相同、头/中/尾各 1KB 采样窗也相同 → 可能漏判
      2) str/bytes 长度相同、首尾 2KB 相同、中间不同
      两类都要求极端结构，真实消息基本触不到。
    - 小消息不比原实现慢：体积低于 `_FP_FAST_PATH_BYTES` 直接走 `str()`

    例外发生时的后果链：save() 在 `_content_hash_cache` 命中相同 content_key
    时直接 `return True`，跳过序列化与写盘，且不更新缓存（同键会持续命中）；
    后续内容变化若突破指纹差异，会整篇补写（写的是完整 session，之前的变更
    一并落库）。因此这里不是零成本：**若进程在下次成功写盘前退出/崩溃，
    这段未落库的变更会丢失**。
    """
    if not isinstance(msg, dict):
        return ("non-dict", type(msg).__name__, _fp_hash_value(msg, 0, [_FP_MAX_NODES]))
    # 极快路径：字段数少且没有任何长内容 → 直接 str()，零额外开销。
    # 下探 3 层（见 _fp_has_long_child）：单层下探会漏掉
    # {role, content:短, tool_result:{payload:{data:500KB}}} 这类常见嵌套，
    # 导致它误走 str() 慢路径（实测 1.58ms vs 分片 0.02ms）。
    if len(msg) <= 8:
        small = True
        for v in msg.values():
            if isinstance(v, (str, bytes, bytearray)):
                if len(v) >= _FP_FAST_PATH_BYTES:
                    small = False
                    break
            elif isinstance(v, dict):
                if len(v) >= 128 or _fp_has_long_child(v):
                    small = False
                    break
            elif isinstance(v, (list, tuple)):
                if len(v) >= 128 or _fp_has_long_child(v):
                    small = False
                    break
        if small:
            try:
                return ("small", len(msg), hash(str(msg)))
            except Exception:
                pass
    # 小消息快速路径：分片递归只在单项内容很大时才划算
    if not _fp_needs_chunking(msg):
        try:
            return ("small", len(msg), hash(str(msg)))
        except Exception:
            pass
    budget = [_FP_MAX_NODES]
    return ("n", len(msg), tuple(
        (str(k), _fp_hash_value(msg[k], 1, budget)) for k in sorted(msg, key=str)
    ))


def _persist_vision_image_blocks_impl(messages: List[Any], session_id: str, root_dir) -> List[Any]:
    """把消息 content 中 data:image 块替换为 image_ref 引用块（就地修改）。

    - 仅处理 ``type=image_url`` 且 ``image_url.url`` 以 ``data:image`` 开头的块
    - 落盘 ``<root_dir>/screenshots/persisted/<session_id>/sha1[:16]<ext>``
    - 已是 ``type=image_ref`` 的块跳过（幂等：二次 save 不重复落盘）
    - 单块失败隔离：该块保留原样（对齐 save 链既有逐段 try 风格）
    """
    import base64 as _base64
    import hashlib as _hashlib
    import os as _os

    out_dir = Path(root_dir) / "screenshots" / "persisted" / session_id
    # 优化点：原行为=每个 data:image 块都重复 b64decode+sha1（同一图被多条
    # 消息/多块引用时按块数全量重解码，单图可达数 MB），fpath.exists() 只挡
    # 写盘不挡解码；新行为=同一次 persist 调用内按完整 data URI 复用
    # (raw, digest, ext, meta) 解码结果，失败块不缓存保持逐块 try 语义；
    # 根因=解码结果只取决于 b64 串，重复解码是纯浪费。
    decode_cache: Dict[str, tuple] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            img = block.get("image_url")
            url = img.get("url") if isinstance(img, dict) else None
            if not isinstance(url, str) or not url.startswith("data:image"):
                continue
            try:
                header_end = url.find(",")
                if header_end == -1:
                    continue
                cached = decode_cache.get(url)
                if cached is not None:
                    raw, digest, ext, meta = cached
                else:
                    meta = url[len("data:") : header_end]  # image/png;base64
                    ext = ".png" if "png" in meta else (".jpg" if "jpeg" in meta or "jpg" in meta else ".bin")
                    b64 = url[header_end + 1 :]
                    raw = _base64.b64decode(b64)
                    digest = _hashlib.sha1(raw).hexdigest()[:16]
                    decode_cache[url] = (raw, digest, ext, meta)
                out_dir.mkdir(parents=True, exist_ok=True)
                fpath = out_dir / f"{digest}{ext}"
                if not fpath.exists():  # 幂等：同图（同 sha1）不重复写
                    # 原子写：先写同目录 .tmp 再 os.replace，避免进程中断留下
                    # 半截文件（revive 侧读到损坏文件 → QImage 解码失败丢图）。
                    tmp = out_dir / f".{digest}{ext}.tmp"
                    with open(tmp, "wb") as _fh:
                        _fh.write(raw)
                        _fh.flush()
                        _os.fsync(_fh.fileno())
                    _os.replace(tmp, fpath)
                content[bi] = {
                    "type": "image_ref",
                    "image_ref": {"file": fpath.name, "meta": meta, "session_id": session_id},
                }
            except Exception:  # noqa: BLE001 — 单块失败保留原样
                continue
    return messages


def _has_inline_data_image_blocks(messages: List[Any]) -> bool:
    """块级快筛：是否存在未迁移的 data:image 内嵌块（与 persist 链命中条件一致）。

    注：不能用 blob 字符串快筛——serialize 走 zstd 压缩，DB 里的 blob 是
    压缩 bytes，b"data:image/" in blob 永不命中（实测踩坑）。
    """
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            img = block.get("image_url")
            url = img.get("url") if isinstance(img, dict) else None
            if isinstance(url, str) and url.startswith("data:image"):
                return True
    return False

def revive_vision_image_refs(messages: List[Any], root_dir=None) -> List[Any]:
    """把 image_ref 引用块还原为 data:image 块（发送 / API 读侧）。

    session_id 从各块的 image_ref.session_id 取（persist 时写入），
    因此调用方无需知道会话上下文（chat_worker / gateway 读侧均可直用）。

    - 读 ``<root_dir>/screenshots/persisted/<session_id>/<file>`` → base64 data URI
    - >5MB 的 data URI 走 ``app.utils.image_utils.compress_data_uri`` 压缩
    - 文件缺失/IO 失败 → 替换为 text 占位块（不抛错）
    """
    import base64 as _base64

    if root_dir is None:
        from app.utils.app_paths import get_app_data_dir

        root_dir = get_app_data_dir()
    base_dir = Path(root_dir) / "screenshots" / "persisted"
    # 优化点：原行为=每个 image_ref 块都重复 read_bytes+b64encode+压缩（同一
    # 落盘图被多条消息引用时全量重编）；新行为=同一次 revive 调用内按
    # (session_id, file, mime) 复用编码结果，失败/空结果分支不缓存保持逐块
    # 重试语义；根因=同一 ref 的编码结果确定性，重复编码纯浪费。
    encode_cache: Dict[tuple, str] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "image_ref":
                continue
            ref = block.get("image_ref") or {}
            fname = str(ref.get("file") or "")
            sid = str(ref.get("session_id") or "")
            data_uri = ""
            try:
                cache_key = (sid, fname, str(ref.get("meta") or "image/png").split(";")[0])
                cached_uri = encode_cache.get(cache_key)
                cached_hit = cache_key in encode_cache
                if cached_hit:
                    data_uri = cached_uri or ""
                else:
                    fpath = base_dir / sid / fname
                    if fname and fpath.exists():
                        raw = fpath.read_bytes()
                        mime = cache_key[2]
                        b64 = _base64.b64encode(raw).decode("ascii")
                        data_uri = f"data:{mime};base64,{b64}"
                        from app.utils.image_utils import compress_data_uri

                        data_uri = compress_data_uri(data_uri)
                    if data_uri:
                        encode_cache[cache_key] = data_uri
            except OSError:
                data_uri = ""
            except Exception:  # noqa: BLE001 — 解码失败降级占位
                data_uri = ""
            if data_uri:
                content[bi] = {"type": "image_url", "image_url": {"url": data_uri}}
            else:
                content[bi] = {
                    "type": "text",
                    "text": f"[图片已失效：{_ref_basename(fname or 'unknown')}]",
                }
    return messages


def _ref_basename(p: str) -> str:
    return Path(p).name


def extract_offload_fields(messages: List[Any]) -> Tuple[Dict[int, Dict[str, bytes]], List[Any]]:
    """提取保活窗外的 UI 态字段，返回 (extras, 轻量消息副本)。

    - 不修改入参消息：session.messages 是内存唯一事实源（UI/agent_trace
      正在读全量），剥离只作用于写库的轻量副本，且仅待剥离项为浅拷贝
    - 待剥离消息打上 _x_idx 绝对索引哨兵；已带哨兵的消息重复保存时索引不变
      （messages 追加式增长，历史索引稳定）
    """
    n = len(messages)
    keep_from = max(0, n - KEEP_RECENT_ROUNDS)
    extras: Dict[int, Dict[str, bytes]] = {}
    light: List[Any] = []
    for i, msg in enumerate(messages):
        # 保活窗（i >= keep_from）内字段原样保留；窗外（i < keep_from）剥离
        if i >= keep_from or not isinstance(msg, dict):
            light.append(msg)
            continue
        patch = {f: msg[f] for f in OFFLOAD_FIELDS if msg.get(f)}
        if not patch:
            # 🛡️ 回归探测器：带 _x_idx 的消息说明曾从轻量 blob 读出，若其无任何
            # 剥离字段却再次进入保存链，意味着调用方拿到了轻量消息列表。
            # [T2f] save 链已有 _backfill_offload_light_messages 自愈前置：走到
            # 这里说明 backfill 查无 extras 行（真无历史字段），降 DEBUG 常驻；
            # 附调用栈（栈底 8 帧）便于定位放行轻量消息的入口。
            if isinstance(msg.get(OFFLOAD_IDX_FIELD), int):
                import traceback

                frames = traceback.extract_stack()[-8:-1]
                stack_str = " <- ".join(
                    f"{fr.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{fr.lineno}:{fr.name}" for fr in frames
                )
                logger.debug(
                    f"[SessionRepository] 轻量剥离消息(role={msg.get('role')}, idx={i}) "
                    f"无剥离字段进入保存链（backfill 未命中，保留原样） | stack: {stack_str}"
                )
            light.append(msg)
            continue
        m2 = dict(msg)
        for f in patch:
            m2.pop(f, None)
        m2[OFFLOAD_IDX_FIELD] = i
        extras[i] = {f: serialize(v) for f, v in patch.items()}
        light.append(m2)
    return extras, light


def _backfill_offload_light_messages_impl(messages: List[Any], session_id: str, repo) -> Tuple[List[Any], bool]:
    """[T2f 修复 c] 轻量剥离消息自愈回填（save 链入口前置调用）。

    带 ``_x_idx`` 哨兵且无剥离字段的消息 → 按哨兵 idx 从 session_msg_extras
    IN 查询回填副本字段（仅补齐消息上缺失/falsy 的字段，不覆盖已有真值）。

    - 全部补齐 → INFO（自愈成功）
    - 查无 extras 行 → DEBUG 保留原样（可能真是无剥离字段的轻量消息）
    - 查询异常/读失败（None）→ WARNING 保留原样（不阻塞保存）

    Args:
        messages: 待保存消息列表（就地修改）
        session_id: 会话 ID（extras 查询键）
        repo: SessionRepository 实例（复用其 load_extras_for_session）

    Returns:
        ``(messages, load_failed)``。load_failed=True 表示 extras 读取失败
        （异常或 load_extras_for_session 返回 None）——调用方**必须**跳过
        ``_write_extras``，否则空 extras 会全删该会话已落库的剥离字段。
    """
    light_pairs: list[tuple[int, Dict[str, Any]]] = []
    for i, msg in enumerate(messages):
        if (
            isinstance(msg, dict)
            and isinstance(msg.get(OFFLOAD_IDX_FIELD), int)
            and not any(msg.get(f) for f in OFFLOAD_FIELDS)
        ):
            light_pairs.append((i, msg))
    if not light_pairs:
        return messages, False
    idxs = [i for i, _ in light_pairs]
    try:
        extras = repo.load_extras_for_session(session_id, idxs)
    except Exception as e:  # noqa: BLE001 — 查询失败不阻塞保存
        logger.warning(f"[SessionRepository] backfill extras 查询失败（保留原样，本轮不写 extras）: {e}")
        return messages, True
    if extras is None:
        # load_extras_for_session 用 None 表示读失败（{} 表示读成功但无行）。
        # 此处折叠成 {} 会让 save 链把「读失败」当成「无字段」→ 全删旧 extras。
        logger.warning(
            f"[SessionRepository] backfill extras 读失败（None，保留原样且本轮不写 extras）"
            f"（session={session_id[:8]}, idxs={idxs[:8]}）"
        )
        return messages, True
    repaired = 0
    missing_rows: list[int] = []
    for i, msg in light_pairs:
        patch = extras.get(i)
        if not patch:
            missing_rows.append(i)
            continue
        for f, v in patch.items():
            if not msg.get(f):
                msg[f] = v
        repaired += 1
    if missing_rows:
        logger.debug(
            f"[SessionRepository] backfill: {len(missing_rows)} 条查无 extras 行（idx={missing_rows[:8]}），保留原样"
        )
    if repaired:
        logger.info(
            f"[SessionRepository] backfill 自愈: {repaired} 条轻量剥离消息已回填"
            f"（session={session_id[:8]}, idxs={idxs[:8]}）"
        )
    return messages, False


def extract_first_user_question(messages: Optional[List]) -> Tuple[str, str]:
    """从消息列表中提取首条「真实用户提问」（团队首问语义）。

    筛选规则与 HistoryManager.get_team_first_question 严格一致：
    - 跳过带 _hook_event 的系统消息
    - 跳过 role != user
    - content 为 list 时用 content_to_text 转换
    - 跳过空 content
    - 跳过 "📨 **来自" 开头的任务邮件注入（兼容无 _hook_event 标记的旧数据）

    Args:
        messages: 会话消息列表

    Returns:
        (timestamp, content)；无有效提问时返回 ("", "")。
        content 已按 FIRST_USER_MSG_STORE_LIMIT 截断。
    """
    if not messages:
        return "", ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("_hook_event"):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            from app.core.conversation.message_content import content_to_text

            content = content_to_text(content)
        if not content:
            continue
        content = str(content)
        if content.startswith("📨 **来自"):
            continue
        return str(msg.get("timestamp") or ""), content[:FIRST_USER_MSG_STORE_LIMIT]
    return "", ""


class SessionRepository:
    """会话数据仓储，处理会话的 CRUD 操作"""

    TABLE_NAME = "sessions"
    DB_FILENAME = "sessions.db"

    def __init__(self, db_manager):
        """
        Args:
            db_manager: DatabaseManager 实例
        """
        self._db = db_manager
        # 内容地址缓存：session_id -> (message_count, last_msg_hash)
        # 用于跳过内容未变的重复持久化，节省全量序列化+zstd压缩开销
        self._content_hash_cache: Dict[str, tuple] = {}
        # [E10] extras 行集基线缓存：session_id -> {(msg_idx, field): value_bytes}
        # _write_extras 差异 upsert 的比对基线（流式拍 extras 集合不变 → 0 写 SQL）。
        # 缓存缺失（重启首拍/写入失败）→ 退化为旧全删全插并重建基线；delete 清理。
        # DB extras 仅本仓储写（唯一 INSERT 点），单实例下行集缓存与 DB 一致。
        self._extras_written: Dict[str, Dict[tuple, bytes]] = {}
        # 保护 _content_hash_cache 的读写原子性。
        # Gateway（异步写入）+ UI（同步 save）跨线程访问同一缓存时，
        # 裸 dict 的复合判断+赋值在 GIL 释放窗口内可能丢失更新或脏命中，
        # 导致两个线程都走昂贵的 serialize+INSERT OR REPLACE 路径。
        # RLock 而非 Lock：get() 可能在持锁状态下再次访问缓存。
        self._cache_lock = RLock()

    @property
    def is_initialized(self) -> bool:
        return self._db is not None and self._db.is_connected

    def _execute(self, sql: str, params: tuple = ()) -> Tuple[bool, Any]:
        """执行 SQL（内部使用）"""
        if not self._db:
            return False, "数据库未初始化"
        return self._db.execute_sql(sql, params)

    def _row_to_session(self, row) -> Dict:
        """将数据库行转换为会话字典"""
        if not row:
            return {}

        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return {}

        # 解析 JSON 字段（使用 serde 自动处理 zstd 压缩 + 旧数据兼容）
        messages = []
        compaction_state = {}
        compaction_cache = {}

        try:
            msg_raw = d.get("messages")
            if isinstance(msg_raw, (str, bytes)):
                messages = deserialize(msg_raw) or []
            elif isinstance(msg_raw, list):
                messages = msg_raw
        except Exception as e:
            logger.warning(f"Failed to deserialize session messages: {e}")

        try:
            state_raw = d.get("compaction_state")
            if isinstance(state_raw, (str, bytes)):
                compaction_state = deserialize(state_raw) or {}
            elif isinstance(state_raw, dict):
                compaction_state = state_raw
        except Exception as e:
            logger.warning(f"Failed to deserialize compaction_state: {e}")

        try:
            cache_raw = d.get("compaction_cache")
            if isinstance(cache_raw, (str, bytes)):
                compaction_cache = deserialize(cache_raw) or {}
            elif isinstance(cache_raw, dict):
                compaction_cache = cache_raw
        except Exception as e:
            logger.warning(f"Failed to deserialize compaction_cache: {e}")

        # 统一字段名：DB 的 title 列映射到 name 和 topic_summary
        raw_title = d.get("title", "") or ""
        return {
            "session_id": d.get("session_id", ""),
            "name": raw_title,  # ChatSession.name
            "title": raw_title,  # HistoryManager 兼容
            "topic_summary": raw_title,  # ChatSession.topic_summary
            "project": d.get("project", "默认项目"),
            "messages": messages,
            "system_prompt": d.get("system_prompt", ""),
            "compaction_state": compaction_state,
            "compaction_cache": compaction_cache,
            "message_count": d.get("message_count", 0),
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
            "worktree_path": d.get("worktree_path", "") or "",
            "context_usage": d.get("context_usage", 0),
            "last_api_prompt_tokens": d.get("last_api_prompt_tokens", 0),
            "last_api_message_count": d.get("last_api_message_count", 0),
            # 三态：1 / 0 / None（老行无此列 → None = 未知，按"来自 API"处理）
            "last_api_prompt_from_usage": d.get("last_api_prompt_from_usage", None),
            "token_calibration": d.get("token_calibration", 0),
            "token_calibration_model": d.get("token_calibration_model", "") or "",
            # 团队元数据（方案 A 团队会话恢复基础，非团队会话为空串）
            "team_run_id": d.get("team_run_id", "") or "",
            "team_name": d.get("team_name", "") or "",
            "agent_name": d.get("agent_name", "") or "",
            # 团队成员快照（F3：JSON 字符串，恢复时找回无会话记录的手动成员）
            "team_members": d.get("team_members", "") or "",
            # 会话置顶标记（历史面板置顶分组；默认 False）
            "pinned": bool(d.get("pinned", 0)),
            # 添加兼容字段（HistoryManager 期望这些字段）
            # 优先使用消息列表中最后一条消息的时间
            "last_time": d.get("last_time")
            or (messages[-1].get("timestamp") if messages else None)
            or d.get("updated_at", ""),
            "saved_at": d.get("saved_at") or d.get("created_at", ""),
            "user_edited_title": d.get("user_edited_title", False),
        }

    def save(self, session: Dict) -> bool:
        """
        原子性保存单个会话（带内容去重，内容未变时跳过序列化和写盘）

        Args:
            session: 会话数据字典

        Returns:
            bool: 保存是否成功
        """
        if not self.is_initialized:
            logger.warning("[SessionRepository] 未初始化，无法保存")
            return False

        session_id = session.get("session_id")
        if not session_id:
            logger.warning("[SessionRepository] session_id 不能为空")
            return False

        # 增量内容指纹：消息流式过程中只追加/修改最后一条消息，
        # 用 (len, 末条轻量指纹) 二元组替代 hash(str(全量)) 避免 O(n) 字符串化。
        # 对于"修改历史消息"的非常规场景（如 compaction），后续 serialize 会覆盖写入。
        # 🛡️ R5（T3）：元组追加 team_members 指纹——消息未变但快照变了
        # （如 join 新成员后 _get_team_members_snapshot_json 内容更新）时
        # hash 不同不跳过，保证新快照落库（否则恢复仍丢新成员）。
        # [PERF] 末条指纹走 _light_msg_fingerprint：原 `hash(str(last_msg))` 在
        # 末条含大 tool_result/base64 时每次保存都全量复制 1MB 级文本。轻量版
        # 只取字段数 + 每字段递归内容哈希（字符串取长度 + 首尾 2KB）。
        # 极端结构（depth>6 / nodes>2000）降级为长度 + 头/中/尾各 1KB 采样，
        # 仅采样间隙内变化可能碰撞 → 本次写盘被跳过；
        # 后续变化会整篇补写，但崩溃窗口内的变更会丢（详见函数 docstring）。
        messages = session.get("messages", [])
        if messages:
            last_msg = messages[-1]
            content_key = (len(messages), _light_msg_fingerprint(last_msg))
        else:
            content_key = (0, 0)
        content_key = content_key + (hash(session.get("team_members") or ""),)
        # 持锁读 cache：判断是否可跳过序列化+写盘。
        # 锁粒度最小（仅 cache 读 / 写），不阻塞后续长时间的 serialize+SQL。
        with self._cache_lock:
            cached = self._content_hash_cache.get(session_id)
            if cached is not None and cached == content_key:
                return True  # 消息未变，跳过昂贵的序列化+压缩+写盘

        # message_extras：提取剥离字段（不就地修改 session.messages）
        # [T2f 修复 c] 前置自愈：轻量剥离消息（带 _x_idx 无字段）先回填 extras
        backfill_extras_failed = False
        try:
            messages, backfill_extras_failed = _backfill_offload_light_messages_impl(messages, session_id, self)
        except Exception as e:
            logger.warning(f"[SessionRepository] backfill 自愈失败，按原消息继续保存: {e}")
            # 自愈异常时 extras 未知，跳过本轮 _write_extras（全删全插会丢字段）
            backfill_extras_failed = True
        # [方案 1] vision base64 落盘：data:image 块 → image_ref 文件引用
        try:
            from app.utils.app_paths import get_app_data_dir

            messages = _persist_vision_image_blocks_impl(messages, session_id, get_app_data_dir())
        except Exception as e:
            logger.warning(f"[SessionRepository] vision 落盘失败，按原消息继续保存: {e}")
        # [E12] 方案 α：流式拍跳过剥离，blob 直接存全量消息。
        # 声明通道：尾条 _partial（main_widget._maybe_flush_partial_reply 每 25s
        # 维护的流式残缺回复标记，落定拍该标记消失）——调用方已声明的流式拍，
        # 无需新增签名参数。窗口判定：len 不减且增量 ≤ KEEP_RECENT_ROUNDS
        # （新增全在保活窗内 → 窗外消息集合结构性不变 → extras 差异必为空），
        # 复用 _content_hash_cache 的 len 分量，零新增结构。基线缺失/get 失效/
        # 超窗/截断一律走全链（保守正确）。blob 全量使崩溃窗口内（一个流式拍
        # 周期 25s）强退恢复零丢失成为结构保证；落定拍走完整链恢复剥离。
        skip_offload = False
        _tail = messages[-1] if messages else None
        if isinstance(_tail, dict) and _tail.get("_partial"):
            with self._cache_lock:
                _cached_key = self._content_hash_cache.get(session_id)
            if _cached_key is not None:
                _prev_len = _cached_key[0]
                if len(messages) >= _prev_len and len(messages) - _prev_len <= KEEP_RECENT_ROUNDS:
                    skip_offload = True
                    logger.debug(f"[SessionRepository] 流式拍跳过剥离（blob 全量，session={session_id[:8]}）")
        if skip_offload:
            extras, light_messages = {}, messages
        else:
            try:
                extras, light_messages = extract_offload_fields(messages)
            except Exception as e:
                logger.warning(f"[SessionRepository] extract_offload_fields 失败，按全量保存: {e}")
                extras, light_messages = {}, messages

        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            user_edited = 1 if session.get("user_edited_title", False) else 0
            session_data = {
                "session_id": session_id,
                # 优先使用 topic_summary（UI/Agent生成），其次 name（Gateway创建），兜底空字符串
                "title": session.get("topic_summary") or session.get("name") or session.get("title", ""),
                "project": session.get("project", "默认项目"),
                # 使用 serde 透明压缩（zstd + 格式魔数），DB 体积减少 50-80%
                "messages": serialize(light_messages),
                "system_prompt": session.get("system_prompt", ""),
                "compaction_state": serialize(session.get("compaction_state", {})),
                "compaction_cache": serialize(session.get("compaction_cache", {})),
                "message_count": session.get("message_count", 0),
                "user_edited_title": user_edited,
                "worktree_path": session.get("worktree_path", "") or "",
                "preview": session.get("preview", "") or "",
                "context_usage": session.get("context_usage", 0),
                "last_api_prompt_tokens": session.get("last_api_prompt_tokens", 0),
                "last_api_message_count": session.get("last_api_message_count", 0),
                # None → NULL（未知），绝不能落成 0（0 = "本地估算"，会丢弃权威值）
                "last_api_prompt_from_usage": session.get("last_api_prompt_from_usage", None),
                "token_calibration": session.get("token_calibration", 0),
                "token_calibration_model": session.get("token_calibration_model", "") or "",
                # 团队元数据透传（方案 A）：非团队会话保持空串
                "team_run_id": session.get("team_run_id", "") or "",
                "team_name": session.get("team_name", "") or "",
                "agent_name": session.get("agent_name", "") or "",
                # 团队成员快照透传（F3）：JSON 字符串，非团队会话保持空串
                "team_members": session.get("team_members", "") or "",
                # 会话置顶标记透传（历史面板置顶分组）
                "pinned": 1 if session.get("pinned", False) else 0,
            }
            # 首问落库（T4 内存治理）：随保存增量写入，使团队合并条目的预览
            # 查询无需反序列化完整 messages（旧路径 246 条约 285MB 常驻）。
            session_data["first_user_ts"], session_data["first_user_msg"] = extract_first_user_question(messages)

            success, result = self._execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_NAME}
                (session_id, title, project, messages, system_prompt,
                 compaction_state, compaction_cache, message_count, user_edited_title,
                 worktree_path, preview, context_usage,
                 last_api_prompt_tokens, last_api_message_count, last_api_prompt_from_usage,
                 token_calibration, token_calibration_model,
                 team_run_id, team_name, agent_name, team_members,
                 pinned,
                 first_user_msg, first_user_ts,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?,
                    ?, ?,
                    COALESCE((SELECT created_at FROM {self.TABLE_NAME} WHERE session_id = ?), ?),
                    ?)
            """,
                (
                    session_data["session_id"],
                    session_data["title"],
                    session_data["project"],
                    session_data["messages"],
                    session_data["system_prompt"],
                    session_data["compaction_state"],
                    session_data["compaction_cache"],
                    session_data["message_count"],
                    session_data["user_edited_title"],
                    session_data["worktree_path"],
                    session_data["preview"],
                    session_data["context_usage"],
                    session_data["last_api_prompt_tokens"],
                    session_data["last_api_message_count"],
                    _to_sql_bool_or_null(session_data.get("last_api_prompt_from_usage")),
                    session_data["token_calibration"],
                    session_data["token_calibration_model"],
                    session_data["team_run_id"],
                    session_data["team_name"],
                    session_data["agent_name"],
                    session_data["team_members"],
                    session_data["pinned"],
                    session_data["first_user_msg"],
                    session_data["first_user_ts"],
                    session_id,  # for coalesce
                    now,  # created_at default
                    now,  # updated_at
                ),
            )

            if success:
                # 更新内容 hash 缓存（持锁写）
                with self._cache_lock:
                    self._content_hash_cache[session_id] = content_key
                # INSERT OR REPLACE = DELETE 旧行 + INSERT 新行，旧页全部进 freelist
                # 高频率 save（每次用户发消息+Agent 回复）叠加 zstd 压缩后 blob
                # 大小波动（50KB~500KB），持续制造不可复用的空闲页。
                # 此处检测 freelist 是否超过安全阈值（5000 页 ≈ 20MB），超过则
                # 增量回收 500 页（≈2MB），防止 freelist 滚雪球到 GB 级。
                self._reclaim_freelist_if_needed()
                # message_extras：全删全插。compaction 重写/截断导致的索引漂移
                # 由此天然覆盖（每次 save 后 extras 与主 blob 严格一致）。
                # 🛡️ 独立隔离：extras 写入失败只记日志，不得让异常冒泡到外层
                # try（主 blob 已落库，误报 False 会误导调用方重试/报错）。
                # 🛡️ backfill 读失败（load_failed）时跳过：此时 extras 为空不代表
                # 该会话真无字段，全删全插会把已落库的剥离字段一并抹掉。
                if backfill_extras_failed:
                    logger.warning(
                        f"[SessionRepository] backfill 读失败，跳过本轮 extras 写入"
                        f"（保留库内旧值，session={session_id[:8]}）"
                    )
                elif skip_offload:
                    # [E12] 流式拍：blob 已全量自含字段，extras 表与本会话基线不动
                    pass
                else:
                    try:
                        self._write_extras(session_id, extras)
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"[SessionRepository] write_extras 调用异常（不影响主保存）: {e}")

            return success

        except Exception as e:
            logger.error(f"[SessionRepository] save_session 异常: {e}")
            # 失败时不更新 cache，下次 save 会重新尝试序列化+写盘
            return False

    def _reclaim_freelist_if_needed(self, threshold_pages: int = 5000, reclaim_pages: int = 500):
        """
        空闲页超过阈值时增量回收，防止 freelist 滚雪球

        INSERT OR REPLACE 的 DELETE→INSERT 路径将旧 blob 页全部释放进
        freelist，高频率 save（每次对话回合 2 次）叠加 zstd 压缩后大小波动
        （50KB~500KB）持续制造不可复用空闲页。用户截断/子任务清理等大型
        DELETE 操作一次性释放数千页，必须及时回收。

        每次最多回收 reclaim_pages 页（≈2MB @ 4KB page）。
        incremental_vacuum 仅在 auto_vacuum=INCREMENTAL 时实际回收，
        否则静默跳过。

        Args:
            threshold_pages: freelist 超过此页数才触发回收（默认 5000 页 ≈ 20MB）
            reclaim_pages:  单次回收最多页数（默认 500 页 ≈ 2MB）
        """
        # 优化点：原行为=save 主路径同步 PRAGMA freelist_count，超阈值再同步
        # incremental_vacuum（needs_clean_tx 先强制同步 flush 全部挂起写再全库
        # 整理）——每次保存都付一次读锁查询，超阈值时保存直接被 vacuum 卡住；
        # 新行为=主路径只登记 pending + 起 daemon 后台线程，攒批窗口落地后再
        # 检查/回收（窗口常量从 db_manager 动态读取，不硬编码），保存热路径零
        # SQL 零锁；根因=vacuum 全库整理属重活，不该卡在每次保存的热路径上。
        # 回收机会不静默丢失：pending 标志 + worker 收尾复查（覆盖执行期间
        # 的新 save）；进程退出丢的那次由退出链 mark_clean_shutdown 全量回收
        # 兜底，且下一次 save 会重新触发。回收只整理空闲页，数据读回内容不变。
        try:
            self._reclaim_pending = True
            t = getattr(self, "_reclaim_thread", None)
            if t is not None and t.is_alive():
                return
            t = Thread(
                target=self._reclaim_freelist_worker,
                args=(threshold_pages, reclaim_pages),
                daemon=True,
                name="session-repo-reclaim-freelist",
            )
            self._reclaim_thread = t
            t.start()
        except Exception:
            pass  # 调度失败不阻塞保存流程（与原静默跳过语义一致）

    def _reclaim_freelist_worker(self, threshold_pages: int, reclaim_pages: int) -> None:
        """后台攒批窗口侧的回收执行体（含「机会不丢失」的收尾复查循环）。

        竞态说明：双 save 同时看到无存活线程会起双 worker——vacuum 幂等无害，
        最坏多做一次检查；pending 由两 worker 交替消费，机会仍被覆盖。
        """
        try:
            import time

            # 等当前攒批窗口落地再查：避免 vacuum 触发 needs_clean_tx 把窗口内
            # 挂起写提前 flush（破坏攒批合并收益）。窗口常量动态读取不硬编码。
            delay = float(getattr(type(self._db), "COMMIT_DELAY_SECONDS", 0.5)) + 0.1
            while True:
                self._reclaim_pending = False
                time.sleep(delay)
                try:
                    ok, rows = self._execute("PRAGMA freelist_count")
                    if not ok or not rows:
                        return
                    freelist = list(rows[0].values())[0] if isinstance(rows[0], dict) else rows[0]
                    if freelist >= threshold_pages:
                        self._execute(f"PRAGMA incremental_vacuum({reclaim_pages})")
                except Exception:
                    return  # auto_vacuum 未启用等异常静默跳过，不重试（与原语义一致）
                if not self._reclaim_pending:
                    return  # 执行期间无新 save，本轮机会已覆盖
        except Exception:
            pass


    def _write_extras(self, session_id: str, extras: Dict[int, Dict[str, bytes]]) -> None:
        """把本次 extract 的剥离字段同步进 DB（差异 upsert；失败不阻塞主保存）。

        [E10] 旧实现全删全插（DELETE 全会话行 + executemany 重插），流式拍
        extras 集合不变也要付 104ms@16K（E3 曲线 59-65%）。现按
        (session_id, msg_idx, field) 行集与基线缓存 diff：
        - 新增/变更行 → executemany INSERT OR REPLACE（len 快筛 + bytes 逐字节比对）
        - 消失行（截断/清空）→ executemany 精确 DELETE，idx 复用不串字段
        - 无变化拍 → 0 写 SQL（流式拍主收益）
        - 基线缓存缺失（重启首拍/历史写入失败）→ 退化为旧全删全插并重建基线，
          保证与旧行为严格等价；compaction 索引漂移由「同 idx 比值不等 → 覆盖、
          旧行消失 → 删除」天然覆盖（与全删全插最终状态一致，回归锁定）。
        语义契约不变：调用后 DB extras ≡ 本次 extract 产物（旧字段整体消失）。
        [E16 C1] 并发防护：基线「读 cached → diff → SQL → 写回 new_rows」必须
        原子——A 删行后 B 按旧基线 diff 会产生基线与 DB 失配（后续 diff 永不
        重插 → 静默丢行）。持 _cache_lock（与 _content_hash_cache 同锁，RLock
        可重入覆盖 save 主路径）。嵌套顺序核查：本锁内 SQL 经 db._write_lock，
        方向恒为 cache_lock → db_write_lock（db_manager 不持有 repo 引用，
        无反向路径），全局单向无死锁；锁内 IO 最坏为全删全插量级（104ms@16K），
        与写库本就串行于 _write_lock 的现状一致。
        """
        try:
            with self._cache_lock:
                self._write_extras_locked(session_id, extras)
        except Exception as e:
            logger.error(f"[SessionRepository] write_extras 异常: {e}")

    def _write_extras_locked(self, session_id: str, extras: Dict[int, Dict[str, bytes]]) -> None:
        """C1 临界区体（持 _cache_lock 调用）：基线 diff 与 SQL 串行化。"""
        try:
            new_rows: Dict[tuple, bytes] = {
                (i, f): v for i, fields in extras.items() for f, v in fields.items()
            }
            upsert_sql = (
                "INSERT OR REPLACE INTO session_msg_extras (session_id, msg_idx, field, value) "
                "VALUES (?, ?, ?, ?)"
            )
            delete_sql = "DELETE FROM session_msg_extras WHERE session_id = ? AND msg_idx = ? AND field = ?"
            cached = self._extras_written.get(session_id)
            if cached is None:
                # 未知基线：旧全删全插（读回断言/既有门禁依赖的等价路径）
                self._execute("DELETE FROM session_msg_extras WHERE session_id = ?", (session_id,))
                if not new_rows:
                    self._extras_written[session_id] = {}
                    return
                rows = [(session_id, i, f, v) for (i, f), v in new_rows.items()]
                if self._executemany_extras(upsert_sql, rows):
                    self._extras_written[session_id] = new_rows
                else:
                    self._extras_written.pop(session_id, None)  # 全失败 → 下拍重新校准
                return
            # 已知基线：差异 upsert
            to_upsert = [(session_id, i, f, v) for (i, f), v in new_rows.items() if cached.get((i, f)) != v]
            to_delete = [(session_id, i, f) for (i, f) in cached if (i, f) not in new_rows]
            if not to_upsert and not to_delete:
                return  # 无变化拍：0 写 SQL
            if len(to_delete) > 512:
                # 大规模消失（清空/大截断）：逐行 DELETE 不如一条全删 + 全量重插
                # （最终状态等价、SQL 数最少；512 阈值≈逐行删开始劣化的量级）
                self._execute("DELETE FROM session_msg_extras WHERE session_id = ?", (session_id,))
                to_upsert = [(session_id, i, f, v) for (i, f), v in new_rows.items()]
                to_delete = []
            ok = True
            if to_delete:
                ok = self._executemany_extras(delete_sql, to_delete)
            if ok and to_upsert:
                ok = self._executemany_extras(upsert_sql, to_upsert)
            if ok:
                self._extras_written[session_id] = new_rows
            else:
                # 降级也失败：基线不可信，清缓存 → 下拍退化为全删全插校准
                self._extras_written.pop(session_id, None)
        except Exception as e:
            logger.error(f"[SessionRepository] write_extras 异常: {e}")

    def _executemany_extras(self, sql: str, rows: list) -> bool:
        """批量下发 extras SQL（SAVEPOINT 包裹，失败降级逐条）。

        Returns:
            True=批量或降级全部成功；False=彻底失败（调用方清基线缓存）。
        """
        db = self._db
        conn = getattr(db, "_conn", None)
        if conn is not None:
            try:
                with db._write_lock:
                    cursor = conn.cursor()
                    cursor.execute("SAVEPOINT sp_exec")
                    try:
                        cursor.executemany(sql, rows)
                    except Exception:
                        cursor.execute("ROLLBACK TO sp_exec")
                        cursor.execute("RELEASE sp_exec")
                    else:
                        cursor.execute("RELEASE sp_exec")
                        db._schedule_commit()
                        return True
            except Exception:
                pass  # 落到降级逐条
        for row in rows:
            try:
                self._execute(sql, row)
            except Exception:  # noqa: BLE001 — 单条失败静默跳过（既有语义）
                return False
        db._schedule_commit()
        return True

    def load_extras_for_session(
        self, session_id: str, idxs: Optional[List[int]] = None
    ) -> Optional[Dict[int, Dict[str, Any]]]:
        """读取剥离的 UI 态字段（message_extras）。

        Args:
            session_id: 会话 ID
            idxs: 消息绝对索引列表；None 读全部

        Returns:
            {msg_idx: {field: 反序列化后的值}}；「无 extras」返回 {}；
            **读失败返回 None**（与空结果可区分，消费方按 falsy 零改动即可）

        契约：
        - idxs 规模由调用方保证（批次级，≤数百）；不做截断，静默截断反而丢数据
        - None=读失败（DB 异常/未初始化）；{}=读成功但该会话无 extras
        """
        if not self.is_initialized or not session_id:
            return None
        try:
            if idxs:
                placeholders = ",".join("?" * len(idxs))
                sql = (
                    "SELECT msg_idx, field, value FROM session_msg_extras "
                    f"WHERE session_id = ? AND msg_idx IN ({placeholders})"
                )
                ok, rows = self._execute(sql, (session_id, *idxs))
            else:
                ok, rows = self._execute(
                    "SELECT msg_idx, field, value FROM session_msg_extras WHERE session_id = ?",
                    (session_id,),
                )
            result: Dict[int, Dict[str, Any]] = {}
            if ok:
                for row in rows or []:
                    mi = row["msg_idx"] if hasattr(row, "keys") else row[0]
                    f = row["field"] if hasattr(row, "keys") else row[1]
                    v = row["value"] if hasattr(row, "keys") else row[2]
                    result.setdefault(int(mi), {})[str(f)] = deserialize(v)
            return result
        except Exception as e:
            logger.error(f"[SessionRepository] load_extras 异常: {e}")
            return None

    def get_full_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """主 blob + extras 合并的全量消息（导出 / agent_trace 深读用）。

        get() 每次返回新反序列化的消息列表，就地合并无副作用。
        extras 读失败（None）→ WARNING + 返回轻量消息（降级不炸）。
        """
        sess = self.get(session_id)
        if not sess:
            return []
        msgs = sess.get("messages", [])
        # [T3] 旧 blob 防御迁移：persist 链引入前的存量 / persist 失败降级残留
        # 仍内嵌 data:image，后台线程堵洞（快筛不命中零开销）
        self._schedule_legacy_image_blob_migration(session_id, msgs)
        extras = self.load_extras_for_session(session_id)
        if extras is None:
            logger.warning(f"[SessionRepository] get_full_messages: {session_id} extras 读取失败，降级返回轻量消息")
            return msgs
        return merge_extras_into(msgs, extras)

    def get_messages_with_lazy_extras(
        self, session_id: str, first_idxs: List[int]
    ) -> Tuple[List[Dict[str, Any]], List[int]]:
        """[E14] 打开链专用懒 merge：blob 读 + 首屏 idxs 的 extras 合并。

        与 get_full_messages（全量语义，导出/深读专用）的差异：extras 只载
        ``first_idxs`` 里「带 _x_idx 哨兵的轻量消息」（缺字段者），其余 idx
        作为 pending 返回，由调用方后台分批补载（load_extras_for_session +
        merge_extras_into，幂等）。E12 方案 α 的全量 blob 无哨兵 → 零查询零
        pending，天然共存。

        Returns:
            (messages, pending_idxs)；会话不存在 → ([], [])。
            pending 为「尚未补载 extras 的消息下标」升序表。
        """
        sess = self.get(session_id)
        if not sess:
            return [], []
        msgs = sess.get("messages", [])
        self._schedule_legacy_image_blob_migration(session_id, msgs)
        first_set = set(first_idxs or [])
        need = [
            i
            for i in first_set
            if 0 <= i < len(msgs) and isinstance(msgs[i], dict) and msgs[i].get(OFFLOAD_IDX_FIELD) is not None
        ]
        extras = self.load_extras_for_session(session_id, need) if need else {}
        if extras:
            merge_extras_into(msgs, extras)
        pending = [
            i
            for i, m in enumerate(msgs)
            if isinstance(m, dict) and m.get(OFFLOAD_IDX_FIELD) is not None and i not in first_set
        ]
        return msgs, pending

    def _schedule_legacy_image_blob_migration(self, session_id: str, msgs: List[Any]) -> None:
        """读侧防御迁移：blob 内嵌 data:image → 后台落盘图并回写 ref 形态 blob。

        persist 链引入前的存量 blob、persist 单块失败降级残留，在每次全量读时
        触发一次后台迁移（复用 _persist_vision_image_blocks_impl 幂等链：sha1
        去重 + 原子写，已落盘则跳过）。

        - 快筛：块级扫描（与 persist 链命中条件一致）不命中直接返回（零开销）
        - 传 blob 快照而非活对象：调用方主线程随后 merge_extras_into 就地改 msgs
        - CAS 回写：UPDATE 带 WHERE messages = 旧快照，blob 已被 worker save 覆盖
          即放弃本轮（防旧快照回写吞新保存）；失败降级保留原 blob 下次重试；
          UPDATE 失败但图已落盘时 sha1 同名覆盖无害
        - 内存侧不就地换 ref：读路径原样返回，UI 链由既有出口 revoke 兜底
        """
        try:
            if not _has_inline_data_image_blocks(msgs):
                return
            # 快筛命中才 serialize：传 blob 快照而非活对象（调用方主线程随后
            # merge_extras_into 会就地改 msgs，线程内 deserialize 出独立对象）
            blob = serialize(msgs)
        except Exception:  # noqa: BLE001 — 快筛失败按无需迁移
            return

        def _migrate(blob: bytes = blob) -> None:
            try:
                from app.utils.app_paths import get_app_data_dir

                work = deserialize(blob)
                _persist_vision_image_blocks_impl(work, session_id, get_app_data_dir())
                new_blob = serialize(work)
                if new_blob == blob:
                    return  # 无 data:image 块（快筛误命中），避免无谓写
                # CAS：blob 已被 worker save 覆盖即放弃（防旧快照回写吞新保存），
                # 未命中即放弃本轮，下次读时快筛自然再触发
                self._execute(
                    f"UPDATE {self.TABLE_NAME} SET messages = ? "
                    f"WHERE session_id = ? AND messages = ?",
                    (new_blob, session_id, blob),
                )
            except Exception as e:  # noqa: BLE001 — 迁移失败保留原 blob 下次重试
                logger.warning(f"[SessionRepository] 旧图 blob 迁移失败 {session_id}: {e}")

        Thread(target=_migrate, daemon=True, name=f"migrate-img-blob-{session_id[:8]}").start()

    def get(self, session_id: str) -> Optional[Dict]:
        """根据 ID 获取单个会话（同时失效内容 hash 缓存）"""
        if not self.is_initialized:
            return None

        try:
            success, rows = self._execute(f"SELECT * FROM {self.TABLE_NAME} WHERE session_id = ?", (session_id,))
            if success and rows and len(rows) > 0:
                # 外部加载了最新数据，失效内容缓存，下次 save 会重建
                # [E10] extras 行集基线同步失效：外部读意味着可能存在绕过 repo 层的
                # DB 直改（老数据迁移/修复脚本），缓存与库可能失配 → 下拍 save 退化
                # 全删全插重新校准（一次性成本，与 hash 缓存同触发点同语义）。
                with self._cache_lock:
                    self._content_hash_cache.pop(session_id, None)
                    self._extras_written.pop(session_id, None)
                return self._row_to_session(rows[0])
            return None
        except Exception as e:
            logger.error(f"[SessionRepository] get_session 异常: {e}")
            return None

    def get_by_ids(self, session_ids: List[str]) -> Dict[str, Dict]:
        """按 ID 批量取回会话（session_id IN 一次查询，I6 引用会话 N+1 消除）。

        每个 get() 都是 SELECT * 全 blob + 解压 + 浓缩前置读，N 个引用会话
        串行执行时发送首字延迟线性叠加（session_reference 的 N+1 场景）。
        本方法一次 IN 查询取回全部；分批防 SQLite 变量数上限（999/批，
        老版 SQLITE_MAX_VARIABLE_NUMBER 固有行为）。

        与 get() 语义对齐：成功取回的 sid 同样失效内容 hash 缓存（外部
        加载最新数据，下次 save 重建）。

        Returns:
            {session_id: session dict}；查不到 / 反序列化失败的 id 不出现。
            未初始化 / SQL 失败返回 {}（调用方降级逐个读）。
        """
        ids = [sid for sid in (session_ids or []) if sid]
        if not ids or not self.is_initialized:
            return {}
        out: Dict[str, Dict] = {}
        try:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                success, rows = self._execute(
                    f"SELECT * FROM {self.TABLE_NAME} WHERE session_id IN ({placeholders})",
                    tuple(chunk),
                )
                if not success:
                    return {}
                for row in rows:
                    try:
                        session = self._row_to_session(row)
                    except Exception as e:  # noqa: BLE001 — 单行坏数据不拖垮整批
                        logger.warning(f"[SessionRepository] get_by_ids 行反序列化失败: {e}")
                        continue
                    if isinstance(session, dict):
                        sid = str(session.get("session_id", "") or "")
                        if sid:
                            out[sid] = session
        except Exception as e:
            logger.error(f"[SessionRepository] get_by_ids 异常: {e}")
            return {}
        if out:
            with self._cache_lock:
                for sid in out:
                    self._content_hash_cache.pop(sid, None)
                    self._extras_written.pop(sid, None)  # [E10] 同步失效（同 get 语义）
        return out

    def get_all(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """获取所有会话（按更新时间倒序）"""
        if not self.is_initialized:
            return []

        try:
            success, rows = self._execute(
                f"SELECT * FROM {self.TABLE_NAME} ORDER BY updated_at DESC LIMIT ? OFFSET ?", (limit, offset)
            )
            if success:
                return [self._row_to_session(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[SessionRepository] get_sessions 异常: {e}")
            return []

    def get_all_lightweight(self, limit: int = 100, offset: int = 0, project: Optional[str] = None) -> List[Dict]:
        """获取所有会话的轻量列表（不含 messages），用于启动加载和历史列表展示。

        避免 SELECT * 一次性反序列化全部 messages JSON（可达数十 MB），
        仅在用户点击某个会话时按需加载 messages。

        Args:
            project: 非空时只取该项目的会话（首屏快速路径用，走 SQL 过滤
                避免先取全局再在内存里筛）。

        Returns:
            成功返回轻量列表（空库 = []）；查询失败（未初始化 / SQL 失败 /
            异常）返回 None——调用方（HistoryManager 懒加载）据此保持未加载
            态重试。不得把失败伪装成空列表：启动瞬时 disk I/O error 曾因此
            把空列表固化成「已加载」，整个会话期历史列表为空。
        """
        if not self.is_initialized:
            return None

        try:
            # 🚀 只选轻量列表展示所需的字段，跳过 compaction_state/cache
            # 等重量级 BLOB 列，减少 SQLite I/O 和传输开销。
            where_sql = ""
            params: list = []
            if project:
                where_sql = "WHERE project = ? "
                params.append(project)
            success, rows = self._execute(
                # 内存优化：不再 SELECT system_prompt。该列均值约 10KB/条，
                # 5000 条常驻内存约 50MB，而会话列表渲染完全用不到它。
                # 需要时通过 get_system_prompt(session_id) 单列回查。
                f"SELECT session_id, title, project, "
                f"message_count, user_edited_title, worktree_path, "
                f"preview, context_usage, created_at, updated_at, "
                f"last_api_prompt_tokens, last_api_message_count, last_api_prompt_from_usage, "
                f"token_calibration, token_calibration_model, "
                f"team_run_id, team_name, agent_name, team_members, "
                f"pinned "
                f"FROM {self.TABLE_NAME} {where_sql}ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            if success:
                return [self._row_to_session_lightweight(row) for row in rows]
            logger.warning(f"[SessionRepository] get_all_lightweight SQL 失败: {rows}")
            return None
        except Exception as e:
            logger.error(f"[SessionRepository] get_all_lightweight 异常: {e}")
            return None

    def get_system_prompt(self, session_id: str) -> str:
        """单列取 system_prompt（轻量列表不再 SELECT 该列后的按需回查入口）。

        Returns:
            该会话的 system_prompt；会话不存在或查询失败返回空串。
        """
        if not self.is_initialized or not session_id:
            return ""
        try:
            success, rows = self._execute(
                f"SELECT system_prompt FROM {self.TABLE_NAME} WHERE session_id = ?",
                (session_id,),
            )
            if success and rows:
                row = rows[0]
                val = row["system_prompt"] if hasattr(row, "keys") else row[0]
                return val or ""
        except Exception as e:
            logger.error(f"[SessionRepository] get_system_prompt 异常: {e}")
        return ""

    def _row_to_session_lightweight(self, row) -> Dict:
        """将数据库行转换为不含 messages 的轻量会话字典"""
        if not row:
            return {}

        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return {}

        # 🚀 性能优化：跳过 compaction_state/cache 的 zstd 解压+反序列化。
        # 轻量列表展示中不使用这两个字段，全量加载在 get_session() 中才做。
        # 省掉 500 条记录的 zstd 解压 + orjson.loads，可节省大量 CPU。
        raw_title = d.get("title", "") or ""
        return {
            "session_id": d.get("session_id", ""),
            "name": raw_title,
            "title": raw_title,
            "topic_summary": raw_title,
            "project": d.get("project", "默认项目"),
            "messages": [],  # 懒加载：不在启动时加载
            # 未 SELECT 该列时为 None —— 哨兵表示"未加载"，区别于真实空串。
            # 消费方（HistoryManager）据此决定是否需要回查，不可当成空值写回。
            "system_prompt": d.get("system_prompt"),
            "compaction_state": {},
            "compaction_cache": {},
            "message_count": d.get("message_count", 0),
            "preview": d.get("preview", "") or "",  # 从 DB 读取预览文本
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
            "worktree_path": d.get("worktree_path", "") or "",
            "context_usage": d.get("context_usage", 0),
            # API 权威上下文占用（历史会话加载后据此显示，缺失则退回本地估算）
            "last_api_prompt_tokens": d.get("last_api_prompt_tokens", 0) or 0,
            "last_api_message_count": d.get("last_api_message_count", 0) or 0,
            # 未 SELECT / 老行 → None（未知），绝不落成 False（会丢弃权威值）
            "last_api_prompt_from_usage": d.get("last_api_prompt_from_usage", None),
            "token_calibration": d.get("token_calibration", 0) or 0,
            "token_calibration_model": d.get("token_calibration_model", "") or "",
            # 团队元数据（方案 A 团队会话恢复基础，非团队会话为空串）
            "team_run_id": d.get("team_run_id", "") or "",
            "team_name": d.get("team_name", "") or "",
            "agent_name": d.get("agent_name", "") or "",
            # 团队成员快照（F3：JSON 字符串，恢复时找回无会话记录的手动成员）
            "team_members": d.get("team_members", "") or "",
            "last_time": d.get("updated_at", ""),
            "saved_at": d.get("created_at", ""),
            "user_edited_title": d.get("user_edited_title", False),
            # 会话置顶标记：轻量加载必须带上，否则启动重建内存列表后置顶丢失，
            # 且后续 save_session 的「None→保留现值」读到的现值恒为 False（写库清零）
            "pinned": bool(d.get("pinned", 0)),
        }

    def get_by_project(self, project: str, limit: int = 100) -> List[Dict]:
        """获取指定项目的会话列表"""
        if not self.is_initialized:
            return []

        try:
            success, rows = self._execute(
                f"SELECT * FROM {self.TABLE_NAME} WHERE project = ? ORDER BY updated_at DESC LIMIT ?", (project, limit)
            )
            if success:
                return [self._row_to_session(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[SessionRepository] get_sessions_by_project 异常: {e}")
            return []

    def get_by_team_run_id(self, run_id: str) -> List[Dict]:
        """按团队 run_id 获取全部成员会话（轻量，不含 messages）。

        恢复团队会话时使用：直接查 SQLite，绕开 HistoryManager 内存
        _history_limit=500 的截断——团队会话可能因长期运行被挤出
        内存前 500 条，导致恢复时按 run_id 收集不到部分成员。

        Returns:
            按 updated_at 倒序的轻量会话字典列表（含团队元数据字段）
        """
        if not self.is_initialized or not run_id:
            return []

        try:
            # 与 get_all_lightweight 相同的轻量字段投影，避免反序列化重量级 BLOB
            success, rows = self._execute(
                f"SELECT session_id, title, project, system_prompt, "
                f"message_count, user_edited_title, worktree_path, "
                f"preview, context_usage, created_at, updated_at, "
                f"last_api_prompt_tokens, last_api_message_count, last_api_prompt_from_usage, "
                f"token_calibration, token_calibration_model, "
                f"team_run_id, team_name, agent_name, team_members, "
                f"pinned "
                f"FROM {self.TABLE_NAME} WHERE team_run_id = ? "
                f"ORDER BY updated_at DESC",
                (run_id,),
            )
            if success:
                return [self._row_to_session_lightweight(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[SessionRepository] get_by_team_run_id 异常: {e}")
            return []

    def get_team_first_question_candidates(self, run_id: str) -> List[Tuple[str, str]]:
        """取该 run 下所有会话已落库的首问候选（纯字符串，零反序列化）。

        团队合并条目的预览只需要「时间戳最早的那条 user 消息」，完全不必翻开
        messages BLOB。候选按 updated_at DESC 返回，与 get_by_team_run_id 的
        遍历顺序一致，保证旧实现「时间戳并列时保留首个」的语义可复现。

        Returns:
            [(timestamp, content), ...]；无候选时为空列表
        """
        if not self.is_initialized or not run_id:
            return []
        try:
            success, rows = self._execute(
                f"SELECT first_user_ts, first_user_msg FROM {self.TABLE_NAME} "
                f"WHERE team_run_id = ? AND first_user_msg IS NOT NULL AND first_user_msg != '' "
                f"ORDER BY updated_at DESC",
                (run_id,),
            )
            if not success:
                return []
            out: List[Tuple[str, str]] = []
            for row in rows or []:
                try:
                    if hasattr(row, "keys"):
                        ts, msg = row["first_user_ts"], row["first_user_msg"]
                    else:
                        ts, msg = row[0], row[1]
                except Exception:
                    continue
                out.append((str(ts or ""), str(msg or "")))
            return out
        except Exception as e:
            logger.error(f"[SessionRepository] get_team_first_question_candidates 异常: {e}")
            return []

    def get_projects(self) -> List[str]:
        """获取所有项目名称列表（含只有工作目录、尚无会话的项目）

        新建项目只写 project_workdir（工作目录），首轮对话后才落 sessions，
        故项目清单必须并入该表，否则「刚建好还没聊过」的项目不显示。
        """
        if not self.is_initialized:
            return ["默认项目"]

        try:
            success, rows = self._execute(
                f"""
                SELECT DISTINCT project FROM (
                    SELECT project FROM {self.TABLE_NAME}
                    UNION
                    SELECT project FROM project_workdir
                ) ORDER BY project
                """
            )
            if success and rows:
                projects = []
                for row in rows:
                    p = row[0] if isinstance(row, tuple) else row.get("project", "")
                    if p and not p.startswith("__archived__"):
                        projects.append(p)
                return projects if projects else ["默认项目"]
            return ["默认项目"]
        except Exception as e:
            logger.error(f"[SessionRepository] get_projects 异常: {e}")
            return ["默认项目"]

    def delete(self, session_id: str) -> bool:
        """删除指定会话（同时清除内容 hash 缓存）"""
        if not self.is_initialized:
            return False

        try:
            self._content_hash_cache.pop(session_id, None)
            self._extras_written.pop(session_id, None)  # [E10] 行集基线随会话删除清理
            success, _ = self._execute(f"DELETE FROM {self.TABLE_NAME} WHERE session_id = ?", (session_id,))
            if success:
                # message_extras 级联清理（后删子表）：主表失败则整体未删保持一致；
                # 主表成功而子表失败只剩无害孤儿行（会话已不存在，无人查询）。
                self._execute("DELETE FROM session_msg_extras WHERE session_id = ?", (session_id,))
            return success
        except Exception as e:
            logger.error(f"[SessionRepository] delete_session 异常: {e}")
            return False

    def update_title(self, session_id: str, title: str) -> bool:
        """更新会话标题"""
        if not self.is_initialized:
            return False

        try:
            success, _ = self._execute(
                f"UPDATE {self.TABLE_NAME} SET title = ?, updated_at = ? WHERE session_id = ?",
                (title, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_id),
            )
            return success
        except Exception as e:
            logger.error(f"[SessionRepository] update_title 异常: {e}")
            return False

    def update_project(self, session_id: str, project: str) -> bool:
        """更新会话的项目归属"""
        if not self.is_initialized:
            return False

        try:
            success, _ = self._execute(
                f"UPDATE {self.TABLE_NAME} SET project = ?, updated_at = ? WHERE session_id = ?",
                (project, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_id),
            )
            return success
        except Exception as e:
            logger.error(f"[SessionRepository] update_project 异常: {e}")
            return False

    def update_pinned(self, session_id: str, pinned: bool) -> bool:
        """更新会话置顶标记（只写 pinned 列，不动 messages blob）"""
        if not self.is_initialized:
            return False
        try:
            success, result = self._execute(
                f"UPDATE {self.TABLE_NAME} SET pinned = ? WHERE session_id = ?",
                (1 if pinned else 0, session_id),
            )
            # rowcount==0 说明 session_id 不存在（UPDATE 语法成功但不命中行）
            return bool(success) and int(result or 0) > 0
        except Exception as e:
            logger.error(f"[SessionRepository] update_pinned 异常: {e}")
            return False

    def archive_by_project(self, project: str) -> int:
        """归档指定项目的所有会话（单条 SQL，避免 N+1 查询）"""
        if not self.is_initialized:
            return 0

        try:
            archived_project = f"__archived__/{project}"
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            success, result = self._execute(
                f"UPDATE {self.TABLE_NAME} SET project = ?, updated_at = ? WHERE project = ?",
                (archived_project, now, project),
            )
            if success and result is not None:
                return int(result)
            return 0
        except Exception as e:
            logger.error(f"[SessionRepository] archive_sessions_by_project 异常: {e}")
            return 0

    def get_session_counts(self) -> Dict[str, int]:
        """获取所有项目（非归档）的会话数量（COUNT DISTINCT session_id 去重）"""
        if not self.is_initialized:
            return {}
        try:
            success, rows = self._execute(
                f"SELECT project, COUNT(DISTINCT session_id) as cnt FROM {self.TABLE_NAME} "
                f"WHERE project NOT LIKE '__archived__%' GROUP BY project"
            )
            if success and rows:
                result = {}
                for row in rows:
                    p = row[0] if isinstance(row, tuple) else row.get("project", "")
                    c = row[1] if isinstance(row, tuple) else row.get("cnt", 0)
                    result[p] = c
                return result
            return {}
        except Exception as e:
            logger.error(f"[SessionRepository] get_session_counts 异常: {e}")
            return {}
