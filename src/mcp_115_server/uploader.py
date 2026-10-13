# -*- coding: utf-8 -*-
"""115 网盘健壮上传核心（单文件上传 + 目录树构建 + 断点续传状态）。

本模块是从实战批量迁移项目（ARRI 素材 8.5 万文件 / 640 GB）中沉淀出来的，
集中了所有踩过的坑的修复：

1. 【超时】显式注入 urllib3 Timeout(connect, read)。
   p115client 底层的 ``urllib3_future_request.request`` **没有任何超时**，
   也不做重试。TCP 连接半死时，单个 PUT 会**永久挂起**。
2. 【看门狗】双指标（reporthook 进展 + 本机网卡上行）。
   单指标会在「大文件上传前的 SHA1 计算阶段」误判卡死
   （该阶段 reporthook 不回调，但网卡仍在动）。
3. 【分块】默认 8 MB。太大时单块失败重传代价高。
4. 【重试】默认 5 次 + 指数退避。
5. 【限流识别】115 开放平台接口限流时 HTTP 仍是 200，
   但 body 里 ``state=false, code=770004``。只看 ``data`` 会误判成「空目录」！
6. 【端点】``fs_files``（webapi.115.com）已永久 405，必须用 ``fs_files_open``。
7. 【字段】目录和文件的 id 都在 ``fid`` 字段，不是 ``cid``。
8. 【建目录容错】mkdir 返回 errno=20004「已存在」但没有 cid 时，
   回退到 ``fs_search`` 按名字查。
9. 【查重不阻断】在线查重失败（限流/网络）绝不阻断上传，降级为直接传。
10. 【线程安全】``save_state`` 用锁 + 快照，避免
    ``RuntimeError: dictionary changed size during iteration``。
11. 【性能】``os.scandir`` 而非 ``os.walk``+``getsize``（网络共享上快 60 倍）；
    目录列表按 pid 缓存（深目录 O(n²) → O(n)）。
12. 【多源列目录 + 单源冷却】``fs_files_open``（proapi）限流后，
    仍可退到 ``fs_files_aps``（aps）。但 aps **带 cookie 时会被 WAF 直接
    返 HTTP 405**（不带 cookie 反而 200）—— 这是账号/会话级拦截，
    **等多久都不恢复**。所以给每个源记冷却期，别每次调用都白撞一次。
13. 【全局熔断】770004 是**账号级**限流：一个 pid 失败，其它 pid 也一定失败。
    若每个文件都去撞一次（``list_retry`` 次指数退避 ≈ 15 s），
    8 并发下吞吐会掉到 **0.53 个/秒**（1.7 万文件要 9 小时），
    而且**永远填不上缓存 → 永远续着限流**。所以失败一次就全局停掉在线列目录。
14. 【负缓存 + 单飞】失败过的 pid 在 TTL 内不再重试；同一 pid 的并发未命中
    只发一次在线请求。**并发越高越容易把限流打出来**，单飞让「提高并发」
    从「更容易限流」变成「和串行一样安全」。
15. 【拒绝静默放错位置】限流/熔断期间 ``ensure_dir`` 每一步都会失败，
    若按「退回父目录」兜底，会把整棵树倒进机型根目录 ——
    **静默的数据放错位置，比本轮什么都不做坏得多**。改为抛 ``ListBlockedError``。
16. 【token 失效快速失败】``code=40140125 access_token 无效`` 时原地重登不可靠
    （token 在 client 内部多处缓存）。改为抛 ``AuthExpiredError``，
    由外层监督器用**全新 client** 重启；因为状态已落盘，重启代价极低。
17. 【零触网建树】``fs_search`` + ``cur=0`` 可以**遍历整棵子树**
    （目录项同时返回 ``cid`` 和 ``pid``，按 pid 自顶向下重建即可），
    把「发现状态」的成本从 O(目录数) 次列表调用压到几十次搜索调用；
    配合落盘的 ``_dircache.json``，重启后建树**完全零触网**。

术语：本模块里的 ``cid`` / ``pid`` 都指 115 目录 id（字符串）。

注意：``P115Client`` **不是线程安全的**，元数据查询必须串行；
本模块的公开函数都接受调用方传入的 client，不自己造连接。

配套模块 ``dircache.py`` 提供「用 fs_search 一次性枚举整棵子树并落盘」的工具，
是本项目里把上传吞吐从 0.53 提到 4.5+ 文件/秒的关键。
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from pathlib import Path

__all__ = [
    "UploaderConfig",
    "UploadPaths",
    "client_from_cookie",
    "make_timeout",
    "log",
    "fs_list",
    "remote_children",
    "cached_children",
    "cache_add",
    "cache_drop",
    "ensure_dir",
    "wait_for_list",
    "trip_list_block",
    "list_blocked",
    "list_block_left",
    "ListBlockedError",
    "AuthExpiredError",
    "fatal_reason",
    "load_dir_cache",
    "save_dir_cache",
    "dir_cache_path",
    "load_state",
    "save_state",
    "Watchdog",
    "upload_one",
    "walk_tree",
]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 目录缓存文件名：让「重启」不必重建整棵目录树。
# 配合 ``dircache.py`` 的 ``fs_search`` 批量枚举，可以让建树【完全零触网】。
_DIR_STORE_NAME = "_dircache.json"


class UploaderConfig:
    """上传行为参数。全部可从环境变量覆盖。

    与本仓库 MCP 服务共用 ``P115_COOKIES`` / ``P115_COOKIES_PATH`` 认证配置；
    本类只负责「怎么传」，不负责「传去哪」。
    """

    def __init__(
        self,
        chunk: int | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        watchdog_sec: float | None = None,
        max_retry: int | None = None,
        retry_base: float | None = None,
        list_retry: int | None = None,
        list_retry_base: float | None = None,
        src_cooldown_sec: float | None = None,
        list_block_sec: float | None = None,
        list_wait_max: float | None = None,
        dir_fail_ttl: float | None = None,
    ) -> None:
        env = os.environ
        self.chunk = int(chunk if chunk is not None else env.get("UP_CHUNK", 8 * 1024 * 1024))
        self.connect_timeout = float(
            connect_timeout if connect_timeout is not None else env.get("UP_CONNECT_TIMEOUT", 30)
        )
        self.read_timeout = float(
            read_timeout if read_timeout is not None else env.get("UP_READ_TIMEOUT", 180)
        )
        # 看门狗：reporthook 与网卡上行「双双」零进展多久才判卡死。
        # 大文件在分块上传前会先读一遍算 SHA1，该阶段 reporthook 不回调，
        # 所以必须结合网卡指标，否则会误杀。默认 5 分钟。
        self.watchdog_sec = float(
            watchdog_sec if watchdog_sec is not None else env.get("UP_WATCHDOG_SEC", 300)
        )
        self.max_retry = int(max_retry if max_retry is not None else env.get("UP_MAX_RETRY", 5))
        self.retry_base = float(retry_base if retry_base is not None else env.get("UP_RETRY_BASE", 5.0))
        # 目录列举单独重试（限流/405 时退避）
        self.list_retry = int(list_retry if list_retry is not None else env.get("UP_LIST_RETRY", 4))
        self.list_retry_base = float(
            list_retry_base if list_retry_base is not None else env.get("UP_LIST_RETRY_BASE", 2.0)
        )
        # ⭐ 单源冷却：某个列目录主机被 WAF 拦（HTTP 405）或连续失败后，
        #    歇这么久不再去撞它。默认 10 分钟。
        self.src_cooldown_sec = float(
            src_cooldown_sec if src_cooldown_sec is not None else env.get("UP_SRC_COOLDOWN", 600)
        )
        # ⭐ 全局列目录熔断时长（账号级限流 770004 一次要 20 分钟量级才恢复）。默认 15 分钟。
        self.list_block_sec = float(
            list_block_sec if list_block_sec is not None else env.get("UP_LIST_BLOCK_SEC", 900)
        )
        # ⭐ 建树前「等列目录恢复」的最长轮询时间。默认 30 分钟。
        self.list_wait_max = float(
            list_wait_max if list_wait_max is not None else env.get("UP_LIST_WAIT_MAX", 1800)
        )
        # ⭐ 列目录失败负缓存 TTL：失败过的 pid 在这段时间内不再重试。默认 60 s。
        self.dir_fail_ttl = float(
            dir_fail_ttl if dir_fail_ttl is not None else env.get("UP_DIR_FAIL_TTL", 60)
        )

    def make_timeout(self):
        """构造 urllib3 Timeout；分设 connect / read。

        经 ``upload_file(..., timeout=...)`` → ``**request_kwargs`` →
        ``PoolManager.request`` → ``urlopen_kw`` → ``urlopen(timeout=...)`` 透传。
        """
        try:
            from urllib3_future import Timeout

            return Timeout(connect=self.connect_timeout, read=self.read_timeout)
        except Exception:
            return self.read_timeout

    def as_dict(self) -> dict:
        return {
            "chunk_bytes": self.chunk,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "watchdog_sec": self.watchdog_sec,
            "max_retry": self.max_retry,
            "src_cooldown_sec": self.src_cooldown_sec,
            "list_block_sec": self.list_block_sec,
        }


def make_timeout(cfg: UploaderConfig | None = None):
    """构造 urllib3 Timeout（模块级便捷函数，等价于 ``cfg.make_timeout()``）。"""
    return (cfg or UploaderConfig()).make_timeout()


class UploadPaths:
    """断点续传状态文件与日志路径。"""

    def __init__(self, workdir: str | os.PathLike | None = None) -> None:
        base = Path(workdir) if workdir is not None else Path.cwd()
        base.mkdir(parents=True, exist_ok=True)
        self.workdir = str(base)
        self.state = str(base / "_upload_state.json")
        self.log = str(base / "_upload_log.jsonl")
        self.progress = str(base / "_progress.jsonl")
        self.dircache = str(base / _DIR_STORE_NAME)


def client_from_cookie(cookie: str | os.PathLike, app: str = "web"):
    """从 cookie 字符串或文件路径构造 P115Client。

    ⚠ 只有在独立运行（不挂 MCP 服务）时才需要自己造 client。
    嵌进 MCP 服务时应复用 ``P115Service.client()``，避免两套登录态。
    """
    text = str(cookie)
    if isinstance(cookie, os.PathLike) or (
        isinstance(cookie, str) and os.path.exists(cookie) and len(text) < 4096
    ):
        text = io.open(cookie, encoding="utf-8").read().strip()
    from p115client import P115Client

    return P115Client(text, app=app)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def log(rec: dict, *, paths: UploadPaths | None = None) -> None:
    target = paths.log if paths is not None else "_upload_log.jsonl"
    with io.open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


# ---------------------------------------------------------------------------
# 目录查询（多源 + 单源冷却 + 全局熔断）
# ---------------------------------------------------------------------------

def _list_aps(c, cid, offset: int, cfg: UploaderConfig):
    """aps.115.com 的列目录（备用源）。

    ⚠ 实测（2026-10）：带 cookie 请求 aps 会被 115 的 WAF 直接返 **HTTP 405**
    （**不带 cookie 反而 200**）。这是账号/会话级拦截，**等多久都不会恢复**，
    所以它只能是备用源，且被拦后必须进冷却（见 ``_SRC_COOLDOWN``），
    否则每次调用都会白撞一次 405。
    """
    return c.fs_files_aps(
        {"cid": cid, "limit": 1000, "offset": offset, "show_dir": 1,
         "o": "file_name", "asc": 1},
        timeout=cfg.make_timeout(),
    )


def _list_open(c, cid, offset: int, cfg: UploaderConfig):
    """``proapi.115.com/open/ufile/files`` 的列目录（主源，但限流极严）。

    ⚠ 不要用 ``fs_files`` —— 它指向 ``webapi.115.com/files``，**已永久 405**。
    """
    return c.fs_files_open(
        {"cid": cid, "limit": 1000, "offset": offset, "show_dir": 1,
         "o": "file_name", "asc": 1},
        timeout=cfg.make_timeout(),
    )


# 字段兼容性（实测）：aps 返回的【目录】项里，目录自身的 id 在 ``cid``；
# proapi 是在 ``fid``。【文件】项的 id 一律在 ``fid``、大小在 ``s``。
# ``remote_children`` 用 ``it.get("fid") or it.get("cid")``，
# 所以两个端点的返回都能直接吃，无需改解析逻辑。
_LIST_SOURCES = (("aps", _list_aps), ("open", _list_open))

# ---- 单源冷却：某个主机被 WAF 拦掉时，别每次都去撞它 ----
_SRC_COOLDOWN: dict[str, float] = {}       # tag -> 解禁时刻


def _src_available(tag: str) -> bool:
    return time.time() >= _SRC_COOLDOWN.get(tag, 0.0)


def _src_penalize(tag: str, reason: str, cfg: UploaderConfig) -> None:
    _SRC_COOLDOWN[tag] = time.time() + cfg.src_cooldown_sec
    log({"t": time.strftime("%F %T"), "warn": "src-cooldown",
         "src": tag, "sec": cfg.src_cooldown_sec, "reason": str(reason)[:120]})


# ---- ⭐⭐ 全局熔断：列目录被限流时，短期内地毯式停掉【所有】在线列目录 ----
# 为什么必须是【全局】而不是 per-pid：
#   770004 是【账号级】限流 —— 一个 pid 失败，其它 pid 也一定失败。
#   ``fs_list`` 自身有 list_retry 次指数退避（默认 2+4+8=14s，加往返约 15s），
#   所以「每个文件都撞一次」会把整个任务压到 8 并发/15s = **0.53 个/秒**
#   （1.7 万文件要 9 小时）。更糟的是它【永远填不上缓存】
#   → 永远续着限流 → 限流永远不恢复。
#   熔断之后：一次失败 → 之后所有 ``cached_children`` 立刻返回空表 →
#   ``upload_one`` 走它文档里写明的降级「直接传」，115 自己判重
#   （素材实测 100% 秒传，所以这个降级完全不损失正确性）。
#   同时限流期内【零查询】，给 115 留出恢复窗口。
_LIST_BLOCK_UNTIL = [0.0]                  # 用 list 包一层，避免 global 声明


def trip_list_block(reason: str = "", cfg: UploaderConfig | None = None) -> None:
    """触发全局列目录熔断。"""
    sec = (cfg or UploaderConfig()).list_block_sec
    until = time.time() + sec
    if until > _LIST_BLOCK_UNTIL[0]:
        _LIST_BLOCK_UNTIL[0] = until
        log({"t": time.strftime("%F %T"), "warn": "list-block-tripped",
             "sec": sec, "reason": str(reason)[:160]})


def list_blocked() -> bool:
    """当前是否处于列目录熔断期。"""
    return time.time() < _LIST_BLOCK_UNTIL[0]


def list_block_left() -> float:
    """熔断剩余秒数（未熔断返回 0）。"""
    return max(0.0, _LIST_BLOCK_UNTIL[0] - time.time())


class ListBlockedError(RuntimeError):
    """列目录处于熔断期，无法安全定位目录。

    见 ``ensure_dir`` 末尾的注释：此时**宁可失败，也不要把文件倒进错误目录**。
    """


# ---- ⭐ 致命错误（access_token 失效）：本轮直接放弃，交给外层重启 ----
# 为什么不做「原地重登」：
#   p115client 的 access_token 在对象内部有多处缓存（cookies / 各 app 的 token /
#   open client），原地替换不可靠。而「非零退出 → 监督器用全新 client 重启」
#   拿到的是全新登录态，且整条链路幂等（``_upload_state.json`` + ``_dircache.json``
#   都已落盘），重启代价极低。所以这里选择「快速失败 + 外部重启」。
# 触发场景（实测）：``code=40140125 msg=access_token 无效``，
#   原因是多个进程同时 ``P115Client(cookie)`` 会**互相顶掉 token**。
_FATAL: list = [None]


class AuthExpiredError(RuntimeError):
    """115 的 access_token 失效。本轮放弃，由外层用全新 client 重启。"""


def fatal_reason():
    """本轮是否已判定 token 失效；是则返回原始错误消息，否则 None。"""
    return _FATAL[0]


def _note_auth_expired(msg) -> None:
    if _FATAL[0] is None:
        _FATAL[0] = msg
        log({"t": time.strftime("%F %T"), "warn": "auth-expired",
             "err": str(msg)[:200]})


def fs_list(c, cid, offset: int = 0, cfg: UploaderConfig | None = None):
    """列举目录内容（最多一页 1000 条），带重试，并**显式识别限流**。

    关键：限流时 HTTP 仍是 200，body 里 ``state`` 为 False、``code`` 为 770004。
    只看 ``data`` 会被当成「空目录」，进而让 ``ensure_dir`` 误判目录不存在
    又去 mkdir，拿到 errno=20004「已存在」→ 建树失败一片。
    所以这里必须显式抛错让上层重试。

    端点按 ``aps`` → ``open`` 的顺序尝试；**两个都失败才算真失败**。
    只有【两个源都】报账号级限流才触发全局熔断；只有【两个源都】报
    token 失效才判定致命（aps 走 cookie、不用 access_token，若只是 proapi
    的 token 过期而 aps 正常，绝不该把整轮任务判死）。
    """
    cfg = cfg or UploaderConfig()
    last: Exception | None = None
    for attempt in range(1, cfg.list_retry + 1):
        rate_limited_all = True                # 本轮是否所有源都因限流失败
        auth_all = True                        # 本轮是否所有源都报 token 失效
        tried = 0
        for tag, fn in _LIST_SOURCES:
            if not _src_available(tag):
                continue                       # 该源处于冷却期，跳过
            tried += 1
            try:
                r = fn(c, cid, offset, cfg)
                if isinstance(r, dict) and r.get("state") is False:
                    code = r.get("code") or r.get("errno")
                    msg = r.get("message") or r.get("error") or ""
                    raise RuntimeError(
                        "%s 返回失败 state=False code=%s msg=%s" % (tag, code, msg)
                    )
                return r
            except Exception as e:              # noqa: BLE001 - 需按消息分级
                last = e
                msg = str(e)
                # ⭐ token 失效：不是限流，重试也没用 → 记致命标记
                is_auth = ("access_token" in msg) or ("40140125" in msg) \
                    or ("请重新登录" in msg) or ("token 无效" in msg)
                if not is_auth:
                    auth_all = False
                if is_auth:
                    _note_auth_expired(msg)
                # ⭐ 405 / Method Not Allowed：多半是该主机被 WAF 拦 → 进冷却
                if ("405" in msg) or ("Method Not Allowed" in msg):
                    _src_penalize(tag, msg, cfg)
                if not (("770004" in msg) or ("访问上限" in msg)):
                    rate_limited_all = False
        if tried == 0:
            # 所有源都在冷却 —— 当作「暂时拿不到」，交给熔断/负缓存去降级
            trip_list_block("all-sources-cooling", cfg)
            if attempt == cfg.list_retry:
                break
            time.sleep(min(cfg.list_retry_base * (2 ** (attempt - 1)), 30))
            continue
        if rate_limited_all and last is not None:
            trip_list_block(str(last), cfg)
        if auth_all and last is not None:
            _note_auth_expired(str(last))
        if attempt == cfg.list_retry:
            break
        msg = str(last or "")
        soft = ("405" in msg) or ("429" in msg) or ("770004" in msg) \
            or ("访问上限" in msg) or ("Too Many" in msg)
        delay = min(cfg.list_retry_base * (2 ** (attempt - 1)), 30)
        time.sleep(delay if soft else min(delay, 5))
    raise last if last is not None else RuntimeError("列目录未知失败（所有源不可用）")


def remote_children(c, cid) -> dict:
    """返回 ``{名字: (id, 字节数 or None)}``；目录的字节数为 None。

    ⚠ 字段坑：**目录和文件的 id 都在 `fid`**，不在 `cid`（aps 端点例外，见上）。
    """
    out: dict[str, tuple[str | None, int | None]] = {}
    off = 0
    while True:
        r = fs_list(c, cid, off)
        data = (r or {}).get("data") or []
        before = len(out)
        for it in data:
            name = it.get("n") or it.get("fn") or ""
            isdir = str(it.get("fc")) == "0"
            idv = it.get("fid") or it.get("cid")       # 两者都优先取 fid
            size = None if isdir else int(it.get("s") or it.get("fs") or 0)
            if name:
                out[name] = (str(idv) if idv else None, size)
        if len(data) < 1000:
            break
        # ⚠ 翻页死循环保护：
        #   aps 的文档写明「offset >= 1200+n 时相当于 offset=0」—— 也就是
        #   超过上限后它会【从头再来】。若某目录 >1000 条，``len(data)==1000``
        #   会让我们一直翻下去，而每页都返回同样的内容 → 永不结束。
        #   判据：这一页没有带来任何【新】名字 ⇒ 翻页失效，停。
        if len(out) == before:
            log({"t": time.strftime("%F %T"), "warn": "page-no-progress",
                 "cid": cid, "off": off, "got": len(data)})
            break
        off += 1000
    return out


# ---- 目录列表缓存（性能关键）----
# 每文件都重查目录列表 → O(n²)。按 pid 缓存后：
# 深目录机型实测 0.8s/文件 → 0.06s/文件（约 15×）。
_DIR_CACHE: dict[str, dict] = {}
_DIR_LOCK = threading.RLock()
_DIR_INFLIGHT: dict[str, threading.Event] = {}   # pid -> Event（单飞）
_DIR_FAIL: dict[str, float] = {}                 # pid -> 上次失败时刻（负缓存）


def dir_cache_path(workdir: str | os.PathLike | None = None) -> str:
    """目录缓存文件路径（默认放在 ``UploadPaths`` 的工作目录下）。"""
    base = Path(workdir) if workdir is not None else Path.cwd()
    return str(base / _DIR_STORE_NAME)


def load_dir_cache(path: str | os.PathLike | None = None) -> int:
    """把上次落盘的目录缓存读回来；返回载入的 pid 数（失败返回 0，不抛）。"""
    target = str(path) if path is not None else dir_cache_path()
    try:
        with io.open(target, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return 0
    except Exception as e:                     # noqa: BLE001 - 纯优化，失败即忽略
        log({"t": time.strftime("%F %T"), "warn": "dircache-load-fail",
             "err": repr(e)[:160]})
        return 0
    n = 0
    with _DIR_LOCK:
        for pid, m in (raw.get("cache") or {}).items():
            _DIR_CACHE[pid] = {k: (v[0], v[1]) for k, v in m.items()}
            n += 1
    return n


def save_dir_cache(path: str | os.PathLike | None = None) -> int:
    """把目录缓存落盘；返回写入的 pid 数。失败不抛（它只是省时间的优化）。"""
    target = str(path) if path is not None else dir_cache_path()
    try:
        with _DIR_LOCK:
            snap = {pid: {k: [v[0], v[1]] for k, v in m.items()}
                    for pid, m in _DIR_CACHE.items()}
        tmp = "%s.%d.tmp" % (target, os.getpid())   # 带 pid，避免多 worker 互踩
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.strftime("%F %T"), "cache": snap},
                      f, ensure_ascii=False)
        for attempt in range(5):                 # Windows 上可能被杀软/索引短暂占用
            try:
                os.replace(tmp, target)
                return len(snap)
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception as e:                       # noqa: BLE001
        log({"t": time.strftime("%F %T"), "warn": "dircache-save-fail",
             "err": repr(e)[:160]})
    return 0


def wait_for_list(c, root_cid: str, tag: str = "",
                  max_wait: float | None = None,
                  cfg: UploaderConfig | None = None) -> bool:
    """等列目录接口恢复可用；返回 True 表示可用。

    ⭐ 短路：如果 ``root_cid`` **已经在目录缓存里**，说明 ``_dircache.json``
    已经把整棵树喂进来了 —— 建树和预取都会纯内存命中，根本不需要列目录。
    这时再做一次网络探测纯属浪费，而且探测失败还会把熔断触发、白等 30 分钟。

    ⚠ 为什么建树【之前】必须等：
      限流期间 ``ensure_dir`` 的每一步都会失败，最后走到「退回父目录」，
      把整棵树的文件倒进机型根目录 —— 这是**静默的数据放错位置**，
      比「本轮什么都不做」坏得多。
      770004 是账号级限流，恢复要 20 分钟量级，所以这里按 20s → 40s → 80s → 120s
      的间隔轮询探测，最长等 ``max_wait`` 秒。

    探测成功会【提前解除熔断】（说明 115 已经放开了）。
    """
    cfg = cfg or UploaderConfig()
    with _DIR_LOCK:
        if root_cid in _DIR_CACHE:
            return True
    if max_wait is None:
        max_wait = cfg.list_wait_max
    t0 = time.time()
    delay = 20.0
    while True:
        try:
            remote_children(c, root_cid)         # 直接查，绕过熔断/缓存
            if list_blocked():
                _LIST_BLOCK_UNTIL[0] = 0.0       # 探测成功 → 解除熔断
                log({"t": time.strftime("%F %T"), "warn": "list-block-cleared",
                     "tag": tag})
            return True
        except Exception as e:                    # noqa: BLE001
            trip_list_block(str(e), cfg)
            # token 失效时等多久都没用 → 立刻放弃，交给外层重启
            if _FATAL[0] is not None:
                log({"t": time.strftime("%F %T"), "warn": "list-wait-auth-fatal",
                     "tag": tag, "err": repr(e)[:160]})
                return False
            waited = time.time() - t0
            if waited >= max_wait:
                log({"t": time.strftime("%F %T"), "warn": "list-wait-timeout",
                     "tag": tag, "waited": round(waited, 1), "err": repr(e)[:160]})
                return False
            log({"t": time.strftime("%F %T"), "warn": "list-wait",
                 "tag": tag, "waited": round(waited, 1),
                 "sleep": delay, "err": repr(e)[:120]})
            time.sleep(min(delay, 120))
            delay = min(delay * 2, 120)


def cached_children(c, pid, refresh: bool = False) -> dict:
    """带缓存的 ``remote_children``，并对同一 pid 做「单飞」合并。

    ⚠ 为什么需要单飞：
      原实现只在【读/写 ``_DIR_CACHE``】时持锁，而 ``remote_children()``
      是在锁【外面】调的。于是 N 个 worker 同时撞上同一个 pid 的缓存未命中时，
      会各自发一次在线列目录请求 —— 而 115 的列目录接口连打 ~30 次就会触发
      770004「已达到当前访问上限」。**并发越高，越容易把限流打出来**，
      而限流一旦触发要 20 分钟以上才恢复。

      单飞之后：同一 pid 的并发未命中只会有 1 次在线请求，其余线程等它。
      这让「提高并发」从「更容易触发限流」变成「和串行一样安全」。

    ``refresh=True`` 语义不变（强制拿新数据）：它会发起一次新查询，
    但同样与其它并发请求合并 —— 等待者拿到的是刚查回来的新数据，
    对调用方而言等价于「自己刷新过了」。
    """
    cfg = UploaderConfig()
    while True:
        with _DIR_LOCK:
            if not refresh and pid in _DIR_CACHE:
                return _DIR_CACHE[pid]
            # ⭐ 全局熔断（账号级限流）：直接返回空表，零触网。
            #   调用方 upload_one 会走「直接传」降级 —— 对秒传素材完全无损失。
            if list_blocked():
                return {}
            # ⚠ 负缓存：刚失败过的 pid 【不再重试】，直接返回空表。
            #   ``fs_list`` 对 770004/405/429 有指数退避重试（单次最坏约 2 分钟）。
            #   若失败后不记住，该目录下的【每一个文件】都会再触发一次这种重试 ——
            #   1.7 万文件会把整段任务从「慢」直接变成「瘫」。
            #   返回空表 = 让 upload_one 走降级路径「直接传，115 自己判重」。
            ts = _DIR_FAIL.get(pid)
            if not refresh and ts is not None and (time.time() - ts) < cfg.dir_fail_ttl:
                return {}
            ev = _DIR_INFLIGHT.get(pid)
            if ev is None:
                ev = _DIR_INFLIGHT[pid] = threading.Event()
                owner = True
            else:
                owner = False
        if owner:
            break
        ev.wait()                                # 等别人查完，回头再读缓存
        with _DIR_LOCK:
            if pid in _DIR_CACHE:
                return _DIR_CACHE[pid]
        refresh = False                          # 别人查完却没写缓存 → 这轮我自己来
    try:
        kids = remote_children(c, pid)
        with _DIR_LOCK:
            _DIR_CACHE[pid] = kids
            _DIR_FAIL.pop(pid, None)             # 成功了就清掉负缓存
        return kids
    except Exception:
        with _DIR_LOCK:
            _DIR_FAIL[pid] = time.time()         # 记一笔，见上面负缓存注释
        raise
    finally:
        with _DIR_LOCK:
            _DIR_INFLIGHT.pop(pid, None)
        ev.set()


def cache_add(pid, name, size, fid=None) -> None:
    """把（可能新产生的）子项写进缓存。

    ⚠ 缓存污染坑：上传完文件后如果无脑 ``cache_add(pid, name, size)``，
    会把**同名目录条目**的 id 覆盖成空串，导致后续 ``ensure_dir`` 认为
    目录不存在 → 重复 mkdir → errno=20004 → 建树失败。
    所以这里：已有非空 id 时保留；目录条目不许被降级成文件条目。
    """
    with _DIR_LOCK:
        m = _DIR_CACHE.get(pid)
        if m is None:
            return
        old = m.get(name)
        new_id = str(fid) if fid else ""
        if old and old[0]:
            if old[1] is None and size is not None:
                return                             # 别把「目录」降级成「文件」记录
            if not new_id:
                new_id = old[0]                    # 保留原有非空 id
        m[name] = (new_id, size)


def cache_drop(pid) -> None:
    with _DIR_LOCK:
        _DIR_CACHE.pop(pid, None)


def _search_child(c, pid, name):
    """兜底：115 端脏数据（目录存在但列不出来）时按名字搜。"""
    try:
        r = c.fs_search({"search_value": name, "cid": pid, "limit": 50},
                        timeout=UploaderConfig().make_timeout())
    except Exception:
        return None
    for it in (r.get("data") or []):
        if (it.get("fn") or it.get("n")) == name and str(it.get("fc")) == "0":
            cid = it.get("fid") or it.get("cid")
            if cid:
                return str(cid)
    return None


def _mkdir_try(c, name, pid):
    """尝试建目录；返回 ``(cid or None, 是否已存在)``。"""
    try:
        r = c.fs_mkdir(name, pid, timeout=UploaderConfig().make_timeout())
    except Exception:
        return None, False
    if not isinstance(r, dict):
        return None, False
    cid = r.get("cid") or r.get("fid") or r.get("category_id")
    if cid:
        return str(cid), False
    existed = (str(r.get("errno")) == "20004") or ("已存在" in str(r.get("error", "")))
    return None, existed


def ensure_dir(c, pid, name) -> str:
    """确保 ``pid/name`` 存在，返回其 cid。分层兜底，尽量不丢文件。"""

    def _lookup(refresh: bool):
        kids = cached_children(c, pid, refresh=refresh)
        v = kids.get(name)
        if v and v[0]:                             # id 必须非空才算命中
            return v[0]
        return None

    cid = _lookup(False)
    if cid:
        return cid
    cid = _lookup(True)                            # 刷新一次，排除缓存不完整/脏
    if cid:
        return cid

    new_cid, existed = _mkdir_try(c, name, pid)
    if new_cid:
        cache_add(pid, name, None, new_cid)
        return new_cid

    cid = _lookup(True)
    if cid:
        return cid
    if existed:
        # ⚠ 115 侧脏数据：mkdir 说「已存在」（errno=20004）但列表接口漏报该条目。
        #   实测 ``fs_search`` 能按名字查到它 → 用搜索结果拿真实 cid。
        scid = _search_child(c, pid, name)
        if scid:
            cache_add(pid, name, None, scid)
            log({"t": time.strftime("%F %T"), "warn": "dir-found-via-search",
                 "parent": pid, "name": name, "cid": scid})
            return scid
        # ⚠⚠ 搜索也拿不到 —— 传统做法是「退回父目录（不丢文件）」，
        #   但那会把文件传进【错误的目录】，比什么都不做坏得多！
        #   限流/熔断期间，上面的每一步都会失败，正好走到这里，
        #   于是整棵树的文件都可能被倒进机型根目录。这里改成显式失败：
        #   让调用方把这条 chain 记为 build_fail（文件本轮不处理，下一轮再建树），
        #   **绝不静默放错位置**。
        if list_blocked():
            raise ListBlockedError(
                "列目录处于熔断期，无法定位目录 cid（拒绝退回父目录以免放错位置）"
                " parent=%r name=%r" % (pid, name))
        log({"t": time.strftime("%F %T"), "warn": "dir-unresolvable",
             "parent": pid, "name": name})
        return pid
    raise RuntimeError("无法确定目录 cid: parent=%r name=%r existed=%s"
                       % (pid, name, existed))


# ---------------------------------------------------------------------------
# 断点续传状态（进程级 + 文件级）
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.RLock()


def load_state(path: str) -> dict:
    if os.path.exists(path):
        try:
            return json.load(io.open(path, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(st: dict, path: str) -> None:
    """原子 + 线程安全地落盘状态。

    ⚠ 锁保护两件事（多线程并发上传时的崩溃根因）：
      1) 序列化时必须拿到「稳定快照」——并发 worker 边写 st 边 dump，
         会抛 ``RuntimeError: dictionary changed size during iteration``；
      2) 多个 worker 同时 ``os.replace(tmp, path)`` 会互相踩 .tmp 文件。
    锁内只做 dict() 浅拷贝（GIL 下安全），磁盘 IO 放在锁外。
    """
    with _STATE_LOCK:
        try:
            snap = dict(st)
        except RuntimeError:
            snap = {}
    tmp = "%s.%d.tmp" % (path, os.getpid())        # 带 pid，多 worker 不互踩
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())                       # 落盘再 rename，防半截文件
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 看门狗
# ---------------------------------------------------------------------------

class Watchdog:
    """监控上传进展；超时无进展则让 reporthook 抛异常中断上传。

    双指标判定（任一有流量即视为「活着」）：
      a) reporthook 回调 —— 上传阶段每读到一块数据都会回调；
      b) 本机网卡上行 —— 覆盖「正在发但 reporthook 尚未回调」的间隙。
    这样既能抓住「TCP 半死挂起」（两者都归零），又不会误杀
    「大文件上传前的 SHA1 计算阶段」。
    """

    def __init__(self, limit_sec: float, label: str = "") -> None:
        self.limit = limit_sec
        self.label = label
        self.last = time.monotonic()
        self.total = 0
        self._stop = threading.Event()
        self._dead: Exception | None = None
        self._last_nic = self._nic_octets()
        self._last_nic_time = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def expired(self) -> Exception | None:
        """卡死时返回异常对象，否则 None。由上传循环轮询。"""
        return self._dead

    @staticmethod
    def _nic_octets():
        """本机网卡已发送字节数；拿不到则返回 None（退化为单指标）。

        ⚠ 注意：若上传数据流**不经过本机**（例如源在 NAS 上、由 NAS 直连 115），
        这个指标恒为 0，看门狗会退化成「只看 reporthook」。届时应调大
        ``UP_WATCHDOG_SEC``（例如 900），或给底层请求设更短的 read timeout，
        避免误判卡死。
        """
        try:
            import psutil

            return psutil.net_io_counters().bytes_sent
        except Exception:
            return None

    def hook(self, blocknum, blocksize, totalsize) -> None:
        self.total = min(blocknum * blocksize, totalsize) if totalsize else self.total
        self.last = time.monotonic()
        if self._dead is not None:
            # 看门狗已判定卡死 → 在 reporthook 里抛异常，才能中断底层 PUT
            raise self._dead

    def _run(self) -> None:
        while not self._stop.wait(15):
            now = time.monotonic()
            quiet = now - self.last
            if quiet < self.limit:
                continue
            nic_now = self._nic_octets()
            if nic_now is None or self._last_nic is None:
                # 拿不到网卡指标：保守起见不判死，只刷新计时交由上层超时兜底
                self.last = now
                self._last_nic = nic_now
                self._last_nic_time = now
                continue
            moved = nic_now - self._last_nic
            self._last_nic = nic_now
            self._last_nic_time = now
            if moved > 256 * 1024:                 # 网卡仍在动 → 判定「活着」
                self.last = now
                continue
            self._dead = TimeoutError(
                "上传 %s 已 %.0fs 无任何进展（reporthook 与网卡双归零），判定卡死"
                % (self.label, quiet)
            )
            return

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

def upload_one(c, local, pid, st, key, cfg: UploaderConfig | None = None,
               dry: bool = False) -> str:
    """上传单文件；本地状态或远端同大小则跳过。

    查重策略：
      - 本地状态 ``st[key]=='done'`` 是【第一权威】，命中直接跳过，零触网；
      - 仅当状态未记录时才在线列目录查重；
      - **在线查重失败绝不阻断上传**（限流时降级为直接传，
        115 收到重复文件会自行判重）。
    """
    cfg = cfg or UploaderConfig()
    size = os.path.getsize(local)
    name = os.path.basename(local)
    # ⭐ token 失效就别再白干了：直接抛，让它冒泡出去中止本轮，
    #   由外层监督器用【全新 client】重启（见 AuthExpiredError 注释）。
    if _FATAL[0] is not None:
        raise AuthExpiredError(_FATAL[0])
    if st.get(key) == "done":
        return "skip-state"

    try:
        kids = cached_children(c, pid)
        if name in kids and kids[name][1] == size:
            st[key] = "done"
            return "skip-exists"
    except Exception:
        pass                                       # 限流/网络问题 → 直接传

    if dry:
        return "dry"

    last_err = ""
    for attempt in range(1, cfg.max_retry + 1):
        wd = Watchdog(cfg.watchdog_sec, label=local)
        t0 = time.time()
        try:
            c.upload_file(
                local, pid,
                filesize=size,
                partsize=cfg.chunk if size >= cfg.chunk else 0,
                reporthook=wd.hook,
                timeout=cfg.make_timeout(),
            )
            wd.stop()
            st[key] = "done"
            cache_add(pid, name, size)
            return "ok"
        except Exception as e:                     # noqa: BLE001 - 上传失败要重试
            wd.stop()
            last_err = repr(e)[:300]
            log({"file": local, "err": last_err, "attempt": attempt,
                 "elapsed": round(time.time() - t0, 1), "got_bytes": wd.total})
            if attempt == cfg.max_retry:
                break
            delay = min(cfg.retry_base * (2 ** (attempt - 1)), 120)
            print("    ! 重试 %d/%d（等 %.0fs）%s"
                  % (attempt, cfg.max_retry, delay, last_err[:90]))
            sys.stdout.flush()
            time.sleep(delay)
    st[key] = "fail"
    return "fail:" + last_err[:120]


def walk_tree(root) -> list:
    """列出目录下所有文件 ``(本地路径, 目录链, 字节数)``，按体积升序。

    用 ``os.scandir`` 而非 ``os.walk`` + ``getsize``：网络共享上 scandir
    一次就能拿到 ``DirEntry.stat()``（含 size），避免每个文件一次额外
    stat 往返。8572 文件实测：**102s → 1.7s**。
    """
    base = str(root)
    items = []
    stack = [(base, ())]
    while stack:
        cur, chain = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append((e.path, chain + (e.name,)))
                        elif e.is_file(follow_symlinks=False):
                            items.append((e.path, list(chain), e.stat().st_size))
                    except OSError:
                        pass
        except OSError:
            pass
    items.sort(key=lambda x: x[2])                 # 小文件先传，进度可见
    return items
