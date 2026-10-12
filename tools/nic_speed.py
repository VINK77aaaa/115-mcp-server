# -*- coding: utf-8 -*-
"""本机网卡上行速率采样（零依赖，Windows IPHLPAPI）

面板「实时速度」用它做数据源：
  - 115 侧的落盘字节是「事后」数据，滞后且粒度粗（整文件级别），
    所以用它算瞬时速度会长期显示 0 或 --。
  - 网卡计数器是「当下」数据，毫秒级真实反映上传流量。
"""
import ctypes, time


class _MIB_IFROW(ctypes.Structure):
    _fields_ = [
        ('wszName', ctypes.c_wchar * 256), ('dwIndex', ctypes.c_uint),
        ('dwType', ctypes.c_uint), ('dwMtu', ctypes.c_uint),
        ('dwSpeed', ctypes.c_uint), ('dwPhysAddrLen', ctypes.c_uint),
        ('bPhysAddr', ctypes.c_ubyte * 8), ('dwAdminStatus', ctypes.c_uint),
        ('dwOperStatus', ctypes.c_uint), ('dwLastChange', ctypes.c_uint),
        ('dwInOctets', ctypes.c_uint), ('dwInUcastPkts', ctypes.c_uint),
        ('dwInNUcastPkts', ctypes.c_uint), ('dwInDiscards', ctypes.c_uint),
        ('dwInErrors', ctypes.c_uint), ('dwInUnknownProtos', ctypes.c_uint),
        ('dwOutOctets', ctypes.c_uint), ('dwOutUcastPkts', ctypes.c_uint),
        ('dwOutNUcastPkts', ctypes.c_uint), ('dwOutDiscards', ctypes.c_uint),
        ('dwOutErrors', ctypes.c_uint), ('dwOutQLen', ctypes.c_uint),
        ('dwDescrLen', ctypes.c_uint), ('bDescr', ctypes.c_char * 256),
    ]


def _rows():
    ip = ctypes.windll.iphlpapi
    n = ctypes.c_ulong(0)
    ip.GetNumberOfInterfaces(ctypes.byref(n))
    out = []
    for i in range(n.value):
        r = _MIB_IFROW()
        r.dwIndex = i
        if ip.GetIfEntry(ctypes.byref(r)) == 0:
            out.append((r.dwIndex, r.dwOutOctets, r.dwOperStatus, r.dwSpeed))
    return out


def measure(seconds=3.0):
    """返回 (总上行字节/秒, 各网卡明细)。"""
    a = _rows()
    t0 = time.time()
    time.sleep(seconds)
    b = _rows()
    dt = max(time.time() - t0, 1e-6)
    da = {i: (x, st, sp) for i, x, st, sp in a}
    total = 0
    detail = []
    for i, x, st, sp in b:
        prev = da.get(i, (0, 0, 0))[0]
        d = x - prev
        if d < 0:
            d = 0
        total += d
        if st == 1 and d / dt > 1024:
            detail.append({'idx': i, 'bps': d / dt, 'link_mbps': sp})
    return total / dt, detail


if __name__ == '__main__':
    sp, det = measure(3.0)
    print('上行 %.2f MB/s' % (sp / 1024 / 1024))
    for d in det:
        print('  nic%d  %.2f MB/s  链路 %.0f Mbps'
              % (d['idx'], d['bps'] / 1024 / 1024, d['link_mbps']))
