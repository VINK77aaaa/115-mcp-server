# 部署笔记（Windows 实测）

> 本文档由部署者补充，记录在本机实际跑通这套 MCP 的过程与踩过的坑。
> 上游项目：[`lingwunb666/115-MCP-Server`](https://github.com/lingwunb666/115-MCP-Server)，
> MIT 协议，版权归原作者 `lingwu` 所有（见 `LICENSE`）。
> 本仓库保留上游全部源码与许可，仅额外补充本笔记与 `.gitignore` 的凭据排除规则。

## 这是什么

基于 **FastMCP** + **p115client** 的 115 网盘 MCP 服务器。

之所以用它，是因为 **115 官方桌面客户端自带的 MCP（默认监听 `127.0.0.1:11502`）没有上传能力** ——
它只有 35 个工具，覆盖浏览、检索、整理、分享、离线下载、回收站，但**无法把本地文件推到 115**。
本项目提供 `upload_local_file`，补齐了这条链路。

实测本项目暴露 **58 个工具**，除上传外还多出回收站恢复、分享接收、目录遍历/glob、路径解析等能力。

## 本仓库的额外扩展：批量并发上传

在 `upload_local_file`（单文件）之外，本仓库**另外内置了一套批量上传引擎**，
用于把整棵目录树推到 115。它来自一次真实迁移：**8 572 个文件 / 640 GB ARRI 原始素材**
（NAS → 115），在 115 开放平台的限流环境下跑通。

新增 2 个 MCP 工具：

- `batch_upload_plan(local_root, only)` —— 出计划，**不触网**
- `batch_upload(local_root, parent_cid, only, concurrency)` —— 真正上传，**长耗时**

新增 4 个模块：`uploader.py`（核心）、`batch.py`（编排）、`supervisor.py`（崩溃重启）、
`batch_upload.py`（命令行入口）。

新增配置（均**可选**，不设置时单文件上传行为不变）：
`UPLOAD_ROOT` / `UPLOAD_PARENT_CID` / `UP_CONC` / `UPLOAD_WORKDIR` /
`UP_CHUNK_MB` / `UP_CONNECT_TIMEOUT` / `UP_READ_TIMEOUT` / `UP_WATCHDOG_SEC` / `UP_MAX_RETRY`。

文档：

- **[`docs/BATCH-UPLOAD.md`](docs/BATCH-UPLOAD.md)** —— 批量上传完整指南（环境变量、两种用法、断点续传、监督器、性能表、FAQ）
- **[`docs/PITFALLS.md`](docs/PITFALLS.md)** —— **17 个坑**的完整记录，强烈建议先读

几个最要命的坑（详见 PITFALLS）：

1. `p115client` 底层的 `urllib3_future_request.request` **没有任何超时** —— TCP 半死时单个 PUT 会永久挂起。
2. 115 限流时 **HTTP 仍是 200**，body 里才是 `code 770004` —— 只看 `data` 会把「限流」误判成「空目录」。
3. `fs_files`（`webapi.115.com`）已**永久 405**，必须走 `fs_files_open`。
4. 目录和文件的 id **都在 `fid` 字段**，不是 `cid`。
5. 多线程并发写状态文件会抛 `RuntimeError: dictionary changed size during iteration` —— 必须锁 + 快照。
6. **后台进程会被父进程整组杀掉**（Job Object），`nohup` / `DETACHED_PROCESS` 全无效，静默消失无日志 —— 必须用计划任务。

## 环境要求

- Python **3.12+**（实测 3.13.12 可用）
- 能访问 115 相关接口
- Windows 已实测通过

## 安装

```bash
git clone https://github.com/<your-account>/115-mcp-server.git
cd 115-mcp-server

python -m venv .venv
./.venv/Scripts/python -m pip install --upgrade pip
./.venv/Scripts/python -m pip install -e .
```

验证：

```bash
./.venv/Scripts/115-MCP-Server --help
```

如果还要自己生成登录二维码，额外装一个 Pillow：

```bash
./.venv/Scripts/python -m pip install pillow
```

> 依赖里 `fastmcp` 会拉不少包，**国内网络走代理时安装可能超过 15 分钟**，
> 且 `pip --quiet` 看不到进度，容易误以为卡死。耐心等，或临时换源。

## 认证

支持三种方式（环境变量 `P115_COOKIES` / `P115_COOKIES_PATH` 指向文件 / 二维码登录）。

推荐用 `P115_COOKIES_PATH`，把 cookie 放在**仓库之外**的路径，避免误提交。

### 扫码登录（推荐）

`p115client` 的扫码流程是：`login_qrcode_token()` → 轮询 `login_qrcode_scan_status(payload)`
→ 成功后 `login_qrcode_scan_result(uid, app)` 取 cookie。

**这里有三个坑，都是实际踩过的：**

#### 坑 1：`login_qrcode_scan_status` 收的是 payload 字典，不是 uid 字符串

必须传完整的三元组：

```python
P115Client.login_qrcode_scan_status({"uid": uid, "time": time, "sign": sign})
```

只传 uid 会一直返回 `{"state":0,"code":40199002,"message":"key invalid"}`，
表现为「轮询永远没动静」，非常容易被误判成网络问题。

#### 坑 2：未扫描时 `data` 是空字典，不是 `{"status":0}`

等待扫码阶段返回的是：

```json
{"state": 1, "code": 0, "message": "", "data": {}}
```

只有扫到之后才出现 `data.status`：

| status | 含义 |
|---|---|
| 1 | 已扫描，**待手机确认** |
| 2 | 已确认，登录成功 |
| -1 | 二维码过期 |
| -2 | 已取消 |

所以 `status` 为 `None` 必须当作「继续等待」，不能当异常退出。
（注意：上游 `p115client` 自带的 `login_with_qrcode` 在 `case _` 分支直接抛错，照抄它的判定会误判。）

**另外：扫码后必须在手机上点「确认登录」。** 只扫不点会一直停在 `status=1`。

#### 坑 3（最坑）：`login_qrcode_scan_result` 是一次性的，且 `data.cookie` 是 dict

两个子问题叠加，会**永久丢失凭据**：

1. 令牌**一次性**，调用成功一次即被消耗，再调返回 `40101017 老乡验证失败！`。
   所以**不能**用循环去「先探测能不能拿到、再取一次」。
2. `data.cookie` 返回的是 **dict**（`{'UID':..., 'CID':..., 'SEID':..., 'KID':...}`），
   **不是 str**。直接 `write_text(dict)` 会 `TypeError` 崩掉 —— 而崩的那一刻令牌已被消耗，
   凭据就再也拿不回来了。

**正确做法：拿到响应第一件事就是把原始 JSON 无条件写盘，再做解析。**

```python
res = P115Client.login_qrcode_scan_result(uid, app="web")
pathlib.Path("raw.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")  # 先存！

d = res.get("data") or {}
ck = d.get("cookie")
cookie = ck if isinstance(ck, str) else "; ".join(f"{k}={v}" for k, v in ck.items())
```

补充：`app="web"` 可用；`desktop` / `android` / `ios` / `alipaymini` 会报 `40101017`。

二维码内容就是 token 响应里的 `qrcode` 字段，形如 `https://115.com/scan/dg-<uid>`，
有效期约 5–7 分钟。

### 不要试图复用桌面客户端的登录态

115 桌面客户端的会话文件在 `%LOCALAPPDATA%\Life115\session.bin`，是 **DPAPI 加密**的
（文件头 `01000000d08c9ddf0115d1118c7a00c0` 是标准 DPAPI provider GUID）。
但用 `CryptUnprotectData` 无熵调用会返回 **err=13 ERROR_INVALID_DATA**，
说明客户端传了自定义熵（optional entropy），无法离线解开。直接扫码更省事。

## MCP 客户端配置

`mcp.json` 里加一段（**把路径换成你自己的，cookie 文件不要放进仓库**）：

```json
{
  "mcpServers": {
    "115-upload": {
      "command": "<repo>/.venv/Scripts/python.exe",
      "args": ["-m", "mcp_115_server"],
      "env": {
        "P115_COOKIES_PATH": "<仓库外的路径>/115-cookies.txt",
        "P115_DEBUG_LOGGING": "false"
      }
    }
  }
}
```

默认 transport 是 `stdio`。想跑 HTTP 模式：

```bash
./.venv/Scripts/python -m mcp_115_server --transport http --port 8000 --path /mcp
```

## 注意事项

- **单文件上传工具 `upload_local_file` 本身没有目录递归**；需要整目录上传请改用
  `batch_upload`（见 `docs/BATCH-UPLOAD.md`）。
- `client.user_space_info()` 在 `app="web"` 身份下会报 `errno 99 请重新登录`
  （它走的是 `/android/user/space_info`）。查容量改用 `get_storage_info` 或 `get_account_info`。
- p115client 新 API 的字段名和旧文档不一致：文件列表用 `n`(名称) / `cid`(ID) / `fc`(0=目录)，
  不是老版的 `fn` / `fid`。
  > 注：这一条只适用于旧版 `fs_files` 系接口。`fs_files_open`（批量上传走的就是它）
  > 返回里的 id 字段**目录和文件都在 `fid`** —— 两个接口的字段命名规则不同，别混用。
- 调用 115 接口时若本机有代理，建议清空代理环境变量，否则可能被拦。
- `P115Client` **不是线程安全的**：并发查询实测 8/8 失败（即使每个线程独立建实例）。
  批量上传的做法是「元数据查询全部主线程串行 + 线程池只搬数据」。

## 已验证记录

在本机（Windows，Python 3.13.12）实测：

- 扫码登录成功，凭据字段 `UID / CID / SEID / KID`
- 上传 62 字节测试文件 → 返回 `file_id` → 列表确认存在 → `fs_delete` 删除 → 列表确认已消失
- MCP 服务以 stdio 启动正常，`tools/list` 返回 60 个工具（含新增 `batch_upload_plan` / `batch_upload`）
- 批量上传：`batch_upload_plan` 对 51 文件 / 104697 字节的子树返回正确统计；
  `fs_list` 返回 `state=True` 并读到 `ARRIRAW` 目录；`save_state` / `load_state` 往返一致
- 修掉上游 `service.py` 里 `P115Client(**check_for_relogin=...)` 引发的
  `TypeError: unexpected keyword argument`（该问题会导致**整个服务起不来**）；
  现由 `_build_client()` 用 `inspect.signature` 过滤参数，兼容不同 p115client 版本
- 并发吞吐实测（6.88 MB/文件）：并发 1 → 8.9 MB/s，4 → 20.6，8 → 30.2；16/24 无提升，32 报错
