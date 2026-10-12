# -*- coding: utf-8 -*-
"""严格版并发测试：每个档位用【互不相同】的文件，全部首次上传，杜绝秒传污染。

关键：判断是否真的走了网络 —— 通过耗时合理性校验
（存储读取上限 ~28-45 MB/s，若吞吐远超此值则必然含秒传）。

⚠ 本脚本会**真实上传**文件到 115 测试目录，产生真实流量与占用。

用法：
    UPLOAD_ROOT=/path/to/material BENCH_MODEL="ALEXA SXT" python bench/conc_test.py
"""
import os, sys, time, json, threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'src'))
sys.path.insert(0, os.path.join(HERE, '..'))
from mcp_115_server import uploader as U

MODEL = os.environ.get('BENCH_MODEL', 'ALEXA SXT')
ROOT = os.path.expanduser(os.environ.get('UPLOAD_ROOT', os.environ.get('ARRI_ROOT', './material')))
PARENT_CID = os.environ.get('UPLOAD_PARENT_CID', os.environ.get('P115_PARENT_CID', '0'))
TEST_DIRNAME = '_wb_conc_v3'
PER = 60
LEVELS = (8, 16, 24, 32)


def main():
    cookie = os.environ.get('P115_COOKIES') or os.environ.get('P115_COOKIES_PATH')
    if not cookie:
        raise SystemExit('请设置 P115_COOKIES 或 P115_COOKIES_PATH')
    c = U.client_from_cookie(cookie)
    cfg = U.UploaderConfig()
    test_cid = U.ensure_dir(c, PARENT_CID, TEST_DIRNAME)
    print('测试目录 cid =', test_cid); sys.stdout.flush()

    items = U.walk_tree(os.path.join(ROOT, MODEL))
    band = [x for x in items if 6 * 1024**2 < x[2] < 8 * 1024**2]
    need = PER * len(LEVELS)
    print('样本池 %d 个（需 %d），每个 %.2f MB' % (len(band), need, band[0][2] / 1024**2))
    if len(band) < need:
        print('样本不足'); return

    results = []
    cursor = 0
    for conc in LEVELS:
        batch = band[cursor:cursor + PER]     # ← 各档位取【不同】文件
        cursor += PER
        sub = U.ensure_dir(c, test_cid, 'c%d' % conc)
        st = {}
        t0 = time.time(); db = 0; nok = 0
        lock = threading.Lock()

        def one(it):
            nonlocal db, nok
            local, chain, size = it
            try:
                r = U.upload_one(c, local, sub, st,
                                 '%d/%s' % (conc, os.path.basename(local)), cfg=cfg)
            except Exception:
                r = 'err'
            with lock:
                if r == 'ok':
                    db += size; nok += 1

        if conc == 1:
            for it in batch:
                one(it)
        else:
            with ThreadPoolExecutor(max_workers=conc) as ex:
                list(ex.map(one, batch))

        el = time.time() - t0
        mbps = db / el / 1024**2
        # 判定是否可信：超过 NAS 读取上限（~45MB/s）说明含秒传
        trust = 'OK' if mbps < 45 else '!! 疑似秒传污染'
        results.append({'conc': conc, 'sec': round(el, 1), 'files': nok,
                        'fps': round(nok / el, 2), 'mbps': round(mbps, 1), 'trust': trust})
        print('conc=%-2d  %d 文件 %.1fs  %.2f 文件/秒  %.1f MB/s  [%s]'
              % (conc, nok, el, nok / el, mbps, trust))
        sys.stdout.flush()

    print()
    base = [r for r in results if r['conc'] == 1][0]['mbps']
    for r in results:
        print('  conc=%-2d  %.1f MB/s  加速 %.2fx  %s'
              % (r['conc'], r['mbps'], r['mbps'] / base, r['trust']))

    try:
        c.fs_delete(test_cid); print('\n已删除测试目录')
    except Exception as e:
        print('清理失败: %r' % e)
    json.dump(results, open(os.path.join(HERE, '_conc_v3.json'), 'w'), indent=2)


if __name__ == '__main__':
    main()
