# -*- coding: utf-8 -*-
"""零触网建树：用 ``fs_search`` 一次性枚举 115 上的整棵子树，落盘成目录缓存。

为什么需要它
------------
上传前必须知道「本地每个目录在 115 上对应哪个 cid」。直觉做法是对每个目录调一次
列目录接口（``fs_files_open``）—— 但在实战项目里，9 个机型合计 346 个目录，
就是 **346 次列目录请求**，而该接口**连打约 30 次就会触发账号级限流 770004**
（HTTP 仍是 200，body 里 ``state=false``；恢复要 20 分钟量级）。
于是「建树」这一小步就能把整个任务从「几小时」拖成「永远跑不完」。

关键发现：列目录不是唯一的路
----------------------------
``fs_search``（``webapi.115.com/files/search``）在同时给出
``search_value`` 和 ``cur=0`` 时会**遍历 cid 下的整棵子树**（递归），
且返回的目录条目同时带：

* ``cid`` —— 该目录**自身**的 id
* ``pid`` —— 它的**父目录** id

于是「(父 id, 名字) → 自身 id」的关系可以用少量搜索全部拿到，
再按 ``pid`` 自顶向下重建整棵树。

⚠ 两个实测陷阱：

1. **必须给 ``search_value``** —— 不给的话，即使带上 ``cur=0&show_dir=1``
   也返回空数组。
2. ``suffix`` 参数**无效**，别指望用它按扩展名筛。

搜索词怎么选
------------
取「每个目录名的前 4 个字符」再去重。因为任何目录 D 的名字 N 都包含前缀
``N[:4]``，所以搜索 ``N[:4]`` 必然命中 D。实测把某机型的 70 个去重目录名
压缩到 17 个搜索词；9 个机型合计只用 **48 次搜索调用**就 dump 完整棵树。

产出
----
``_dircache.json``：``{"t": ..., "cache": {pid: {name: [id, size]}}}``，
其中 ``size is None`` 表示目录。``uploader.load_dir_cache()`` 直接读它，
之后建树 + 预取全部**纯内存命中、零网络请求**。

典型用法
--------
::

    from mcp_115_server import dircache as DC, uploader as U

    c = U.client_from_cookie("115-cookies.txt")
    # 1) 扫本地目录（也可以自己构造 {model: [[目录链...], ...]}）
    local = DC.scan_local_dirs(r"\\\\nas\\share\\素材")
    # 2) 用 fs_search 把 115 上的树 dump 下来（顺便补建缺失目录）
    cache, report = DC.build_dir_cache(c, roots={"（AWG3）ALEXA XT": "3538..."},
                                       local_dirs=local)
    DC.write_cache("_dircache.json", cache)
    # 3) 上传器侧：零触网建树
    U.load_dir_cache("_dircache.json")
"""
from __future__ import annotations

import io
import json
import os
import time

__all__ = [
    "prefix_terms",
    "search_all",
    "scan_local_dirs",
    "build_dir_cache",
    "read_cache",
    "write_cache",
]

PAGE = 1000          # fs_search 单页上限
MAX_PAGES = 20       # 单次搜索最多翻 20 页（防失控）


def prefix_terms(names, n: int = 4) -> list:
    """把一组目录名压成一组搜索词：取前 ``n`` 个字符再去重。

    任何目录 D 的名字 N 都包含前缀 ``N[:n]``，所以搜 ``N[:n]`` 必然命中 D。
    名字本身短于 ``n`` 时直接用整个名字。
    """
    return sorted({(s[:n] if len(s) > n else s) for s in names if s})


def search_all(c, search_value: str, cid, *, limit: int = PAGE,
               timeout: float = 30.0, max_pages: int = MAX_PAGES) -> list:
    """带 ``search_value`` + ``cur=0`` 的**子树**搜索，自动翻页。返回 data 列表。

    ⚠ ``cur=0`` 是「递归整个子树」的开关；``search_value`` 是「必须给」的参数。
    限流时同样会 ``state=False`` —— 这里显式抛错，绝不当成空结果。
    """
    out, off = [], 0
    for _ in range(max_pages):
        r = c.fs_search(
            {"search_value": search_value, "cid": cid, "cur": 0,
             "show_dir": 1, "limit": limit, "offset": off},
            timeout=timeout,
        )
        if isinstance(r, dict) and r.get("state") is False:
            raise RuntimeError(
                "fs_search 返回失败 state=False code=%s msg=%s"
                % (r.get("code"), r.get("message"))
            )
        d = (r or {}).get("data") or []
        out += d
        if len(d) < limit:
            break
        off += limit
    return out


def scan_local_dirs(root, models=None) -> dict:
    """扫本地素材目录，返回 ``{机型: [目录链, ...]}``（只扫目录，很快）。

    用 ``os.scandir`` 而非 ``os.walk``：网络共享上一次 scandir 就能拿到
    ``DirEntry``，不必每个文件再 stat 一次。实测 8572 文件 **102 s → 1.7 s**。
    """
    base = str(root)
    if models is None:
        try:
            models = sorted(e.name for e in os.scandir(base)
                            if e.is_dir(follow_symlinks=False))
        except OSError:
            models = []
    tree: dict[str, list] = {}
    for m in models:
        dirs, stack = [], [(os.path.join(base, m), ())]
        while stack:
            cur, chain = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                c2 = chain + (e.name,)
                                dirs.append(list(c2))
                                stack.append((e.path, c2))
                        except OSError:
                            pass
            except OSError:
                pass
        tree[m] = dirs
    return tree


def _mkdir(c, name, pid, timeout: float):
    """建目录；返回新 cid 或 None（"已存在"等一律返回 None）。"""
    try:
        r = c.fs_mkdir(name, pid, timeout=timeout)
    except Exception:                              # noqa: BLE001
        return None
    if not isinstance(r, dict):
        return None
    cid = r.get("cid") or r.get("fid") or r.get("category_id")
    return str(cid) if cid else None


def build_dir_cache(c, roots: dict, *, local_dirs: dict | None = None,
                    terms: dict | None = None, dry_run: bool = False,
                    gap: float = 0.15, timeout: float = 30.0,
                    mkdir_missing: bool = True, log=None):
    """用 ``fs_search`` 枚举 115 上的子树，产出可直接喂给上传器的目录缓存。

    :param c: 已登录的 ``P115Client``。
    :param roots: ``{机型名: 顶层 cid}`` —— 每个机型在 115 上的根目录 id。
    :param local_dirs: ``{机型名: [目录链, ...]}``（``scan_local_dirs`` 的产物）。
        给了它才能：(a) 用前 4 字符压缩搜索词；(b) 发现**缺失目录**并补建。
        不给则退化为「搜索词 = 顶层机型名」，只 dump 已有结构、不建目录。
    :param terms: ``{机型名: [搜索词, ...]}``，显式覆盖自动推导的搜索词。
    :param dry_run: 只算不写（不 mkdir，也不落盘）。
    :param gap: 每次搜索/建目录之间的间隔，别把限流打出来。
    :param mkdir_missing: 本地有、115 没有的目录是否自动补建。
    :param log: 可选 ``callable(rec: dict)``，用于记录告警。
    :return: ``(cache, report)``。``cache`` 结构 ``{pid: {name: [id, size]}}``；
        ``report`` 是每个机型一行的统计 dict 列表。

    搜索词取「每个目录名前 4 字符」去重；按 ``pid`` 自顶向下把本地目录链
    映射到 cid，映射不到的（115 上还没有）用 ``fs_mkdir`` 自顶向下补建。
    """
    local_dirs = local_dirs or {}
    terms = dict(terms or {})
    n_search = n_mkdir = 0
    cache: dict[str, dict] = {}

    def _warn(kind, **kw):
        rec = {"t": time.strftime("%F %T"), "warn": kind}
        rec.update(kw)
        if log is not None:
            log(rec)

    report = []
    for model, root in roots.items():
        t0 = time.time()
        chains = list(local_dirs.get(model) or [])
        # ---- 1) 搜索词 ----
        if model in terms:
            use_terms = list(terms[model])
        elif chains:
            use_terms = prefix_terms({ch[-1] for ch in chains})
        else:
            use_terms = [model]                  # 没有本地树时只能粗搜
        # ---- 2) 搜出整棵子树，按 pid 建索引 ----
        by_pid: dict[str, dict] = {}             # pid -> {name: cid}   （目录）
        n_hit = n_files = n_fail = 0
        for t in use_terms:
            try:
                res = search_all(c, t, root, timeout=timeout)
            except Exception as e:               # noqa: BLE001
                n_fail += 1
                _warn("search-err", model=model, term=t, err=repr(e)[:200])
                time.sleep(1.0)
                continue
            for it in res:
                n = it.get("n")
                if not n:
                    continue
                if str(it.get("fc")) == "0":     # 目录
                    cid = it.get("cid") or it.get("fid")
                    pid = it.get("pid")
                    if cid and pid:
                        by_pid.setdefault(str(pid), {})[n] = str(cid)
                        n_hit += 1
                else:                            # 文件：顺带记进缓存
                    fid = it.get("fid")
                    pid = it.get("cid")
                    sz = it.get("s") or it.get("fs")
                    if fid and pid and sz is not None:
                        cache.setdefault(str(pid), {})[n] = [str(fid), int(sz)]
                        n_files += 1
            n_search += 1
            time.sleep(gap)
        # ---- 3) 按 pid 自顶向下把本地目录链映射到 cid ----
        resolved: dict[tuple, str] = {}
        missing: list[list] = []
        for chain in sorted(chains, key=len):
            p = root if len(chain) == 1 else resolved.get(tuple(chain[:-1]))
            if p is None:
                missing.append(chain)
                continue
            cid = by_pid.get(p, {}).get(chain[-1])
            if cid:
                resolved[tuple(chain)] = cid
            else:
                missing.append(chain)
        # ---- 4) 补建缺失目录（必须自顶向下：父要先有 cid）----
        made = 0
        if mkdir_missing:
            for chain in sorted(missing, key=len):
                p = root if len(chain) == 1 else resolved.get(tuple(chain[:-1]))
                if p is None:
                    _warn("no-parent", model=model, chain=list(chain))
                    continue
                if dry_run:
                    continue
                cid = _mkdir(c, chain[-1], p, timeout)
                if cid:
                    resolved[tuple(chain)] = cid
                    by_pid.setdefault(p, {})[chain[-1]] = cid
                    made += 1
                    n_mkdir += 1
                time.sleep(gap)
        # ---- 5) 写进 cache ----
        # 每个目录的 pid 都要有一条（哪怕空表），这样 cached_children 才会
        # 命中缓存、零触网；漏掉任何一个都会让上传时又去打一次在线列目录。
        for _chain, cid in resolved.items():
            cache.setdefault(cid, {})
        for pid, kids in by_pid.items():
            cache.setdefault(pid, {}).update({n: [cid, None] for n, cid in kids.items()})
        report.append({
            "model": model, "root": root,
            "dirs_local": len(chains), "dirs_resolved": len(resolved),
            "dirs_created": made, "terms": len(use_terms),
            "dir_hits": n_hit, "file_hits": n_files, "search_fail": n_fail,
            "seconds": round(time.time() - t0, 1),
        })
    return cache, report


def write_cache(path, cache: dict, *, indent: int | None = None) -> str:
    """落盘目录缓存（原子写：先写 ``.tmp`` 再 ``os.replace``）。"""
    target = str(path)
    tmp = "%s.%d.tmp" % (target, os.getpid())
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump({"t": time.strftime("%F %T"), "cache": cache},
                  f, ensure_ascii=False, indent=indent)
    for attempt in range(5):
        try:
            os.replace(tmp, target)
            return target
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    return target


def read_cache(path) -> dict:
    """读回目录缓存；文件不存在或损坏时返回空 dict（不抛）。"""
    try:
        with io.open(str(path), encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    return raw.get("cache") or {}
