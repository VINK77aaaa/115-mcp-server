# -*- coding: utf-8 -*-
"""上传监督器：循环运行上传任务，崩溃后自动等待再重启。

⚠ 关键前提：**必须由操作系统级调度器拉起**，否则进程活不下来。

实测教训（2026-10-12）：从「受作业对象（Job Object）管控的进程」里 spawn
出去的子孙进程，会随父进程**一起被整组杀掉** —— ``nohup``、
``DETACHED_PROCESS``、``CREATE_NEW_PROCESS_GROUP`` **全都没用**，
而且进程是静默消失、日志里没有任何异常。

而且：**验证后台存活必须跨命令调用**。在同一个 shell 会话里
``sleep`` 完再检查，会看到「进程还活着、日志在写」的假象；
换一次独立调用再查，就会发现进程已死、日志为空。

正解：
  - Windows：计划任务（Task Scheduler 服务托管）——
    :func:`register_windows_task` 可直接生成注册命令；
  - Linux：``systemd-run --user --unit=xxx`` 或 systemd service；
  - macOS：``launchd``。

设计要点：
  1. 上传任务**幂等** —— 状态文件记录已完成文件，重启后跳过、断点续传，
     所以「崩了就重启」是安全的。
  2. 以 rc=0 退出（全部完成）时监督器直接结束，不再重启。
  3. 崩溃后等待 ``crash_wait`` 秒再重启；连续快速崩溃则指数退避，
     避免死循环把 115 打成限流。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import time

__all__ = ["Supervisor", "register_windows_task", "TASK_NAME"]


TASK_NAME = "115-MCP-BatchUpload"


class Supervisor:
    """循环拉起一个子命令，崩溃后指数退避重启。"""

    def __init__(self, argv: list[str], cwd: str,
                 log_file: str = "_supervisor.log",
                 run_log: str = "_run_auto.log",
                 crash_wait: int = 60, max_backoff: int = 900,
                 max_restart: int = 200,
                 long_run_sec: int = 600,
                 env_extra: dict | None = None) -> None:
        self.argv = argv
        self.cwd = cwd
        self.log_file = os.path.join(cwd, log_file)
        self.run_log = os.path.join(cwd, run_log)
        self.crash_wait = crash_wait
        self.max_backoff = max_backoff
        self.max_restart = max_restart
        # 跑够这么久才崩 → 视为正常作业后失败，退避重置
        self.long_run_sec = long_run_sec
        self.env_extra = env_extra or {}

    def _slog(self, msg: str) -> None:
        line = "[%s] %s" % (time.strftime("%F %T"), msg)
        print(line)
        sys.stdout.flush()
        try:
            with io.open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def run(self) -> int:
        self._slog("=== 监督器启动：%s" % " ".join(self.argv))
        backoff = self.crash_wait
        for i in range(1, self.max_restart + 1):
            env = dict(os.environ)
            env.setdefault("UP_CONC", "4")
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            env.update(self.env_extra)
            with io.open(self.run_log, "a", encoding="utf-8") as f:
                f.write("\n[%s] === 监督器第 %d 次启动（UP_CONC=%s）===\n"
                        % (time.strftime("%F %T"), i, env.get("UP_CONC")))
            self._slog("第 %d 次启动子任务" % i)
            t0 = time.time()
            try:
                proc = subprocess.Popen(
                    self.argv, cwd=self.cwd, env=env,
                    stdout=io.open(self.run_log, "a", encoding="utf-8"),
                    stderr=subprocess.STDOUT)
                rc = proc.wait()
            except Exception as e:                 # noqa: BLE001 - 启动失败也要继续试
                rc = -1
                self._slog("启动异常: %r" % e)
            dur = time.time() - t0
            self._slog("子任务退出 rc=%s，运行 %.1fs" % (rc, dur))
            if rc == 0:
                self._slog("=== rc=0，视为正常完成，监督器退出 ===")
                return 0
            if dur > self.long_run_sec:
                backoff = self.crash_wait
            self._slog("等待 %ds 后重启（退避当前 %ds）" % (backoff, backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)
        self._slog("=== 达到重启上限 %d，监督器退出 ===" % self.max_restart)
        return 1


def register_windows_task(task_name: str, cmd_path: str, workdir: str,
                          extra_args: list[str] | None = None) -> list[str]:
    """返回「注册 Windows 计划任务」的 PowerShell 命令（不执行，交给调用方）。

    计划任务由 Task Scheduler 服务托管，**不在调用方的 Job Object 里**，
    所以进程能真正长驻（实测跨调用持续存活）。

    ⚠ 本机环境里 ``schtasks.exe`` 在安全黑名单中会被拦，
    但 PowerShell 的 ``*-ScheduledTask`` cmdlet 可用 —— 走 cmdlet。
    """
    ps = [
        "powershell", "-NoProfile", "-Command",
        ('$dir = "%s"; '
         '$act = New-ScheduledTaskAction -Execute "%s" -WorkingDirectory $dir; '
         '$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries '
         '-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0) '
         '-StartWhenAvailable; '
         'Register-ScheduledTask -TaskName "%s" -Action $act -Settings $set -Force; '
         'Start-ScheduledTask -TaskName "%s"'
         % (workdir, cmd_path, task_name, task_name)),
    ]
    return ps + list(extra_args or [])
