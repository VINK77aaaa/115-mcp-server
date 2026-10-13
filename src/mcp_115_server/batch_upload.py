# -*- coding: utf-8 -*-
"""批量上传命令行入口。

由监督器 / 计划任务拉起，也可手动运行。

认证复用 MCP 服务的配置：
    P115_COOKIES         cookie 字符串
    P115_COOKIES_PATH    或 cookie 文件路径

上传配置：
    ARRI_ROOT            素材根目录（默认 ./material）
    P115_PARENT_CID      115 目标父目录 id（默认 0 = 根目录）
    UP_CONC              多文件并发度（默认 4）

用法：
    python -m mcp_115_server.batch_upload --root /data/material --parent-cid 123
    python -m mcp_115_server.batch_upload --root /data/material --only "ALEXA 65"

⭐ 零触网建树（强烈建议）
    逐层列目录去建树，会把 115 的列目录接口打成限流（连打约 30 次即触发，
    恢复要 20 分钟量级）。正确做法是先用 ``fs_search`` 把整棵树 dump 成
    ``_dircache.json``，之后上传器建树/预取全部纯内存命中：

        python -m mcp_115_server.batch_upload --root /data/material \
            --parent-cid 123 --rebuild-dircache      # 一次性 dump（几十次搜索）

    随后正常跑（不带该参数）即可 —— 启动时会自动 ``load_dir_cache``。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import uploader as U
from .batch import BatchUploader
from .config import Settings


def _client_from_settings(settings: Settings):
    """按 MCP 服务的同一套规则构造 client（复用 cookie 配置）。"""
    source: object | None = settings.p115_cookies or settings.cookies_path
    if source is None:
        raise SystemExit(
            "未配置 115 认证。请设置 P115_COOKIES 或 P115_COOKIES_PATH。")
    from p115client import P115Client

    if isinstance(source, os.PathLike) or (
            isinstance(source, str) and os.path.exists(source)
            and len(source) < 4096):
        cookie = open(source, encoding="utf-8").read().strip()
    else:
        cookie = str(source)
    return P115Client(cookie, app="web")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mcp_115_server.batch_upload",
                                 description="115 批量并发上传（断点续传）")
    ap.add_argument("--root", default=os.environ.get("ARRI_ROOT", "./material"),
                    help="素材根目录（其下每个子目录作为一个远端目录）")
    ap.add_argument("--parent-cid", default=os.environ.get("P115_PARENT_CID", "0"),
                    help="115 目标父目录 id（默认 0 = 根目录）")
    ap.add_argument("--only", action="append", default=None,
                    help="只上传指定子目录名（可多次指定）")
    ap.add_argument("--conc", type=int, default=int(os.environ.get("UP_CONC", 4)),
                    help="多文件并发度（默认 4）")
    ap.add_argument("--workdir", default=None,
                    help="状态文件与日志目录（默认当前目录）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列计划，不真正上传")
    ap.add_argument("--rebuild-dircache", action="store_true",
                    help="先用 fs_search 把 115 上的目录树 dump 成 _dircache.json"
                         "（几十次搜索，代替 O(目录数) 次列目录），然后退出")
    args = ap.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isdir(root):
        raise SystemExit("素材根目录不存在：%s" % root)

    settings = Settings()
    client = _client_from_settings(settings)
    cfg = U.UploaderConfig()
    paths = U.UploadPaths(args.workdir)

    names = args.only or sorted(
        d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    if not names:
        raise SystemExit("没有找到可上传的子目录：%s" % root)

    print("目标父目录 cid : %s" % args.parent_cid)
    print("并发          : %d" % args.conc)
    print("状态文件      : %s" % paths.state)
    print("目录缓存      : %s" % paths.dircache)
    print("待上传子目录  : %d 个" % len(names))

    # ---- 可选：一次性 dump 115 上的目录树（零触网建树的前提）----
    if args.rebuild_dircache:
        from . import dircache as DC

        # 顶层机型 cid 直接由「列一次父目录」得到，不需要硬编码
        tops = {n: cid for n, (cid, size) in
                U.remote_children(client, args.parent_cid).items()
                if size is None}
        roots = {n: tops[n] for n in names if n in tops}
        missing = [n for n in names if n not in tops]
        if missing:
            print("  ! 115 上还没有这些顶层目录（首次上传时自动创建）：%s"
                  % ", ".join(missing))
        local_dirs = DC.scan_local_dirs(root, models=list(roots))
        cache, report = DC.build_dir_cache(
            client, roots, local_dirs=local_dirs,
            log=lambda rec: U.log(rec, paths=paths))
        for r in report:
            print("  %-28s 目录 %3d/%3d  新建 %2d  搜索 %2d 次  命中 %3d 目录/%4d 文件  %.1fs"
                  % (r["model"], r["dirs_resolved"], r["dirs_local"],
                     r["dirs_created"], r["terms"], r["dir_hits"],
                     r["file_hits"], r["seconds"]))
        DC.write_cache(paths.dircache, cache)
        print("  ✓ 目录缓存已写入 %s（%d 个 pid）" % (paths.dircache, len(cache)))
        return 0

    # ---- 启动时载入目录缓存：命中则建树/预取全程零触网 ----
    n_cached = U.load_dir_cache(paths.dircache)
    if n_cached:
        print("  ✓ 已载入目录缓存：%d 个 pid（建树将零触网）" % n_cached)
    else:
        print("  ! 没有目录缓存；建树会逐层列目录，可能触发限流。"
              "建议先跑一次 --rebuild-dircache。")

    if args.dry_run:
        for n in names:
            items = U.walk_tree(os.path.join(root, n))
            print("  [dry] %s  %d 文件 / %.2f GB"
                  % (n, len(items), sum(x[2] for x in items) / 1024 ** 3))
        return 0

    up = BatchUploader(client, cfg, paths, conc=args.conc)
    grand = {"ok": 0, "skip": 0, "fail": 0}
    T0 = time.time()
    for n in names:
        local = os.path.join(root, n)
        if not os.path.isdir(local):
            print("  ! 跳过（不是目录）：%s" % local)
            continue
        try:
            r = up.upload_tree(local, args.parent_cid, n)
        except U.AuthExpiredError as e:
            # token 失效 → 非零退出，让监督器用【全新 client】重启。
            # 因为状态与目录缓存都已落盘，重启是幂等且极廉价的。
            print("\n✗ 115 access_token 失效，放弃本轮：%s" % str(e)[:200])
            print("  （状态已落盘，重启后自动续传；若反复出现，"
                  "请确认没有其它进程同时在用同一 cookie）")
            return 2
        except U.ListBlockedError as e:
            print("\n✗ 列目录被限流/熔断，放弃本轮：%s" % str(e)[:200])
            print("  （下一轮会自动重试；也可先跑 --rebuild-dircache）")
            return 3
        for k in grand:
            grand[k] += r[k]

    print("\n" + "#" * 72)
    print("全部完成：成功 %d / 跳过 %d / 失败 %d，耗时 %.2f 小时"
          % (grand["ok"], grand["skip"], grand["fail"],
             (time.time() - T0) / 3600))
    # 有失败时以非 0 退出 → 监督器会自动重试（幂等，已传的会跳过）
    return 1 if grand["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
