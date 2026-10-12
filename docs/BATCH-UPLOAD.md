# 批量并发上传（batch upload）

本仓库除原有的单文件 `upload_local_file` 之外，还内置了一套**面向海量素材的批量上传引擎**。
它来自一个真实迁移项目：**8 572 个文件 / 640 GB ARRI 原始素材**（NAS → 115），
在 115 开放平台的限流环境下跑通，最终沉淀为本仓库的 `mcp_115_server.uploader` /
`batch` / `supervisor` 三个模块。

> 所有「为什么这么写」的坑，都记在 [`PITFALLS.md`](./PITFALLS.md)（17 个坑）。
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
| `UPLOAD_WORKDIR` | 当前目录 | 状态文件 / 日志目录。 |
| `UP_CHUNK_MB` | `8` | 分块大小（MB）。太大时单块失败重传代价高。 |
| `UP_CONNECT_TIMEOUT` | `30` | TCP 连接超时（秒）。 |
| `UP_READ_TIMEOUT` | `180` | 读超时（秒）。 |
| `UP_WATCHDOG_SEC` | `300` | 看门狗：进展**与**网卡**双双**为零多久判卡死。 |
| `UP_MAX_RETRY` | `5` | 单文件重试次数（指数退避）。 |

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
  uploader.py       # 单文件上传核心：超时注入、看门狗、限流识别、状态、os.scandir 扫描
  batch.py          # 批量编排：两阶段（串行建树 → 线程池上传）+ 自动降并发
  supervisor.py     # 崩溃重启监督器 + Windows 计划任务注册
  batch_upload.py   # 命令行入口（python -m mcp_115_server.batch_upload）
  config.py         # 新增 UPLOAD_* / UP_* 配置项
  service.py        # 新增 batch_upload_plan() / batch_upload()
  server.py         # 新增 MCP 工具 batch_upload_plan / batch_upload
docs/
  PITFALLS.md       # 17 个坑的完整记录（强烈建议先读）
  BATCH-UPLOAD.md   # 本文
bench/
  storage_read_ceiling.py
  conc_test.py
  conc_test_real.py
tools/
  nic_speed.py      # 独立测本机网卡上行速率
```

---

## 8. 作为库使用

```python
from mcp_115_server import uploader as U
from mcp_115_server.batch import BatchUploader

client = U.client_from_cookie("C:/Users/me/115-cookies.txt")   # 或复用 P115Service.client()
cfg   = U.UploaderConfig(chunk=8 * 1024 * 1024, connect_timeout=30, read_timeout=180)
paths = U.UploadPaths("C:/data/workdir")

up = BatchUploader(client, cfg, paths, conc=4)
res = up.upload_tree(r"C:\data\material\ALEXA 65", parent_cid="3538089913171838179", remote_name="ALEXA 65")
print(res.summary())          # -> "ok=412 skip=1180 fail=0 ..."
print(res.mbps)               # 平均 MB/s
```

> **`P115Client` 不是线程安全的。** 不要把同一个 client 并发用于**查询**。
> 本仓库的做法是：所有元数据查询压到主线程串行完成，线程池只负责搬运数据。
> 详见 [`PITFALLS.md`](./PITFALLS.md)。

---

## 9. 常见问题

**Q：上传中途报 `code 770004`「已达到当前访问上限」怎么办？**
A：这是 115 的限流。本引擎会识别并退避，但**恢复需要 20 分钟以上**。
建议降低 `UP_CONC`，并避免同时跑多个上传实例。

**Q：状态文件损坏了？**
A：删除 `_upload_state.json` 会**从头重传全部文件**。
若只想重传个别文件，编辑该文件删除对应条目即可（保留 JSON 结构）。
写入是「锁 + 快照 + pid 后缀临时文件 + fsync + 原子替换」，正常不会损坏。

**Q：为什么重跑时 `skip` 这么多？**
A：这是幂等设计的正常表现——已传过的文件被跳过了。

**Q：目录建好了但文件全失败？**
A：检查是否漏建了**中间层目录**。本引擎会展开所有目录链前缀，
若你自行裁剪了 `dir_chains`，深层目录会因 `no-parent` 全灭。详见 `PITFALLS.md`。

**Q：MCP 服务起不来，报 `unexpected keyword argument 'check_for_relogin'`？**
A：`p115client` 版本差异导致。本仓库已用 `_build_client()` 做参数过滤兼容，
若你在自己的代码里直接构造 `P115Client`，请照做或去掉该参数。
