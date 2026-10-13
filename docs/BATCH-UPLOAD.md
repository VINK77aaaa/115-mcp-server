# 批量并发上传（batch upload）

本仓库除原有的单文件 `upload_local_file` 之外，还内置了一套**面向海量素材的批量上传引擎**。
它来自一个真实迁移项目：**8 572 个文件 / 640 GB ARRI 原始素材**（NAS → 115），
在 115 开放平台的限流环境下跑通，最终沉淀为本仓库的 `mcp_115_server.uploader` /
`batch` / `supervisor` 三个模块。

> 所有「为什么这么写」的坑，都记在 [`PITFALLS.md`](./PITFALLS.md)（24 个坑，
> 时间只够读一条的话读**坑 18**）。
> 本文只讲**怎么用**。

---

## 1. 它和 `upload_local_file` 的区别

| | `upload_local_file`（原有） | `batch_upload`（新增） |
|---|---|---|
| 粒度 | 单个文件 | 整棵本地目录树 |
| 并发 | 无（串行） | 线程池，默认 4，推荐 4~8 |
| 断点续传 | 无 | 状态文件记录，重跑自动跳过 |
| 超时 | 无 | 显式 `Timeout(connect, read)` |
| 卡死检测 | 无 | 双指标看门狗 |
| 限流识别 | 无 | 识别 `code 770004` 并退避 |
| 适用 | 零星文件 | 几万文件 / 几百 GB |

两者**互不影响**：不改 `UPLOAD_*` / `UP_*` 环境变量时，单文件上传行为与原来完全一致。

---

## 2. 环境变量

认证复用本仓库原有配置（`P115_COOKIES` / `P115_COOKIES_PATH`），无需第二套 cookie。

| 变量 | 默认 | 说明 |
|---|---|---|
| `UPLOAD_ROOT` | 无 | 素材根目录；其下**每个子目录**作为一个远端目录。 |
| `UPLOAD_PARENT_CID` | `0` | 115 目标父目录 id，`0` = 根目录。 |
| `UP_CONC` | `4` | 多文件并发度。4 是性价比拐点，8 是天花板（见下表）。 |
| `UPLOAD_WORKDIR` | 当前目录 | 状态文件 / 日志 / **目录缓存**目录。 |
| `UP_CHUNK_MB` | `8` | 分块大小（MB）。太大时单块失败重传代价高。 |
| `UP_CONNECT_TIMEOUT` | `30` | TCP 连接超时（秒）。 |
| `UP_READ_TIMEOUT` | `180` | 读超时（秒）。 |
| `UP_WATCHDOG_SEC` | `300` | 看门狗：进展**与**网卡**双双**为零多久判卡死。 |
| `UP_MAX_RETRY` | `5` | 单文件重试次数（指数退避）。 |
| `UP_LIST_RETRY` | `4` | 目录列举重试次数。 |
| `UP_LIST_RETRY_BASE` | `2.0` | 目录列举退避基数（秒）。 |
| `UP_SRC_COOLDOWN` | `600` | **单个列目录源**被 WAF 拦（HTTP 405）后的冷却时长（秒）。 |
| `UP_LIST_BLOCK_SEC` | `900` | **全局列目录熔断**时长（秒）：账号级限流后短期内地毯式停掉所有在线列目录。 |
| `UP_LIST_WAIT_MAX` | `1800` | 建树前「等列目录恢复」的最长轮询时间（秒）。**测试时务必设 0。** |
| `UP_DIR_FAIL_TTL` | `60` | 列目录失败**负缓存** TTL（秒）：失败过的目录在这段时间内不再重试。 |

> `UP_WATCHDOG_SEC` 的注意事项：若素材源的**数据不经过本机网卡**
> （例如本机只做控制、数据由 NAS 直连 115），网卡指标恒为 0，
> 看门狗会退化成单指标，容易误杀——此时应调大，例如 `900`。

---

## 3. 三种用法

### 3.1 MCP 工具（推荐给 AI 客户端）

```text
batch_upload_plan(local_root="/data/material", only=["ALEXA 65"])
```

先出**计划**：列出每个子目录的文件数与字节数，**不触网、不上传**。用来评估规模。

```text
batch_upload(local_root="/data/material", parent_cid="3538089913171838179",
             only=["ALEXA 65"], concurrency=4)
```

真正上传。返回结构：

```json
{
  "local_root": "/data/material",
  "parent_cid": "3538089913171838179",
  "concurrency": 4,
  "totals": { "ok": 412, "skip": 1180, "fail": 0, "bytes_uploaded": 3221225472 },
  "trees": [
    { "name": "ALEXA 65", "ok": 412, "skip": 1180, "fail": 0,
      "dirs_built": 37, "dir_fails": 0, "elapsed_sec": 623.4 }
  ],
  "state_file": "/data/workdir/_upload_state.json"
}
```

> ⚠ `batch_upload` 是**长耗时**操作，超大素材可能几小时才返回。
> 建议**按子目录分批调用**（`only=[...]`），这样每次都能拿到结果、进度可观测。
> 重跑是安全的：已上传的文件会被状态文件跳过。

### 3.2 命令行

```bash
# 只看计划
python -m mcp_115_server.batch_upload --root /data/material --dry-run

# 上传，指定父目录与并发
python -m mcp_115_server.batch_upload \
    --root /data/material --parent-cid 3538089913171838179 --conc 4

# 只传某个子目录
python -m mcp_115_server.batch_upload --root /data/material --only "ALEXA 65"
```

退出码约定：**有失败文件时返回 `1`**，全部成功返回 `0`——这样监督器能据此决定是否重试。
另有：`2` = access_token 失效（放弃本轮，等重启换新 client）；
`3` = 列目录被限流/熔断（放弃本轮，避免把文件倒进错误目录）。

### 3.2.1 ⭐ 先做一次「零触网建树」（强烈建议，别跳过）

**为什么**：上传前必须知道「本地每个目录在 115 上对应哪个 cid」。
最直觉的做法是逐层列目录——但那是 **O(目录数)** 次请求，
而 115 的列目录接口**连打约 30 次就会触发账号级限流**（恢复要 20 分钟量级）。
实测这会把吞吐压到 **0.53 文件/秒**。

正确做法：用 `fs_search`（带 `search_value` + `cur=0`，会**递归遍历整棵子树**）
一次性把 115 上的目录树 dump 下来，落盘成 `_dircache.json`：

```bash
python -m mcp_115_server.batch_upload \
    --root /data/material --parent-cid 3538089913171838179 --rebuild-dircache
```

它会：

1. 扫本地目录（`os.scandir`，很快）；
2. 把每个目录名的**前 4 个字符**去重作为搜索词（任何目录 D 的名字 N 都含
   前缀 `N[:4]`，所以搜 `N[:4]` 必然命中 D）——9 个机型 346 个目录
   只需 **48 次搜索调用**；
3. 按目录项返回的 `pid` 指针**自顶向下重建**整棵树；
4. 本地有、115 没有的目录用 `fs_mkdir` 补建（自顶向下）；
5. 写出 `_dircache.json`。

之后正常跑上传即可 —— 启动时会自动 `load_dir_cache()`，
**建树 + 预取全程零网络请求**。实测吞吐从 **0.53 → 4.5+ 文件/秒**。

> 这一步是**幂等**的，随时可以重跑（例如换了素材目录、或 115 上手工加过目录）。
> 详见 [`PITFALLS.md` 坑 18](./PITFALLS.md)（本仓库最值钱的一条）。

### 3.3 无人值守（监督器 + 操作系统级计划任务）

```python
from mcp_115_server.supervisor import Supervisor, register_windows_task

argv = [sys.executable, "-m", "mcp_115_server.batch_upload",
        "--root", "/data/material", "--parent-cid", "3538089913171838179"]
Supervisor(argv, cwd="/data/workdir").run()
```

监督器会：循环拉起上传 → 崩溃后等待 60 s → 再崩溃则指数退避（上限 15 min）
→ 跑够 10 分钟才崩则重置退避 → `rc=0` 时正常退出。

> **⚠ 必须由操作系统级调度器拉起。**
> 从普通父进程（尤其是被 Job Object 管控的沙箱进程）spawn 出去的子孙进程，
> 会随父进程**一起被整组杀掉**——`nohup`、`DETACHED_PROCESS`、
> `CREATE_NEW_PROCESS_GROUP` **全都没用**，而且是**静默消失、日志无异常**。
> 详见 [`PITFALLS.md` 坑 17](./PITFALLS.md)。

`register_windows_task()` 会返回注册 PowerShell 计划任务的命令（不直接执行）：

```python
register_windows_task("115-MCP-BatchUpload", r"C:\path\run_upload.cmd", r"C:\data\workdir")
```

在 Windows 上也可直接粘贴执行：

```powershell
$dir = "C:\data\workdir"
$act = New-ScheduledTaskAction -Execute "C:\path\run_upload.cmd" -WorkingDirectory $dir
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -ExecutionTimeLimit (New-TimeSpan -Hours 0) -StartWhenAvailable
Register-ScheduledTask -TaskName "115-MCP-BatchUpload" -Action $act -Settings $set -Force
Start-ScheduledTask -TaskName "115-MCP-BatchUpload"
```

Linux 用 `systemd-run --user --unit=115-upload ...`，macOS 用 `launchd`。
`schtasks.exe` 在部分受限环境会被安全策略拦，PowerShell 的 `*-ScheduledTask` cmdlet 可用。

---

## 4. 断点续传与幂等

状态文件 `_upload_state.json`（位于 `UPLOAD_WORKDIR`）记录每个已完成文件的相对路径与目标目录 id。
重跑时：

- 状态里已标记 `done` 的文件 → 直接 `skip`，零触网；
- 未完成的 → 正常上传。

因此**中断、崩溃、手动杀掉进程都不会导致重复上传**，重新拉起即可接着跑。

配套日志（同目录）：

| 文件 | 内容 |
|---|---|
| `_upload_state.json` | 断点续传状态（**不要手动编辑**） |
| `_upload_log.jsonl` | 逐文件结果 + 异常明细 |
| `_progress.jsonl` | 进度流水，可用于画进度图 |

---

## 5. 性能与并发选择

在干净样本（6.88 MB/文件，无「秒传」污染）下实测：

| 并发 | 吞吐 | 倍率 |
|---|---|---|
| 1 | 8.9 MB/s | 1.00× |
| 4 | 20.6 MB/s | 2.5× |
| 8 | 30.2 MB/s | 3.4× |
| 16 / 24 | 无提升 | — |
| 32 | 报错 | — |

**瓶颈在存储侧单线程读取上限（约 28 MB/s）**，不在网络。
所以 `UP_CONC=4` 已是性价比拐点，8 是天花板，再往上只会加速触发 115 限流。

`batch_upload` 内置**自动降并发**：某批尝试 ≥4 次且失败过半时，
并发自动减半（下限 1），而不是硬扛着报错。

---

## 6. 基准测试脚本

`bench/` 下三个脚本，用来在你自己的环境复现上述结论：

| 脚本 | 用途 |
|---|---|
| `bench/storage_read_ceiling.py` | 测**存储侧单线程读取上限**（找出真瓶颈） |
| `bench/conc_test.py` | 小文件并发测试（快速摸阈值） |
| `bench/conc_test_real.py` | 真实素材并发测试（会真的上传） |

```bash
export UPLOAD_ROOT=/data/material
export BENCH_MODEL="ALEXA SXT"
python bench/storage_read_ceiling.py
```

> 脚本已做**缓存污染规避**：不同并发档位读不同的切片；
> 若实测 >200 MB/s，会提示「疑似缓存命中」，该档位数据不可信。

---

## 7. 目录结构（新增部分）

```text
src/mcp_115_server/
  uploader.py       # 单文件上传核心：超时注入、看门狗、多源列目录 + 熔断、状态、os.scandir 扫描
  dircache.py       # 零触网建树：fs_search 子树枚举 + 按 pid 重建 + 落盘
  batch.py          # 批量编排：两阶段（串行建树 → 线程池上传）+ 自动降并发
  supervisor.py     # 崩溃重启监督器 + Windows 计划任务注册
  batch_upload.py   # 命令行入口（python -m mcp_115_server.batch_upload）
  config.py         # 新增 UPLOAD_* / UP_* 配置项
  service.py        # 新增 batch_upload_plan() / batch_upload()
  server.py         # 新增 MCP 工具 batch_upload_plan / batch_upload
docs/
  PITFALLS.md       # 24 个坑的完整记录（强烈建议先读，尤其坑 18）
  BATCH-UPLOAD.md   # 本文
bench/
  storage_read_ceiling.py
  conc_test.py
  conc_test_real.py
tools/
  nic_speed.py      # 独立测本机网卡上行速率
tests/
  test_service.py
  test_uploader_hardening.py   # 限流/多源/熔断/零触网建树（全离线，假 client）
```

工作目录里会产生的文件：

```text
_upload_state.json   # 断点续传状态（第一权威，命中即跳过、零触网）
_dircache.json       # 目录树缓存（由 --rebuild-dircache 生成）
_upload_log.jsonl    # 警告/异常日志
_progress.jsonl      # 每文件进度（供监控面板消费）
```

---

## 8. 作为库使用

```python
from mcp_115_server import dircache as DC
from mcp_115_server import uploader as U
from mcp_115_server.batch import BatchUploader

client = U.client_from_cookie("C:/Users/me/115-cookies.txt")   # 或复用 P115Service.client()
cfg   = U.UploaderConfig(chunk=8 * 1024 * 1024, connect_timeout=30, read_timeout=180)
paths = U.UploadPaths("C:/data/workdir")

# ---- 步骤 1（一次性）：零触网建树 ----
local = DC.scan_local_dirs(r"C:\data\material")            # {机型: [目录链, ...]}
cache, report = DC.build_dir_cache(
    client, {"ALEXA 65": "3538089913171838179"}, local_dirs=local)
DC.write_cache(paths.dircache, cache)
# report[0] -> {"dirs_local": 37, "dirs_resolved": 37, "dirs_created": 0,
#               "terms": 12, "dir_hits": 37, "file_hits": 1734, "seconds": 3.2}

# ---- 步骤 2：正常上传（启动时载入缓存 → 建树零触网）----
U.load_dir_cache(paths.dircache)
up = BatchUploader(client, cfg, paths, conc=4)
res = up.upload_tree(r"C:\data\material\ALEXA 65",
                     parent_cid="3538089913171838179", remote_name="ALEXA 65")
print(res.summary())          # -> "ok=412 skip=1180 fail=0 ..."
print(res.mbps)               # 平均 MB/s
```

也可以自己实现「扫描」这一步（例如目录链来自数据库或别的清单），
只要给 `build_dir_cache` 传 `local_dirs={机型: [目录链, ...]}` 即可。

> **`P115Client` 不是线程安全的。** 不要把同一个 client 并发用于**查询**。
> 本仓库的做法是：所有元数据查询压到主线程串行完成，线程池只负责搬运数据。
> 详见 [`PITFALLS.md`](./PITFALLS.md)。

---

## 9. 常见问题

**Q：上传中途报 `code 770004`「已达到当前访问上限」怎么办？**
A：这是 115 的**账号级**限流——一个目录查不到，其它目录也一定查不到。
本引擎的处理是：**一次性全局熔断**（`UP_LIST_BLOCK_SEC`，默认 15 分钟），
熔断期内**零查询**，在线查重降级为「直接传」（115 自己判重，
对秒传素材零损失），给 115 留出恢复窗口。
**最根本的解法是先跑 `--rebuild-dircache`**，让建树这一步根本不需要列目录。

**Q：报 `code=40140125 msg=access_token 无效`？**
A：**多半是有别的进程同时用同一个 cookie**（包括跑着玩的测试脚本）。
`P115Client(cookie, app='web')` 没有 `access_token` 属性，
每次调 open 平台方法都会**实时换一个 token**，换 token 会让上一个失效——
多个 client 同时这么干就互相顶掉。
本引擎的处理是抛 `AuthExpiredError` → 非零退出 → 监督器用**全新 client** 重启
（状态与目录缓存都已落盘，重启是幂等且极廉价的）。
请确认没有第二个 115 client 在跑。详见 [`PITFALLS.md` 坑 20 / 23](./PITFALLS.md)。

**Q：报 `HTTP Error 405: Method Not Allowed`？**
A：那是备用源 `aps.115.com` 被 115 的 **WAF 账号级拦截**了
（带 cookie → 405，**不带 cookie 反而 200**），**等多久都不会恢复**。
本引擎会自动把该源打进冷却（`UP_SRC_COOLDOWN`）并继续用主源，
不会因此中断任务。详见 [`PITFALLS.md` 坑 19](./PITFALLS.md)。

**Q：日志里出现 `list-block-tripped` / `list-wait` / `page-no-progress`？**
A：分别是「全局熔断已触发」「正在等列目录恢复」「分页出现回卷，已停止翻页」，
都是**预期的自我保护行为**，不是崩溃。

**Q：状态文件损坏了？**
A：删除 `_upload_state.json` 会**从头重传全部文件**。
若只想重传个别文件，编辑该文件删除对应条目即可（保留 JSON 结构）。
写入是「锁 + 快照 + pid 后缀临时文件 + fsync + 原子替换」，正常不会损坏。

**Q：`_dircache.json` 可以删吗？**
A：可以，删了只是下次建树要重新问 115（慢、且容易触发限流）。
它是纯缓存，删掉不会丢数据。**但别删 `_upload_state.json`**——那才是断点续传的依据。

**Q：为什么重跑时 `skip` 这么多？**
A：这是幂等设计的正常表现——已传过的文件被跳过了。

**Q：目录建好了但文件全失败？**
A：检查是否漏建了**中间层目录**。本引擎会展开所有目录链前缀，
若你自行裁剪了 `dir_chains`，深层目录会因 `no-parent` 全灭。详见 `PITFALLS.md`。

**Q：跑测试时进程「赖着不走」？**
A：设 `UP_LIST_WAIT_MAX=0`。默认值 1800 s 是为生产设计的，
测试里会让等待类断言真的进轮询循环——而且那个进程会**抢走线上任务的 token**
（它和线上共用同一个 cookie）。详见 [`PITFALLS.md` 坑 23](./PITFALLS.md)。

**Q：MCP 服务起不来，报 `unexpected keyword argument 'check_for_relogin'`？**
A：`p115client` 版本差异导致。本仓库已用 `_build_client()` 做参数过滤兼容，
若你在自己的代码里直接构造 `P115Client`，请照做或去掉该参数。
