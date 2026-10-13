# -*- coding: utf-8 -*-
"""针对 uploader 的「限流 / 多源 / 熔断 / 零触网建树」加固逻辑的单元测试。

这些测试**全部离线**：用一个假 client 精确模拟 115 的返回与异常，
所以既能验证行为，又不会真的去打 115 的接口（也就不可能把限流打出来）。

运行::

    python -m pytest tests/test_uploader_hardening.py -v
    # 或者
    python tests/test_uploader_hardening.py
"""
from __future__ import annotations

import os
import sys
import unittest

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
_PKG = os.path.join(_SRC, "mcp_115_server")
sys.path.insert(0, _SRC)


def _load_modules():
    """把 ``src/mcp_115_server`` 当成包加载，但**跳过 ``__init__.py``**。

    因为 ``__init__.py`` 会 ``from .server import create_server``，
    把 MCP 服务端依赖（fastmcp 等）一起拉进来 —— 而本测试只关心
    ``uploader`` / ``dircache`` / ``batch`` 这三个纯逻辑模块，它们自身
    不依赖任何服务端库。这样测试在「只装了 p115client」的环境里也能跑。
    """
    import importlib.util
    import types

    if "mcp_115_server" not in sys.modules:
        pkg = types.ModuleType("mcp_115_server")
        pkg.__path__ = [_PKG]
        sys.modules["mcp_115_server"] = pkg
    out = {}
    for name in ("uploader", "dircache", "batch"):
        full = "mcp_115_server." + name
        spec = importlib.util.spec_from_file_location(full, os.path.join(_PKG, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        setattr(sys.modules["mcp_115_server"], name, mod)
        spec.loader.exec_module(mod)
        out[name] = mod
    return out


_M = _load_modules()
U, DC, B = _M["uploader"], _M["dircache"], _M["batch"]


class FakeClient:
    """可编程的假 P115Client：分别给两个列目录端点 + search + mkdir 装 handler。"""

    def __init__(self, aps=None, open_=None, search=None, mkdir=None):
        self._aps, self._open = aps, open_
        self._search, self._mkdir = search, mkdir
        self.n_aps = self.n_open = self.n_search = self.n_mkdir = 0
        self.n_upload = 0
        self.mkdir_calls: list = []
        self.uploads: list = []

    def upload_file(self, local, pid, filesize=None, partsize=None,
                    reporthook=None, timeout=None):
        self.n_upload += 1
        self.uploads.append((os.path.basename(local), pid))
        if reporthook:                           # 让看门狗看到「有进展」
            reporthook(0, 1, filesize or 0)
        return {"state": True}

    def fs_files_aps(self, payload, timeout=None):
        self.n_aps += 1
        return self._aps(payload) if callable(self._aps) else self._aps

    def fs_files_open(self, payload, timeout=None):
        self.n_open += 1
        return self._open(payload) if callable(self._open) else self._open

    def fs_search(self, payload, timeout=None):
        self.n_search += 1
        return self._search(payload) if callable(self._search) else self._search

    def fs_mkdir(self, name, pid, timeout=None):
        self.n_mkdir += 1
        self.mkdir_calls.append((name, pid))
        return self._mkdir(name, pid) if callable(self._mkdir) else self._mkdir


def ok(data):
    return {"state": True, "data": data}


def rate_limited():
    return {"state": False, "code": 770004,
            "message": "已达到当前访问上限，请稍后再试", "data": []}


class Base(unittest.TestCase):
    def setUp(self):
        # 重置所有模块级状态，避免测试互相污染
        U._SRC_COOLDOWN.clear()
        U._LIST_BLOCK_UNTIL[0] = 0.0
        U._FATAL[0] = None
        U._DIR_CACHE.clear()
        U._DIR_INFLIGHT.clear()
        U._DIR_FAIL.clear()
        self.logs: list = []
        self._orig_log = U.log
        U.log = self.logs.append                 # 不打日志文件，改收集
        self._env_backup = dict(os.environ)
        os.environ["UP_LIST_RETRY"] = "1"        # 测试里不等待退避
        os.environ["UP_SRC_COOLDOWN"] = "600"
        os.environ["UP_DIR_FAIL_TTL"] = "60"

    def tearDown(self):
        U.log = self._orig_log
        os.environ.clear()
        os.environ.update(self._env_backup)

    def warns(self, kind):
        return [r for r in self.logs if r.get("warn") == kind]


class TestMultiSource(Base):
    def test_aps_405_enters_cooldown_and_open_takes_over(self):
        """aps 被 WAF 拦（405）后应立刻改用 open，且之后不再去撞 aps。"""
        def aps(_p):
            raise RuntimeError("HTTP Error 405: Method Not Allowed")

        c = FakeClient(aps=aps, open_=ok([{"n": "A", "fid": "a1", "fc": 0}]))
        kids = U.remote_children(c, "root")

        self.assertEqual(kids, {"A": ("a1", None)})
        self.assertEqual(c.n_aps, 1, "aps 只该被撞一次")
        self.assertEqual(c.n_open, 1)
        self.assertTrue(self.warns("src-cooldown"), "应记录单源冷却")

        # 第二次：aps 在冷却期，应被完全跳过
        c.n_aps = c.n_open = 0
        U.remote_children(c, "root2")
        self.assertEqual(c.n_aps, 0, "冷却期内不该再碰 aps")
        self.assertEqual(c.n_open, 1)

    def test_both_rate_limited_trips_global_block(self):
        """两个源都 770004 → 全局熔断；此后 cached_children 零触网返回空表。"""
        c = FakeClient(aps=lambda _p: rate_limited(), open_=lambda _p: rate_limited())
        with self.assertRaises(Exception):
            U.remote_children(c, "root")

        self.assertTrue(U.list_blocked(), "应触发全局熔断")
        self.assertGreater(U.list_block_left(), 0)
        self.assertTrue(self.warns("list-block-tripped"))
        n_before = c.n_aps + c.n_open

        # 熔断期内：任何 pid 都不再触网，直接拿到空表（调用方会走「直接传」降级）
        for pid in ("p1", "p2", "p3"):
            self.assertEqual(U.cached_children(c, pid), {})
        self.assertEqual(c.n_aps + c.n_open, n_before, "熔断期内必须零触网")

    def test_single_source_rate_limit_does_not_trip_block(self):
        """只有一个源限流、另一个源正常时，绝不能熔断整个任务。"""
        c = FakeClient(
            aps=lambda _p: rate_limited(),
            open_=ok([{"n": "A", "fid": "a1", "fc": 0}]),
        )
        kids = U.remote_children(c, "root")
        self.assertEqual(kids, {"A": ("a1", None)})
        self.assertFalse(U.list_blocked(), "主源可用就不该熔断")

    def test_auth_expired_from_all_sources_is_fatal(self):
        """两个源都报 token 失效 → 记为致命；upload_one 直接抛 AuthExpiredError。"""
        def dead(_p):
            raise RuntimeError("code=40140125 msg=access_token 无效")

        c = FakeClient(aps=dead, open_=dead)
        with self.assertRaises(Exception):
            U.remote_children(c, "root")

        self.assertIsNotNone(U.fatal_reason())
        self.assertTrue(self.warns("auth-expired"))

        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x")
            path = f.name
        try:
            with self.assertRaises(U.AuthExpiredError):
                U.upload_one(c, path, "root", {}, "k")
        finally:
            os.unlink(path)

    def test_pagination_guard_stops_on_offset_wrap(self):
        """aps 的 offset 超过上限会「从头再来」—— 必须有防死循环判据。"""
        page = [{"n": "f%d" % i, "fid": str(i), "fc": 1, "s": 10}
                for i in range(1000)]

        c = FakeClient(aps=lambda _p: ok(page), open_=ok(page))
        kids = U.remote_children(c, "root")

        self.assertEqual(len(kids), 1000)
        self.assertEqual(c.n_aps, 2, "第二页没带来新名字就该停（2 次调用）")
        self.assertTrue(self.warns("page-no-progress"))


class TestWaitForList(Base):
    def test_short_circuit_when_root_already_cached(self):
        """根目录已在缓存里（说明 dircache 已喂进来）→ 立刻返回，零触网。"""
        c = FakeClient(aps=ok([]), open_=ok([]))
        U._DIR_CACHE["ROOT"] = {}
        self.assertTrue(U.wait_for_list(c, "ROOT"))
        self.assertEqual(c.n_aps + c.n_open, 0, "不该发任何请求")

    def test_polls_then_succeeds_and_clears_block(self):
        """探测成功要能提前解除熔断。"""
        U.trip_list_block("pretend-limited")
        self.assertTrue(U.list_blocked())

        c = FakeClient(aps=ok([]), open_=ok([{"n": "x", "fid": "1", "fc": 1, "s": 5}]))
        self.assertTrue(U.wait_for_list(c, "ROOT", max_wait=5))
        self.assertFalse(U.list_blocked(), "探测成功应解除熔断")
        self.assertTrue(self.warns("list-block-cleared"))


class TestDirCache(Base):
    def test_save_load_roundtrip(self):
        import tempfile
        U._DIR_CACHE["p1"] = {"A": ("a1", None), "f.ari": ("f1", 123)}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "_dircache.json")
            self.assertEqual(U.save_dir_cache(path), 1)
            U._DIR_CACHE.clear()
            self.assertEqual(U.load_dir_cache(path), 1)
        self.assertEqual(U._DIR_CACHE["p1"], {"A": ("a1", None), "f.ari": ("f1", 123)})

    def test_build_from_search_reconstructs_tree_by_pid(self):
        """fs_search 的目录项同时带 cid 与 pid → 可自顶向下重建整棵树。"""
        tree = [
            {"n": "A", "cid": "a1", "pid": "R", "fc": 0},
            {"n": "B", "cid": "b1", "pid": "R", "fc": 0},
            {"n": "C", "cid": "c1", "pid": "a1", "fc": 0},
            # 已存在的文件：也会被搜索命中（真实场景里同名前缀的素材文件与目录
            # 往往一起被搜到）→ 顺带记进缓存，让 upload_one 直接 skip，省一次握手
            {"n": "A-f.ari", "fid": "f1", "cid": "a1", "fc": 1, "s": 999},
        ]
        c = FakeClient(search=lambda p: ok([it for it in tree
                                            if it["n"].startswith(p["search_value"])]))
        cache, report = DC.build_dir_cache(
            c, {"M": "R"},
            local_dirs={"M": [["A"], ["B"], ["A", "C"]]},
            gap=0, mkdir_missing=True,
        )
        self.assertEqual(report[0]["dirs_resolved"], 3)
        self.assertEqual(report[0]["dirs_created"], 0)
        self.assertEqual(report[0]["file_hits"], 1)
        self.assertEqual(cache["R"]["A"], ["a1", None])
        self.assertEqual(cache["R"]["B"], ["b1", None])
        self.assertEqual(cache["a1"]["C"], ["c1", None])
        self.assertEqual(cache["a1"]["A-f.ari"], ["f1", 999])
        self.assertIn("c1", cache, "叶子目录也要有空条目，否则上传时又得在线查")

    def test_build_creates_missing_dirs_top_down(self):
        """本地有、115 没有的目录要自顶向下补建（父必须先拿到 cid）。"""
        tree = [{"n": "A", "cid": "a1", "pid": "R", "fc": 0}]
        c = FakeClient(
            search=lambda p: ok([it for it in tree
                                 if it["n"].startswith(p["search_value"])]),
            mkdir=lambda name, pid: {"cid": "new-" + name, "state": True},
        )
        cache, report = DC.build_dir_cache(
            c, {"M": "R"},
            local_dirs={"M": [["A"], ["A", "D"], ["A", "D", "E"]]},
            gap=0,
        )
        self.assertEqual(report[0]["dirs_created"], 2)
        self.assertEqual(c.mkdir_calls, [("D", "a1"), ("E", "new-D")],
                         "必须自顶向下：E 的父是刚建的 D")
        self.assertEqual(cache["a1"]["D"], ["new-D", None])
        self.assertEqual(cache["new-D"]["E"], ["new-E", None])

    def test_search_terms_compression(self):
        names = ["L001C026_140110_R6MS", "L001C027_140110_R6MS", "abc", "abcd"]
        self.assertEqual(DC.prefix_terms(names), ["L001", "abc", "abcd"])


class TestBatchUploadTree(Base):
    """端到端（仍离线）跑一遍 BatchUploader.upload_tree，验证串起来的加固逻辑。"""

    def _make_material(self, tmp):
        os.makedirs(os.path.join(tmp, "sub1", "s2"))
        for p, data in (("sub1/f1.bin", b"a" * 100),
                        ("sub1/s2/f2.bin", b"b" * 200)):
            with open(os.path.join(tmp, p), "wb") as f:
                f.write(data)
        return tmp

    def test_build_then_upload_and_persist_dircache(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_material(os.path.join(tmp, "mat"))
            work = os.path.join(tmp, "work")
            os.makedirs(work)

            made = {"n": 0}

            def mkdir(name, pid):
                made["n"] += 1
                return {"cid": "c-%s" % name, "state": True}

            c = FakeClient(open_=ok([]), mkdir=mkdir)   # 115 上什么都没有
            paths = U.UploadPaths(work)
            up = B.BatchUploader(c, U.UploaderConfig(), paths, conc=1, quiet=True)
            r = up.upload_tree(root, "PARENT", "M")

            # M 由 upload_tree 单独 ensure_dir，dirs_built 只数它下面的子目录
            self.assertEqual(r["dirs_built"], 2, "sub1 / sub1/s2")
            self.assertEqual(r["dir_fails"], 0)
            self.assertEqual(r["ok"], 2)
            self.assertEqual(r["fail"], 0)
            self.assertEqual(c.n_upload, 2)
            self.assertEqual(sorted(c.uploads), [("f1.bin", "c-sub1"),
                                                 ("f2.bin", "c-s2")])
            # ⭐ 建完树必须落盘，下次重启才能零触网
            self.assertTrue(os.path.exists(paths.dircache))
            self.assertGreater(len(DC.read_cache(paths.dircache)), 0)

    def test_aborts_when_list_is_rate_limited(self):
        """列目录被限流 → 宁可本轮什么都不做，也不要把文件倒进根目录。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_material(os.path.join(tmp, "mat"))
            work = os.path.join(tmp, "work")
            os.makedirs(work)

            c = FakeClient(aps=lambda _p: rate_limited(),
                           open_=lambda _p: rate_limited())
            cfg = U.UploaderConfig(list_retry=1, list_wait_max=0)
            up = B.BatchUploader(c, cfg, U.UploadPaths(work), conc=1, quiet=True)

            with self.assertRaises(U.ListBlockedError):
                up.upload_tree(root, "PARENT", "M")
            self.assertEqual(c.n_upload, 0, "绝不能带着残缺的树去上传")
            self.assertTrue(self.warns("list-wait-timeout"))

    def test_aborts_on_auth_expired(self):
        def dead(_p):
            raise RuntimeError("code=40140125 msg=access_token 无效")

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_material(os.path.join(tmp, "mat"))
            work = os.path.join(tmp, "work")
            os.makedirs(work)

            c = FakeClient(aps=dead, open_=dead)
            cfg = U.UploaderConfig(list_retry=1, list_wait_max=0)
            up = B.BatchUploader(c, cfg, U.UploadPaths(work), conc=1, quiet=True)

            with self.assertRaises(U.AuthExpiredError):
                up.upload_tree(root, "PARENT", "M")
            self.assertEqual(c.n_upload, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
