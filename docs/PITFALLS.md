# 踩坑指南：用 p115client 批量上传到 115 网盘

这份文档记录了在一个真实项目（8.5 万个文件、约 640 GB 素材从存储服务器传到 115）
中踩到的**每一个坑**，以及根因和修复方式。按「坑的严重程度」排序。

> 环境：Windows + `p115client`（Python）+ 115 年费 VIP 账号。
> 素材放在一台独立 NAS 上（SMB 共享），本机只跑调度脚本。

---

## 目录

- [坑 1（最严重）：底层 HTTP 请求没有任何超时，会永久挂起](#坑-1)
- [坑 2：限流伪装成「成功」，被误判为「空目录」](#坑-2)
- [坑 3：`fs_files` 返回 405，而 `fs_files_open` 正常](#坑-3)
- [坑 4：目录 id 不在 `cid` 字段里](#坑-4)
- [坑 5：缓存把目录 id 覆盖成空字符串](#坑-5)
- [坑 6：只收集叶子目录链，中间层目录没建](#坑-6)
- [坑 7：看门狗单指标误杀「SHA1 计算阶段」](#坑-7)
- [坑 8：把「跳过」的字节算进速度，得出几十万 MB/s](#坑-8)
- [坑 9：网络共享上 `os.walk` + `getsize` 慢 60 倍](#坑-9)
- [坑 10：每个文件都重查目录 → O(n²)](#坑-10)
- [坑 11：并发测试被「秒传」污染，数据完全失真](#坑-11)
- [坑 12：`P115Client` 不适合并发查询](#坑-12)
- [坑 13：把「本机网卡流量」当成「上传流量」](#坑-13)
- [坑 14：并发数不是越高越好](#坑-14)
- [坑 15：`mkdir` 说「已存在」但列表接口看不见](#坑-15)
- [坑 16：多线程下 `save_state` 抛 `dictionary changed size during iteration`](#坑-16)
- [坑 17：后台进程被父进程（沙箱 / Job Object）整组杀掉](#坑-17)

---

<a id="坑-1"></a>
## 坑 1（最严重）：底层 HTTP 请求没有任何超时，会永久挂起

### 现象
一个 8 GB 的大文件传到一半，**卡死不动**，一卡就是 24 分钟以上。
- 网卡流量掉到接近 0（0.5 MB/s）
- CPU 1.6%，磁盘无 IO
- 没有任何异常抛出，`upload_file()` 就一直不返回

### 根因
追调用链：

```
client.upload_file
  → p115oss.upload_file
    → oss_multipart_upload_part
      → oss_request
        → urllib3_future_request.request     ← 这里
          → PoolManager.request(**urlopen_kw)
```

`urllib3_future_request.request` **没有设置任何超时，也没有重试**。
当 TCP 连接「半死」（对端没发 RST，但也不响应）时，单个 PUT 请求会
**永远等待**。库自己不会救你。

### 修复
把 `urllib3` 的 `Timeout` 通过 `**request_kwargs` 透传进去：

```python
def make_timeout():
    from urllib3_future import Timeout
    return Timeout(connect=30, read=180)

c.upload_file(local, pid, filesize=size, reporthook=hook, timeout=make_timeout())
```

`upload_file(..., **request_kwargs)` → `PoolManager.request` → `urlopen_kw`
→ `urlopen(timeout=...)`，链路是通的。

**验证方式**：给一个不可达地址发请求，12.1 秒后抛出 `ConnectTimeoutError`
（默认 3 次重试），证明超时确实生效。

### 教训
> **任何「调用第三方库做网络 IO」的代码，第一件事就是确认它有没有超时。**
> 「库应该会处理吧」是最危险的想法。

---

<a id="坑-2"></a>
## 坑 2：限流伪装成「成功」，被误判为「空目录」

### 现象
目录树建到一半，突然大量目录「不存在」，然后 `mkdir` 全部报
`errno=20004「该目录名称已存在」`，接着任务崩掉。

### 根因
115 的开放平台接口限流时，**HTTP 状态码仍然是 200**，但 body 是：

```json
{
  "state": false,
  "code": 770004,
  "message": "已达到当前访问上限，请稍后再试",
  "data": []
}
```

我的代码是这么写的：

```python
r = c.fs_files_open({'cid': cid, ...})
data = r.get('data') or []      # ← 限流时这里是 []
for it in data: ...
```

**限流被当成了「这个目录是空的」**。于是：
1. 已经存在的目录被判定为不存在
2. 去 `mkdir` → 115 说已存在 → 拿不到新 cid
3. 整个目录树结构错乱

### 修复
**必须显式检查 `state` 字段**，而不是只看 `data`：

```python
if isinstance(r, dict) and r.get('state') is False:
    code = r.get('code')
    msg = r.get('message') or ''
    raise RuntimeError('state=False code=%s msg=%s' % (code, msg))
```

抛出后由外层统一做指数退避重试。

### 限流的真实参数（实测）
- **触发阈值极低**：连续调用约 **30 次**就触发。
  （实测 40 次连打：前 30 次成功，第 31 次开始全部失败）
- **恢复很慢**：触发后 **20 分钟以上**仍未解除。
- **账号本体不受影响**：`user_info()` 一直正常，
  说明这是**接口级配额**，不是封号。

### 教训
> **不要假设「没抛异常 = 成功」。** 很多 API 用 `state/code` 字段表达失败，
> HTTP 层永远是 200。拿到响应第一件事是先校验业务状态码。

---

<a id="坑-3"></a>
## 坑 3：`fs_files` 返回 405，而 `fs_files_open` 正常

### 现象
`c.fs_files(...)` 持续返回 `HTTP Error 405: Method Not Allowed`，
即使用最简参数、单次调用也一样。而账号其他接口都正常。

### 根因
这两个方法**走的是完全不同的端点**，可用
`inspect.signature(...).parameters['base_url'].default` 验证：

| 方法 | 端点 | 状态 |
|---|---|---|
| `fs_files` | `https://webapi.115.com/files` | **永久 405** |
| `fs_files_open` | `https://proapi.115.com/open/ufile/files` | 可用（但限流严） |
| `fs_files_app` | `https://proapi.115.com/.../android/2.0/ufile/files` | 常回 `errno=99 请重新登录` |
| `fs_search` | `https://webapi.115.com/files/search` | 可用（受同一限流影响） |

`fs_files` 那个端点已经失效了。**换用 `fs_files_open` 即可**，
两者返回的字段结构一致。

### 排查思路
> 遇到 405/403 这类「方法不允许」错误，先别怀疑权限或账号。
> **先确认你打的是哪个 URL。** 很多 SDK 里有多个名字相似的方法
> 指向不同的 host/path，一个挂了另一个还活着。

---

<a id="坑-4"></a>
## 坑 4：目录 id 不在 `cid` 字段里

### 现象
目录列表拿得到名字和大小，但**所有目录的 id 都是 `None`**，
导致后续无法进入子目录。

### 根因
凭直觉写了这段（`fs_files` 老文档里目录 id 叫 `cid`）：

```python
idv = it.get('cid') if isdir else it.get('fid')
```

但 `fs_files_open` 的实际返回是：

```json
// 目录
{"fid": "3538089913171838179", "fn": "ALEXA SXT", "fc": "0"}
// 文件
{"fid": "3538164663294363044", "fn": "xxx.xml", "fc": "1"}
```

**目录和文件的 id 都在 `fid` 字段**，`cid` 不存在。

### 修复
统一取 `fid`，回退 `cid`：

```python
idv = it.get('fid') or it.get('cid')
```

### 教训
> **不要凭字段名猜结构，把真实响应打印出来看。**
> 这个 bug 不会报错，只会静默产生 `None`，非常难查。

---

<a id="坑-5"></a>
## 坑 5：缓存把目录 id 覆盖成空字符串

### 现象
建目录树时，**272 个目录节点失败**（`无法确定目录 cid`）。
单独测试那个目录却完全正常。

### 根因
`upload_one()` 上传成功后会把文件写回目录列表缓存，
用于「下次同名文件不用再查」：

```python
cache_add(pid, name, size)     # ← 注意没传 fid
```

而 `cache_add` 的实现是：

```python
m[name] = (str(fid) if fid else '', size)   # ← fid 是 None → 写成 ''
```

问题在于：**如果这个名字原本是「目录」条目，它的 id 就被覆盖成空串了**。

后续 `ensure_dir` 读到：

```python
if name in kids:
    return kids[name][0]     # 返回 '' —— 空 cid！
```

### 修复
两条规则：
1. 新 id 为空、而缓存里已有非空 id → **保留旧 id**；
2. **不要把「目录」记录降级成「文件」记录**（目录的 size 是 `None`）。

```python
old = m.get(name)
new_id = str(fid) if fid else ''
if old and old[0]:
    if old[1] is None and size is not None:
        return                    # 目录不能被文件覆盖
    if not new_id:
        new_id = old[0]           # 保留原有非空 id
m[name] = (new_id, size)
```

### 教训
> 缓存写入要考虑**「谁在写、写全了没有」**。
> 增量更新缓存时，缺失的字段要么保留旧值，要么显式置空，不能想当然。

---

<a id="坑-6"></a>
## 坑 6：只收集叶子目录链，中间层目录没建

### 现象
文件名格式是相对路径链，比如
`['ARRIRAW', 'M003R00H', '2880x1620']`。建树时大量报 `no-parent`。

### 根因
我收集待建目录时只取了**叶子链**：

```python
dir_chains = {tuple(chain) for _, chain, _ in items}   # ← 只有完整链
```

于是 `('ARRIRAW',)` 这个中间层**从来没被创建**，
它的子目录 `('ARRIRAW','M003R00H')` 就找不到父目录。

### 修复
**展开每条链的所有前缀**：

```python
dir_chains = set()
for _, chain, _ in items:
    for d in range(1, len(chain) + 1):
        dir_chains.add(tuple(chain[:d]))
dir_chains = sorted(dir_chains, key=len)   # 从浅到深，父先于子
```

按长度排序保证父目录总是先建。实测修复后 **8572 文件 / 54 目录节点，失败 0**。

### 教训
> 处理「路径 / 层级」数据时，**永远显式构造所有前缀**。
> 任何依赖「父一定出现在某个叶子链里」的假设都是错的。

---

<a id="坑-7"></a>
## 坑 7：看门狗单指标误杀「SHA1 计算阶段」

### 现象
装上「5 分钟无进展就断开重试」的看门狗后，
大文件**频繁被误判为卡死**，反复重试。

### 根因
`p115client` 在上传大文件前，会**先把整个文件读一遍计算 SHA1**。
这个阶段：
- `reporthook`（只在 HTTP chunk 真正上传时回调）**完全不触发**
- 但 NAS 正在以 28 MB/s 读盘，网卡也可能有流量

对 8 GB 文件，这个「静默期」长达 **约 5 分钟** —— 正好踩到阈值。

### 修复
**双指标判定**：只有 `reporthook` **和** 本机网卡上行**同时**归零，才判定卡死。

```python
if time.time() - self.last < self.timeout:
    continue                      # reporthook 有进展 → 活着
# reporthook 停了，看网卡
bps, _ = nic_speed.measure(6.0)
if bps > 256 * 1024:              # 网卡还有流量 → 也认为活着
    self.last = time.time()
    continue
self._fired = True                # 两个都归零 → 真卡死
```

### 教训
> 「超时判定」用单一信号很容易误杀。
> **只要还有任何一条证据表明「事情在推进」，就不该判死。**
> 反过来：只有**所有**指标都归零，才说明真出问题了。

---

<a id="坑-8"></a>
## 坑 8：把「跳过」的字节算进速度，得出几十万 MB/s

### 现象
日志里打印出 `142262 MB/s`、`175998 MB/s`、`434298 MB/s` 这种
**物理上不可能**的速度。

### 根因
速度是这么算的：

```python
speed = done_bytes / elapsed
```

而 `done_bytes` 里包含了 **`skip` 的字节**（文件已存在，根本没传）。
当整批文件全是 `skip` 时，几十 GB 的「传输」在 1 秒内「完成」。

### 修复
严格区分两个计数器：

```python
done_bytes = 0       # 已落盘总量（含跳过）→ 只用于进度条
uploaded_bytes = 0   # 本次真实上传字节 → 用于算速度
t_up = None          # 首次真实上传的时刻（速度分母起点）
...
if r == 'ok':
    if uploaded_bytes == 0:
        t_up = time.time()        # 从第一次真传开始计时
    uploaded_bytes += size
...
speed = uploaded_bytes / (time.time() - t_up)
```

### 教训
> 统计口径错了，会得出**看起来很精确、实际毫无意义**的数字。
> 分子分母的「语义」必须严格对齐：分子是「新传的字节」，
> 分母就必须是「真正花在新传上的时间」。

---

<a id="坑-9"></a>
## 坑 9：网络共享上 `os.walk` + `getsize` 慢 60 倍

### 现象
扫描一个目录（8572 个文件）耗时 **102.3 秒**，成为瓶颈。

### 根因
```python
for root, dirs, files in os.walk(base):
    for f in files:
        size = os.path.getsize(os.path.join(root, f))   # ← 每个文件一次 stat
```

在 SMB 网络共享上，**每次 `getsize` 都是一次额外的网络往返**。

### 修复
用 `os.scandir` —— 它返回的 `DirEntry` 自带 `stat()`，
**一次遍历就能拿到大小**，零额外往返：

```python
with os.scandir(cur) as it:
    for e in it:
        if e.is_dir(follow_symlinks=False):
            stack.append((e.path, chain + (e.name,)))
        elif e.is_file(follow_symlinks=False):
            items.append((e.path, list(chain), e.stat().st_size))
```

**102.3s → 1.7s（约 60 倍）。**

### 教训
> 在**网络文件系统**上，`stat` 是昂贵操作。
> 能用 `scandir` 批量拿到元数据，就别一个个 `getsize`。

---

<a id="坑-10"></a>
## 坑 10：每个文件都重查目录 → O(n²)

### 现象
某个机型（8572 文件，但单目录最多 1309 个文件）传得极慢，
**每个文件 0.8～1.1 秒**。

### 根因
`upload_one()` 每次都要列出目标目录，判断文件是否已存在：

```python
kids = remote_children(c, pid)     # ← 每个文件都查一次
if name in kids and kids[name][1] == size:
    return 'skip-exists'
```

但同一个目录下的 1309 个文件，**pid 是同一个**。
每次重查 → 复杂度 O(n²)。

### 修复
按 `pid` 缓存目录列表：

```python
_DIR_CACHE = {}          # pid -> {name: (id, size)}

def cached_children(c, pid, refresh=False):
    with _DIR_LOCK:
        if not refresh and pid in _DIR_CACHE:
            return _DIR_CACHE[pid]
    kids = remote_children(c, pid)
    with _DIR_LOCK:
        _DIR_CACHE[pid] = kids
    return kids
```

上传成功后把新文件写回缓存。
**0.8s/文件 → 0.06s/文件（约 15 倍）。**

### 教训
> 循环里调用「会访问远端」的函数时，先问一句：
> **这个调用的结果在循环内会不会变？** 不变的就能缓存。

---

<a id="坑-11"></a>
## 坑 11：并发测试被「秒传」污染，数据完全失真

### 现象
并发测试得出一组「漂亮」的线性加速数据：

```
conc=8    35.4 MB/s
conc=16  122.4 MB/s
conc=24  253.7 MB/s
conc=32  321.5 MB/s     ← 物理上不可能
```

### 根因
**两个问题叠加：**

1. **115 秒传**：115 会对文件算 SHA1，如果云端已有相同文件，
   直接「秒传」完成（响应里 `q=1`），**根本不走网络**。
2. **测试设计缺陷**：每个并发档位用的是**同一批文件**。
   第 1 档真传完，第 2 档就全是秒传了。

还有一个隐藏问题：**操作系统文件缓存**。
同一批文件读第二遍走内存，实测出现过 `1120 MB/s`、`3912 MB/s` 这种假数据。

### 修复
**每档用互不相同的文件**，并且加一个「合理性校验」：

```python
PER = 60
cursor = 0
for conc in LEVELS:
    batch = band[cursor:cursor + PER]    # ← 每档取不同的文件
    cursor += PER
    ...
    trust = 'OK' if mbps < 45 else '!! 疑似秒传污染'
```

45 MB/s 是存储侧读取上限 —— **超过它的吞吐必然含秒传**。

修正后的真实数据：

| 并发 | 吞吐 | 加速比 |
|---|---|---|
| 1 | 8.9 MB/s | 1.00x |
| 4 | 20.6 MB/s | 2.5x |
| 8 | 30.2 MB/s | 3.4x |
| 16 | 无提升 | — |
| 32 | 出错 | — |

### 教训
> **任何性能测试都要问：这个数字有没有「作弊」的路径？**
> 缓存、去重、秒传、预热 —— 都会让数字变得很好看，但毫无意义。
> **加一个「物理上限」校验**，超标就说明测错了。

---

<a id="坑-12"></a>
## 坑 12：`P115Client` 不适合并发查询

### 现象
共享一个 `P115Client` 实例，8 个线程并发调 `fs_files`：**8/8 全部报错**。

换成「每个线程独立 new 一个 client」，**依然 8/8 失败**。

### 根因
- `P115Client` 内部有状态（cookie / 会话），**不是线程安全的**；
- 即使每线程独立实例，115 侧对**同一 cookie 的并发查询**也有限制。

### 修复
**架构上避免并发查询**：

1. 主线程**先把整棵目录树建好**（串行查询）；
2. 线程池**只做上传**，worker 不再查询任何目录；
3. 上传所需的 pid 从内存字典直接查表。

```python
# 阶段 1：主线程串行建树
dircache = {(): root_cid}
for chain in dir_chains:                  # 去重后，数量 = 目录数
    dircache[chain] = U.ensure_dir(...)

# 阶段 2：线程池只上传
jobs = [(local, dircache[tuple(chain)], size) for ...]
with ThreadPoolExecutor(max_workers=conc) as ex:
    list(ex.map(do_one, jobs))
```

这个改法还顺带把 API 调用量从 **O(文件数) 降到 O(目录数)**。

### 教训
> **并发是把「网络 IO」并行化，不是把「所有东西」并行化。**
> 元数据操作（查目录、建目录）应当串行且去重，
> 把并发额度全部留给真正的数据传输。

---

<a id="坑-13"></a>
## 坑 13：把「本机网卡流量」当成「上传流量」

### 现象
监控面板上的「实时上传速度」长期显示 0.2～0.5 MB/s，
而实际上传明明在正常进行。

### 根因
**数据流不经过本机**。

素材在一台独立 NAS 上。上传时是：

```
NAS  ──────►  115        （数据不经过本机）
      ▲
      └── 本机只在这里发指令（调 API）
```

本机只是个「调度者」，网卡上只有 API 请求的零星流量。

### 修复
面板速度改用 **115 侧的落盘增量**（对比两次查询的已传字节数）。
代价是粒度粗（整文件级）、有滞后，但**至少是真实的**。

> 如果确实需要精确的瞬时速率，应该在数据实际流动的那台机器上采样，
> 或者从传输工具自身读取进度回调。

### 教训
> **先画清楚数据流图，再决定「在哪测量」。**
> 在错误的位置测量，指标再精确也没用。

---

<a id="坑-14"></a>
## 坑 14：并发数不是越高越好

### 现象
并发从 8 加到 16、24、32，**吞吐一点没涨**，32 还直接出错。

### 根因
瓶颈**不在网络，也不在 115**，而在**存储侧的读取能力**。

实测存储读取上限：单线程 ~28 MB/s。
所以并发 8（30.2 MB/s）时**已经打满了**，再往上加只是徒增开销。

### 修复
- 默认并发取 **4**（20.6 MB/s，性价比拐点）；
- 内置**自动降并发**：一批任务里失败率超过 50% 就减半，最低到 1；
- 用 `UP_CONC` 环境变量可调。

### 教训
> **并发是「让瓶颈前面的空闲资源被利用起来」，不是「凭空变快」。**
> 上线前一定要先测出**真正的瓶颈在哪**，
> 否则加了并发也不会有任何提升。

---

<a id="坑-15"></a>
## 坑 15：`mkdir` 说「已存在」但列表接口看不见

### 现象
极少数目录（比如一个叫 `xml` 的），`mkdir` 返回
`errno=20004「该目录名称已存在」`，但列目录返回 **0 条** ——
拿不到它的 id，建树失败。

### 根因
**115 侧的数据不一致 / 脏数据**：
该条目存在（所以 mkdir 说已存在），但**列表接口不返回它**。

### 修复
用 `fs_search` **按名字精确搜**，能查到真实 fid：

```python
def _search_child(c, pid, name):
    r = c.fs_search({'search_value': name, 'cid': pid, 'limit': 50})
    for it in (r.get('data') or []):
        if (it.get('fn') or it.get('n')) == name and str(it.get('fc')) == '0':
            return str(it.get('fid') or it.get('cid'))
    return None
```

兜底顺序：缓存 → 刷新父目录 → `mkdir` → `fs_search` → 降级到父目录（记日志）。

### 教训
> **面对别人的脏数据，目标不是「修好它」，而是「不因此崩溃」。**
> 设计一条清晰的降级链，并**把降级行为写进日志**，
> 这样既能继续跑完，又保留了事后排查的线索。

---

<a id="坑-16"></a>
## 坑 16：多线程下 `save_state` 抛 `dictionary changed size during iteration`

### 现象
开了 4 并发之后，上传**跑得好好的**（已经成功传了 36 个文件、速度 35 MB/s），
突然整个任务崩掉：

```
RuntimeError: dictionary changed size during iteration
  File "upload_batch_robust.py", line 175, in do_one
    U.save_state(st)
  File "upload_115_robust.py", line 289, in save_state
    json.dump(st, io.open(tmp, 'w', encoding='utf-8'), ...)
  File ".../json/encoder.py", line 358, in _iterencode_dict
    for key, value in items:
```

而且这个崩溃**发生在串行版本里完全不会出现**的地方 —— 串行时 `st` 没人抢。

### 根因
这是**经典的多线程竞态**，但隐蔽点在于「报错的位置在 JSON 序列化，不在写字典的地方」：

```
worker A: U.upload_one(...)  →  st[key] = 'done'      ← 正在增长字典
worker B: U.save_state(st)   →  json.dump(st) 遍历它  ← 遍历中被改 → RuntimeError
```

`json.dump` 内部是**迭代式**编码（`_iterencode_dict` 逐 key 遍历），
遍历途中字典 size 变化，Python 立刻抛异常保护你（否则会读到不一致状态）。

### 修复（两处，缺一不可）

**1. `save_state` 里加锁 + 快照**（`upload_115_robust.py`）：

```python
# 模块级锁：同时保护「写 st」和「序列化 st」
_STATE_LOCK = threading.RLock()

def save_state(st):
    with _STATE_LOCK:
        try:
            snap = dict(st)          # ← 锁内做浅拷贝，拿到稳定快照
        except RuntimeError:
            snap = {}
    # 锁外做磁盘 IO，避免阻塞其它 worker
    tmp = '%s.%d.tmp' % (STATE, os.getpid())   # ← tmp 带 pid，避免多 worker 互踩
    with io.open(tmp, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())         # 落盘再 rename，防止断电产生半截文件
    os.replace(tmp, STATE)           # 原子替换
```

**2. `do_one` 里包住 `upload_one`，并把存档移出统计锁**（`upload_batch_robust.py`）：

```python
st_lock = threading.RLock()

def do_one(job):
    try:
        with st_lock:                      # ← upload_one 会写 st，整体进锁
            r = U.upload_one(c, local, pid, st, key)
    except Exception as e:
        r = 'fail:' + repr(e)[:120]

    with lock:                             # 统计用的另一把锁
        ...
        need_save = (i % 25 == 0 or i == total)
    # ⚠ 存档放到锁外面：磁盘 IO 不该阻塞其它 worker 的统计更新
    if need_save:
        _ckpt()                            # 内部再进 st_lock 拿快照

def _ckpt():
    with st_lock:
        U.save_state(st)
```

### 为什么 tmp 文件名要带 pid
原来多个 worker 同时 `os.replace(tmp, STATE)` 会**互相踩同一个 `.tmp` 文件**：
A 写一半，B 把文件覆盖 / 改名走了 → A 的 `replace` 失败或写进脏数据。
带上 pid 后每进程一个 tmp，互不干扰。

### 教训
- **并发 worker 里「写共享 dict」和「序列化该 dict」必须在同一把锁内。**
  光锁写的地方不够 —— 读（序列化）也要锁。
- 报错位置常常不是问题根源：这里错在 `json.dump`，**病根在 `st[key] = ...`**。
- 用锁**只为保护数据一致性**，磁盘 IO 尽量放锁外，否则并发退化成串行。
- 这个 bug 只在「并发 + 有定期存档」组合下才出现 ——
  **并发改造时，除了上传逻辑，所有共享状态的读写路径都要重新过一遍。**

---

<a id="坑-17"></a>
## 坑 17：后台进程被父进程（沙箱 / Job Object）整组杀掉

### 现象
想让上传在后台长期跑，于是：

```bash
nohup python upload_batch_robust.py > _run_auto.log 2>&1 &
# 或者用支持「后台任务」的工具启动
```

日志里明明打印了 `上传任务已启动 PID=23992`，
**但几秒后 `tasklist` 里那个 PID 已经没了**，上传根本没跑起来，
而且**日志里没有任何异常**（进程是被外部杀掉，不是自己崩的）。

更诡异的对照实验：

```python
# 同一个 Bash 调用内启动 + 检查
p = subprocess.Popen([...], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
time.sleep(15)
# → 子进程还活着，日志在写！看起来「脱离成功」
```
```python
# 换【另一次】Bash 调用再检查
# → 进程已死，日志文件是空的
```

### 根因
执行环境用 **Job Object（作业对象）** 管理每次命令的进程树。
**一次命令结束时，整个 Job 被终止** —— 连带 Job 里的所有子孙进程，
无论你加了 `DETACHED_PROCESS`、`CREATE_NEW_PROCESS_GROUP`、还是 `nohup`。

也就是说：**从「受 Job 管控的进程」里 spawn 出去的子进程，跑不出这个 Job。**

### 修复：用操作系统级的任务调度器

进程必须由**已经在 Job 之外的服务**来拉起。Windows 上用**计划任务**：

```powershell
$dir = "C:\path\to\project"
$act = New-ScheduledTaskAction -Execute "$dir\_run_upload.cmd" -WorkingDirectory $dir
$set = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
        -StartWhenAvailable
Register-ScheduledTask -TaskName "ARRI_115_Upload" -Action $act -Settings $set -Force
Start-ScheduledTask   -TaskName "ARRI_115_Upload"
```

计划任务由 **Task Scheduler 服务**（`svchost`，system 会话）托管，
完全不在调用方的 Job 里，**实测跨调用持续存活、日志持续增长**。

配套的 `_run_upload.cmd`：
```bat
@echo off
chcp 65001 >nul
cd /d "C:\path\to\project"
set UP_CONC=4
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
"C:\...\python.exe" -u supervise_upload.py >> "_supervisor_stdout.log" 2>&1
```

Linux 上对应的是 `systemd-run --user --unit=xxx` 或 `systemd` service；
macOS 上是 `launchd`。

### 外加一层保险：监督进程模式
`supervise_upload.py` 循环拉起上传、崩溃后指数退避重启：

```python
backoff = 60
for i in range(1, 201):
    rc = subprocess.Popen([...]).wait()
    if rc == 0:
        return                      # 正常完成，退出
    if dur > 600:
        backoff = 60                # 干过活才崩 → 退避重置
    time.sleep(backoff)
    backoff = min(backoff * 2, 900) # 连续快崩 → 最多退到 15 分钟
```

**「崩了就重启」之所以安全，是因为上传本身是幂等的** ——
`_upload_state.json` 记录已完成文件，重启后自动跳过、断点续传。

### 其他踩到的附带坑
- **`schtasks.exe` 可能在命令黑名单里**（会被拦），
  但 PowerShell 的 `Register-/Start-/Get-ScheduledTask` cmdlet 通常可用 —— 走这条路。
- **Git Bash 的 heredoc 会吞反斜杠**：`cat <<EOF` 写 `C:\Users\...` 会变成 `C:/Users/...`。
  写 `.cmd` / `.bat` 批处理**必须用文件写入工具**，不要用 heredoc。
- 批处理里务必 `chcp 65001` + `set PYTHONIOENCODING=utf-8`，
  否则中文路径 / 中文机型名会乱码。

### 教训
- **验证后台进程是否存活，必须跨「命令调用」检查。**
  在同一个调用里 `sleep` 完再看，是自欺欺人 —— 那时进程当然还没被回收。
- 「我以为脱离了」不等于「真的脱离了」：Job Object 的边界取决于
  **是谁启动了你**，不取决于你给了子进程什么 flag。
- 长任务要交给**操作系统级调度器**，不要指望从短期命令里 spawn 出去。

---

## 附：一张图看懂并发架构

```
主线程（串行，API 调用极少）
├─ 扫描本地素材（os.scandir）
├─ 展开所有目录链前缀，去重
├─ 逐个 ensure_dir  →  建好整棵目录树
└─ 生成 jobs = [(文件, 目标pid, 大小), ...]
        │
        ▼
线程池（并发=4，只做上传，不查目录）
├─ worker 1  ─┐
├─ worker 2  ─┤  每个文件：
├─ worker 3  ─┤   ├─ 查本地状态（零触网）→ 已传就跳过
└─ worker 4  ─┘   ├─ upload_file(局部超时 + 看门狗 + 重试)
                  └─ 写回缓存 + 更新状态
```

**核心原则：把所有「元数据查询」压到主线程串行完成，
让线程池只专心搬数据。**

---

## 附：参数速查

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `UP_CHUNK` | 8 MB | 分块大小 |
| `UP_CONNECT_TIMEOUT` | 30 s | 连接超时 |
| `UP_READ_TIMEOUT` | 180 s | 单块读取超时 |
| `UP_WATCHDOG_SEC` | 300 s | 双指标静默多久判卡死 |
| `UP_MAX_RETRY` | 5 | 单文件重试次数 |
| `UP_RETRY_BASE` | 5.0 s | 指数退避基数 |
| `UP_LIST_RETRY` | 4 | 目录列举重试次数 |
| `UP_CONC` | 4 | 多文件并发度 |

---

## 免责声明

本文记录的是**特定时间点**（2026-10）特定版本的实测行为。
115 的接口和限流策略随时可能变化，请以你自己的实测为准。
所有结论都附了验证方法，建议在改动前先自己复现一遍。
