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

术语：本模块里的 ``cid`` / ``pid`` 都指 115 目录 id（字符串）。

注意：``P115Client`` **不是线程安全的**，元数据查询必须串行；
本模块的公开函数都接受调用方传入的 client，不自己造连接。
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
    "load_state",
    "save_state",
    "Watchdog",
    "upload_one",
    "walk_tree",
]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

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
        }


class UploadPaths:
    """断点续传状态文件与日志路径。"""

    def __init__(self, workdir: str | os.PathLike | None = None) -> None:
        base = Path(workdir) if workdir is not None else Path.cwd()
        base.mkdir(parents=True, exist_ok=True)
        self.workdir = str(base)
        self.state = str(base / "_upload_state.json")
        self.log = str(base / "_upload_log.jsonl")
        self.progress = str(base / "_progress.jsonl")


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
# 目录查询（限流感知）
# ---------------------------------------------------------------------------

def fs_list(c, cid, offset: int = 0, cfg: UploaderConfig | None = None):
    """列举目录内容，带重试，并**显式识别限流**。

    关键：限流时 HTTP 仍是 200，body 里 ``state`` 为 False、``code`` 为 770004。
    只看 ``data`` 会被当成「空目录」，进而让 ``ensure_dir`` 误判目录不存在
    又去 mkdir，拿到 errno=20004「已存在」→ 建树失败一片。
    所以这里必须显式抛错让上层重试。

    端点：``fs_files_open``（proapi.115.com/open/ufile/files）。
    ⚠ 不要用 ``fs_files`` —— 它指向 webapi.115.com，**已永久 405**。
    """
    cfg = cfg or UploaderConfig()
    last: Exception | None = None
    for attempt in range(1, cfg.list_retry + 1):
        try:
            r = c.fs_files_open(
                {"cid": cid, "limit": 1000, "offset": offset, "show_dir": 1,
                 "o": "file_name", "asc": 1},
                timeout=cfg.make_timeout(),
            )
            if isinstance(r, dict) and r.get("state") is False:
                code = r.get("code") or r.get("errno")
                msg = r.get("message") or r.get("error") or ""
                raise RuntimeError(
                    "fs_files_open 返回失败 state=False code=%s msg=%s" % (code, msg)
                )
            return r
        except Exception as e:                     # noqa: BLE001 - 需按消息分级
            last = e
            msg = str(e)
            soft = ("405" in msg) or ("429" in msg) or ("770004" in msg) \
                or ("访问上限" in msg) or ("Too Many" in msg)
            if attempt == cfg.list_retry:
                break
            delay = min(cfg.list_retry_base * (2 ** (attempt - 1)), 30)
            time.sleep(delay if soft else min(delay, 5))
    raise last if last is not None else RuntimeError("fs_files_open 未知失败")


def remote_children(c, cid) -> dict:
    """返回 ``{名字: (id, 字节数 or None)}``；目录的字节数为 None。

    ⚠ 字段坑：**目录和文件的 id 都在 `fid`**，不在 `cid`。
    """
    out: dict[str, tuple[str | None, int | None]] = {}
    off = 0
    while True:
        r = fs_list(c, cid, off)
        data = (r or {}).get("data") or []
        if not data:
            break
        for it in data:
            name = it.get("n") or it.get("fn") or ""
            isdir = str(it.get("fc")) == "0"
            idv = it.get("fid") or it.get("cid")       # 两者都优先取 fid
            size = None if isdir else int(it.get("s") or it.get("fs") or 0)
            if name:
                out[name] = (str(idv) if idv else None, size)
        if len(data) < 1000:
            break
        off += len(data)
    return out


# ---- 目录列表缓存（性能关键）----
# 每文件都重查目录列表 → O(n²)。按 pid 缓存后：
# 深目录机型实测 0.8s/文件 → 0.06s/文件（约 15×）。
_DIR_CACHE: dict[str, dict] = {}
_DIR_LOCK = threading.RLock()


def cached_children(c, pid, refresh: bool = False) -> dict:
    with _DIR_LOCK:
        if not refresh and pid in _DIR_CACHE:
            return _DIR_CACHE[pid]
    kids = remote_children(c, pid)
    with _DIR_LOCK:
        _DIR_CACHE[pid] = kids
    return kids


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
        scid = _search_child(c, pid, name)
        if scid:
            cache_add(pid, name, None, scid)
            log({"t": time.strftime("%F %T"), "warn": "dir-found-via-search",
                 "parent": pid, "name": name, "cid": scid})
            return scid
        log({"t": time.strftime("%F %T"), "warn": "dir-unresolvable",
             "parent": pid, "name": name})
        return pid                                 # 降级：退到父目录，不丢文件
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
