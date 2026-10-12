# -*- coding: utf-8 -*-
"""真实并发上传探针：取 120 个文件，分别用 conc=1 和 conc=8
真实上传到云端测试目录，测量真实吞吐（全部首次上传，无秒传）。

⚠ 会真实上传并占用云端空间。

用法：
    UPLOAD_ROOT=/path/to/material BENCH_MODEL="ALEXA SXT" python bench/conc_test_real.py
"""
import os, sys, time, threading
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'src'))
sys.path.insert(0, os.path.join(HERE, '..'))
from mcp_115_server import uploader as U

cookie = os.environ.get('P115_COOKIES') or os.environ.get('P115_COOKIES_PATH')
if not cookie:
    raise SystemExit('请设置 P115_COOKIES 或 P115_COOKIES_PATH')
c = U.client_from_cookie(cookie)
cfg = U.UploaderConfig()
MODEL = os.environ.get('BENCH_MODEL', 'ALEXA SXT')
ROOT = os.path.expanduser(os.environ.get('UPLOAD_ROOT', os.environ.get('ARRI_ROOT', './material')))
PARENT_CID = os.environ.get('UPLOAD_PARENT_CID', os.environ.get('P115_PARENT_CID', '0'))
TEST = '_wb_real_conc'
test_cid = U.ensure_dir(c, PARENT_CID, TEST)
print('测试目录 cid =', test_cid)

items = U.walk_tree(os.path.join(ROOT, MODEL))
band = [x for x in items if 6*1024**2 < x[2] < 8*1024**2]
PER = 60
print('样本池 %d, 每个 %.2f MB' % (len(band), band[0][2]/1024**2))

st = {}
res = []
cursor = 0
for conc in (1, 8):
    batch = band[cursor:cursor+PER]; cursor += PER
    sub = U.ensure_dir(c, test_cid, 'c%d' % conc)
    lock = threading.Lock(); acc = {'n':0, 'b':0}
    def one(it):
        local, _, size = it
        try:
            r = U.upload_one(c, local, sub, st, 'rc/%s' % os.path.basename(local), cfg=cfg)
        except Exception as e:
            r = 'err:' + repr(e)[:80]
        with lock:
            if r == 'ok': acc['n'] += 1; acc['b'] += size
            elif not r.startswith('skip'):
                print('   !! %s -> %s' % (os.path.basename(local), r[:100]))
    t0 = time.time()
    if conc == 1:
        for it in batch: one(it)
    else:
        with ThreadPoolExecutor(max_workers=conc) as ex:
            list(ex.map(one, batch))
    el = time.time() - t0
    mbps = acc['b']/el/1024**2
    trust = 'OK' if mbps < 45 else '!! 超存储读取上限'
    print('  conc=%-2d  真实新传 %d 文件  %.1fs  %.2f 文件/秒  %.1f MB/s  [%s]'
          % (conc, acc['n'], el, acc['n']/el, mbps, trust))
    res.append((conc, mbps)); sys.stdout.flush()

print()
b = res[0][1]
for conc, m in res:
    print('  conc=%-2d  %.1f MB/s  加速 %.2fx' % (conc, m, m/b if b else 0))
