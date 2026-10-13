# -*- coding: utf-8 -*-
"""批量并发上传编排。

架构（实测出来的最优形态）：

    主线程（串行，API 调用极少）
    ├─ 扫描本地素材（os.scandir）
    ├─ 展开所有目录链前缀，去重
    ├─ 逐个 ensure_dir  →  建好整棵目录树
    └─ 生成 jobs = [(文件, 目标pid, 大小), ...]
            │
            ▼
    线程池（并发=4，只做上传，不查目录）
    └─ 每个文件：查本地状态（零触网）→ 上传 → 写回缓存 + 更新状态

**核心原则：把所有「元数据查询」压到主线程串行完成，让线程池只专心搬数据。**

为什么线程池不查目录：
  - ``P115Client`` 不是线程安全的（实测 8 线程共享实例查询 8/8 失败）；
  - 115 对同一 cookie 的并发查询有限制；
  - 元数据查询会**加速触发限流**（proapi 连打约 30 次即被封）。

实测吞吐（SXT 6.88 MB/文件，干净样本无秒传污染）：
    并发 1 → 8.9 MB/s（1.00×）
    并发 4 → 20.6 MB/s（2.5×）
    并发 8 → 30.2 MB/s（3.4×）
    16/24 无提升；32 出错
瓶颈是存储侧单线程读取上限（约 28 MB/s），故 4 已是性价比拐点，8 为天花板。
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import uploader as U

__all__ = ["BatchUploader", "BatchResult"]


class BatchResult(dict):
    """一次批量上传的统计结果。"""

    def __init__(self) -> None:
        super().__init__(ok=0, skip=0, fail=0, bytes_uploaded=0,
                         elapsed=0.0, dirs_built=0, dir_fails=0,
                         concurrency_final=1, fails=[])

    @property
    def total(self) -> int:
        return self["ok"] + self["skip"] + self["fail"]

    @property
    def mbps(self) -> float:
        el = self["elapsed"] or 1e-6
        return self["bytes_uploaded"] / el / 1024 ** 2

    def summary(self) -> str:
        return ("成功 %d / 跳过 %d / 失败 %d，共 %.2f GB，%.1f MB/s，%.0fs"
                % (self["ok"], self["skip"], self["fail"],
                   self["bytes_uploaded"] / 1024 ** 3,
                   self.mbps, self["elapsed"]))


class BatchUploader:
    """把一棵本地目录树并发镜像到 115 的某个父目录下。

    用法::

        up = BatchUploader(client, cfg, paths, conc=4)
        result = up.upload_tree("/data/ALEXA 65", parent_cid="123456")
        print(result.summary())

    幂等：已完成的文件记在状态文件里，重跑自动跳过（零触网）。
    """

    def __init__(self, client, cfg: U.UploaderConfig | None = None,
                 paths: U.UploadPaths | None = None,
                 conc: int = 4, conc_floor: int = 1,
                 quiet: bool = False) -> None:
        self.client = client
        self.cfg = cfg or U.UploaderConfig()
        self.paths = paths or U.UploadPaths()
        self.conc = max(1, int(conc))
        self.conc_floor = max(1, int(conc_floor))
        self.quiet = quiet
        self.state = U.load_state(self.paths.state)

    # ---- 输出 -------------------------------------------------------------
    def _say(self, msg: str) -> None:
        if not self.quiet:
            print(msg)
            sys.stdout.flush()

    def _progress(self, rec: dict) -> None:
        with io.open(self.paths.progress, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()

    # ---- 主流程 -----------------------------------------------------------
    def upload_tree(self, local_root: str, parent_cid: str,
                    remote_name: str | None = None) -> BatchResult:
        """上传 ``local_root`` 整棵树到 ``parent_cid/remote_name``。"""
        result = BatchResult()
        t0 = time.time()

        model = remote_name or os.path.basename(os.path.normpath(local_root))
        items = U.walk_tree(local_root)
        total = len(items)
        tbytes = sum(x[2] for x in items)
        self._say("\n" + "=" * 72)
        self._say("【%s】%d 文件 / %.2f GB   并发=%d"
                  % (model, total, tbytes / 1024 ** 3, self.conc))
        if not items:
            self._say("  （空目录，跳过）")
            return result

        # ⭐ 建树【之前】必须确认「列目录可用」。
        #   限流期间 ensure_dir 每一步都会失败，最后退化成「退回父目录」，
        #   把整棵树的文件倒进机型根目录 —— 静默的数据放错位置，
        #   比「本轮什么都不做」坏得多。
        #   ⚠ 如果调用方已经 U.load_dir_cache()，这里会立刻短路返回（零触网）。
        if not U.wait_for_list(self.client, parent_cid, tag=model, cfg=self.cfg):
            if U.fatal_reason() is not None:
                raise U.AuthExpiredError(U.fatal_reason())
            raise U.ListBlockedError(
                "列目录接口在 %.0fs 内未恢复，放弃本轮（状态已落盘，下一轮续传）"
                % self.cfg.list_wait_max)

        root_cid = U.ensure_dir(self.client, parent_cid, model)

        # ---- 阶段 1：只按【目录】建树 ----
        # 关键：把每条 chain 的**所有前缀**展开去重，按层建目录。
        # 查询量从 O(文件数) 降到 O(目录数)，降幅 >99%。
        # ⚠ 只收集叶子链会漏掉中间层（例如只有 ('ARRIRAW','M003R00H') 时
        #   ('ARRIRAW',) 就没人建 → 子目录 'no-parent' 全灭）。
        dir_chains = set()
        for _, chain, _ in items:
            for d in range(1, len(chain) + 1):
                dir_chains.add(tuple(chain[:d]))
        dir_chains = sorted(dir_chains, key=len)   # 从浅到深

        dircache = {(): root_cid}
        build_fails = 0
        for chain in dir_chains:
            if chain in dircache:
                continue
            parent = dircache.get(chain[:-1])
            if parent is None:
                build_fails += 1
                continue
            try:
                dircache[chain] = U.ensure_dir(self.client, parent, chain[-1])
            except U.AuthExpiredError:
                raise                              # token 失效：立刻中止本轮
            except Exception as e:                 # noqa: BLE001 - 单目录失败不该中断
                build_fails += 1
                U.log({"t": time.strftime("%F %T"), "remote_name": model,
                       "dir_build_err": repr(e)[:250], "chain": list(chain)},
                      paths=self.paths)
        result["dirs_built"] = len(dircache) - 1
        result["dir_fails"] = build_fails
        self._say("  [建树] %d 个目录节点（去重后），失败 %d"
                  % (result["dirs_built"], build_fails))

        # ⚠ 建树大面积失败 = 列目录被限流/熔断，而不是「目录真的不存在」。
        #   这时绝不能带着残缺的 dircache 继续上传 —— 那会把文件倒进
        #   机型根目录（静默的数据放错位置）。直接放弃本轮，下一轮重试。
        if build_fails and U.list_blocked():
            if U.fatal_reason() is not None:
                raise U.AuthExpiredError(U.fatal_reason())
            raise U.ListBlockedError(
                "列目录处于熔断期（%d/%d 个目录建失败），放弃本轮以免放错位置；"
                "缓存已落盘，下一轮会从断点继续"
                % (build_fails, len(dir_chains)))

        # ⭐ 把建好的树落盘：下次重启就不必再逐层问 115（零触网建树）。
        n_saved = U.save_dir_cache(self.paths.dircache)
        if n_saved:
            self._say("  [建树] 目录缓存已落盘：%d 个 pid -> %s"
                      % (n_saved, self.paths.dircache))

        jobs = []
        for local, chain, size in items:
            pid = dircache.get(tuple(chain))
            if pid is None:
                continue
            jobs.append((local, pid, size))

        # ---- 阶段 2：并发上传（带自动降并发）----
        stats = {"ok": 0, "skip": 0, "fail": 0}
        uploaded_bytes = 0
        done_bytes = 0
        fails: list = []
        lock = threading.Lock()
        # ⚠ st_lock：多线程 worker 会同时写 state / 序列化 state，
        #   不加锁会抛 RuntimeError: dictionary changed size during iteration。
        st_lock = threading.RLock()
        n_done = [0]
        t_up = [t0]

        def do_one(job):
            nonlocal uploaded_bytes, done_bytes
            local, pid, size = job
            key = os.path.relpath(local, local_root)
            try:
                with st_lock:
                    r = U.upload_one(self.client, local, pid, self.state, key,
                                     cfg=self.cfg)
            except U.AuthExpiredError:
                raise                              # token 失效：中止整轮，交给外层重启
            except Exception as e:                 # noqa: BLE001 - 单文件失败要记下继续
                r = "fail:" + repr(e)[:120]

            need_save = False
            with lock:
                if r == "ok":
                    stats["ok"] += 1
                    if uploaded_bytes == 0:
                        t_up[0] = time.time()
                    uploaded_bytes += size
                elif r.startswith("skip"):
                    stats["skip"] += 1
                else:
                    stats["fail"] += 1
                    fails.append((local, r))
                if r == "ok" or r.startswith("skip"):
                    done_bytes += size
                n_done[0] += 1
                i = n_done[0]

                self._progress({
                    "t": time.strftime("%F %T"), "remote_name": model,
                    "file": os.path.basename(local), "size": size,
                    "result": r, "idx": i, "total": total,
                })
                if i % 25 == 0 or i == total:
                    el = max(time.time() - t_up[0], 1e-6)
                    sp = uploaded_bytes / el / 1024 ** 2 if uploaded_bytes else 0.0
                    self._say("  [%d/%d] %.2f/%.2f GB  新传 %.1f MB/s  "
                              "成功%d 跳过%d 失败%d"
                              % (i, total, done_bytes / 1024 ** 3,
                                 tbytes / 1024 ** 3, sp,
                                 stats["ok"], stats["skip"], stats["fail"]))
                    need_save = True
            # 存档放锁外：磁盘 IO 不该阻塞其它 worker 的统计更新
            if need_save:
                with st_lock:
                    U.save_state(self.state, self.paths.state)

        CHUNK_JOBS = max(self.conc * 4, 16)
        cur_conc = self.conc
        idx = 0
        while idx < len(jobs):
            batch = jobs[idx:idx + CHUNK_JOBS]
            idx += len(batch)
            before_ok, before_fail = stats["ok"], stats["fail"]
            if cur_conc <= 1:
                for job in batch:
                    do_one(job)
            else:
                with ThreadPoolExecutor(max_workers=cur_conc) as ex:
                    list(ex.map(do_one, batch))
            new_ok = stats["ok"] - before_ok
            new_fail = stats["fail"] - before_fail
            tried = new_ok + new_fail
            # 连续失败多半是链路/限流问题 → 降并发而不是硬扛
            if tried >= 4 and new_fail > tried * 0.5 and cur_conc > self.conc_floor:
                old = cur_conc
                cur_conc = max(self.conc_floor, cur_conc // 2)
                self._say("  [降并发] 本批 %d 失败 / %d 尝试 -> 并发 %d -> %d"
                          % (new_fail, tried, old, cur_conc))

        with st_lock:
            U.save_state(self.state, self.paths.state)

        result.update(ok=stats["ok"], skip=stats["skip"], fail=stats["fail"],
                      bytes_uploaded=uploaded_bytes, fails=fails,
                      concurrency_final=cur_conc, elapsed=time.time() - t0)
        self._say("  【完成】%s -> %s" % (model, result.summary()))
        return result
