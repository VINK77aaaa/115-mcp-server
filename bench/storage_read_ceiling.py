# -*- coding: utf-8 -*-
"""测存储多线程读取上限：同样大小的文件，不同线程数读满带宽。

用途：判断批量上传的真实瓶颈在**存储侧**还是网络侧。
若读取上限 ~28 MB/s 而上传并发 8 已到 30 MB/s，说明瓶颈在存储，
再加并发无益。

⚠ 反缓存注意：**同一批文件重复读会被 OS 文件缓存命中**，测出
1000+ MB/s 的假数据。本脚本每档取不同切片来缓解。

用法：
    UPLOAD_ROOT=/path/to/material BENCH_MODEL="ALEXA SXT" python bench/storage_read_ceiling.py
"""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'src'))
sys.path.insert(0, os.path.join(HERE, '..'))
from mcp_115_server import uploader as U

MODEL = os.environ.get('BENCH_MODEL', 'ALEXA SXT')
ROOT = os.path.expanduser(os.environ.get('UPLOAD_ROOT', os.environ.get('ARRI_ROOT', './material')))
items = U.walk_tree(os.path.join(ROOT, MODEL))
band = [x for x in items if 6*1024**2 < x[2] < 8*1024**2]
NEED = 24
print('样本 %d 个, 每个 %.2f MB' % (len(band), band[0][2]/1024**2))

def read_one(it):
    p = it[0]; n = 0
    with open(p, 'rb') as f:
        while True:
            b = f.read(1024*1024)
            if not b: break
            n += len(b)
    return n

cursor = 0
for conc in (1, 2, 4, 8, 12):
    # 每档取不同切片，避免被 OS 文件缓存命中（否则速度虚高）
    batch = band[cursor:cursor + NEED]
    cursor += NEED
    if len(batch) < NEED:
        batch = band[:NEED]
    t0 = time.time(); tot = 0
    if conc == 1:
        for it in batch: tot += read_one(it)
    else:
        with ThreadPoolExecutor(max_workers=conc) as ex:
            for n in ex.map(read_one, batch): tot += n
    el = time.time() - t0
    mbps = tot/el/1024**2
    hint = '  ← 疑似缓存命中' if mbps > 200 else ''
    print('  读并发 %-2d  %d 文件 %.1fs  %.2f 文件/秒  %.1f MB/s%s'
          % (conc, len(batch), el, len(batch)/el, mbps, hint))
    sys.stdout.flush()
