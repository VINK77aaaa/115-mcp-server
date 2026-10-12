# -*- coding: utf-8 -*-
"""通过 GitHub API 推送整个目录（git 传输通道不通时的替代方案）。

流程：blob -> tree(带 base_tree) -> commit(带 parents) -> PATCH refs/heads/main
因为父提交就是远端当前 HEAD，所以是快进推送，不需要 --force。
"""
import base64
import json
import os
import subprocess
import sys

OWNER = "VINK77aaaa"
REPO = "115-mcp-server"
BASE_SHA = "8175310a5209dab6d8f1470eef6608db028c1b89"
ROOT = os.path.dirname(os.path.abspath(__file__))

EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "build", "dist",
                ".venv", "logs", ".workbuddy-ai"}
EXCLUDE_FILES = {"_filelist.txt"}

MESSAGE = """集成批量并发上传引擎（8.5 万文件 / 640 GB 实战沉淀）

把原本独立的 arri-115-uploader 项目融合进本仓库，作为 src 包内的正式模块。

新增模块：
- uploader.py    单文件上传核心：显式注入 Timeout(connect, read)、双指标看门狗、
                 115 限流识别（HTTP 200 + code 770004）、断点续传状态、os.scandir 扫描
- batch.py       批量编排：两阶段（主线程串行建树 -> 线程池并发上传）+ 自动降并发
- supervisor.py  崩溃重启监督器（指数退避）+ Windows 计划任务注册
- batch_upload.py  命令行入口 python -m mcp_115_server.batch_upload

新增 MCP 工具：
- batch_upload_plan  出计划，不触网
- batch_upload       真正上传，幂等、断点续传

新增配置（均可选，不影响原单文件上传）：
UPLOAD_ROOT / UPLOAD_PARENT_CID / UP_CONC / UPLOAD_WORKDIR / UP_CHUNK_MB /
UP_CONNECT_TIMEOUT / UP_READ_TIMEOUT / UP_WATCHDOG_SEC / UP_MAX_RETRY

修复上游缺陷：
- service.py 里 P115Client(**check_for_relogin=...) 在 p115client 版本不匹配时抛
  TypeError，会导致整个服务无法启动。新增 _build_client()，用 inspect.signature
  过滤参数，兼容不同版本。

新增文档与工具：
- docs/PITFALLS.md     17 个踩坑记录
- docs/BATCH-UPLOAD.md 批量上传完整指南
- bench/               三个并发/存储读取基准脚本
- tools/nic_speed.py   独立测本机网卡上行速率

实测：
- 60 个 MCP 工具（原 58 + batch_upload_plan + batch_upload）
- 90 个单元测试全部通过
- 并发吞吐（6.88 MB/文件）：1->8.9 MB/s，4->20.6（2.5x），8->30.2（3.4x）；
  16/24 无提升，32 报错。瓶颈是存储侧单线程读取上限（约 28 MB/s）
"""


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def gh(args, input_text=None):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True, input=input_text)
    if r.returncode != 0:
        raise SystemExit("gh 失败: %s\n%s\n%s" % (" ".join(args), r.stdout, r.stderr))
    return r.stdout


def collect():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            if rel in EXCLUDE_FILES:
                continue
            files.append((rel, full))
    return sorted(files)


def main():
    files = collect()
    print("待推送文件 %d 个\n" % len(files))

    tree_items = []
    for rel, full in files:
        with open(full, "rb") as f:
            raw = f.read()
        # 用 Git 计算与本地一致的 blob sha，便于事后校验
        blob_sha = sh(["git", "hash-object", "-w", "--stdin"],
                      input=raw.decode("utf-8", "surrogateescape")).stdout.strip()
        payload = {
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
        }
        out = gh(["api", "-X", "POST", "repos/%s/%s/git/blobs" % (OWNER, REPO),
                  "--input", "-"], input_text=json.dumps(payload))
        remote_sha = json.loads(out)["sha"]
        mark = "OK " if remote_sha == blob_sha else "DIFF"
        print("  %s  %-46s %s" % (mark, rel, remote_sha[:12]))
        tree_items.append({
            "path": rel,
            "mode": "100755" if os.access(full, os.X_OK) and rel.endswith((".ps1", ".cmd")) else "100644",
            "type": "blob",
            "sha": remote_sha,
        })

    tree_payload = {"base_tree": BASE_SHA, "tree": tree_items}
    tree = json.loads(gh(["api", "-X", "POST", "repos/%s/%s/git/trees" % (OWNER, REPO),
                          "--input", "-"], input_text=json.dumps(tree_payload)))
    print("\n新 tree: %s" % tree["sha"])

    commit_payload = {"message": MESSAGE, "tree": tree["sha"], "parents": [BASE_SHA]}
    commit = json.loads(gh(["api", "-X", "POST", "repos/%s/%s/git/commits" % (OWNER, REPO),
                            "--input", "-"], input_text=json.dumps(commit_payload)))
    print("新 commit: %s" % commit["sha"])

    gh(["api", "-X", "PATCH", "repos/%s/%s/git/refs/heads/main" % (OWNER, REPO),
        "--input", "-"],
       input_text=json.dumps({"sha": commit["sha"], "force": False}))
    print("\n✅ main 已更新到 %s" % commit["sha"][:12])


if __name__ == "__main__":
    main()
