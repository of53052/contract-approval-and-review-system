# -*- coding: utf-8 -*-
"""一键停止：收尾 `start_all.py` 拉起的原生服务。

用法（在项目根目录）：
    python scripts/stop_all.py            # 停 mock 审批 / 后端 / 前端
    python scripts/stop_all.py --infra    # 连 Docker 基础设施一起停（会保留数据卷）

**安全约束**：只终止 `.run/*.json` 记录过的 PID；记录缺失时才按端口反查，
且必须先校验命令行里含本项目特征（uvicorn / vite / mock-approval），
避免误杀恰好占用同端口的无关进程。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = PROJECT_ROOT / ".run"

#: 服务名 → (端口, 命令行必须同时包含的标记)。
#: 端口反查时用它们做归属校验，避免误杀恰好占用同端口的无关进程。
#: 不用服务名做标记：mock 审批的命令行是 `uvicorn app.main:app`，
#: 服务名来自工作目录而非命令行（实测漏杀过一次）。
SERVICES = {
    "mock-approval": (8010, ("uvicorn", "--port 8010")),
    "backend": (8000, ("uvicorn", "--port 8000")),
    "frontend": (5173, ("vite", "--port 5173")),
}


class C:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    DIM = "\033[90m"
    BOLD = "\033[1m"
    END = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {C.OK}✓{C.END} {msg}")


def warn(msg: str) -> None:
    print(f"  {C.WARN}!{C.END} {msg}")


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pid_alive(pid: int) -> bool:
    """Windows 下用 tasklist 判断进程是否存在。"""
    r = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return str(pid) in (r.stdout or "")


def command_line_of(pid: int) -> str:
    """取进程命令行，用于端口反查时的归属校验。"""
    ps = (
        f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\")"
        f".CommandLine"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (r.stdout or "").strip()


def pid_on_port(port: int) -> int | None:
    """按监听端口反查 PID（仅 Windows）。"""
    r = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def kill_tree(pid: int) -> None:
    """终止进程树。vite/uvicorn 会派生子进程，必须连子进程一起收。"""
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def stop_service(name: str, port: int, markers: tuple[str, ...], state: dict) -> bool:
    """停止单个服务。返回是否执行了终止操作。"""
    rec = state.get(name)
    pid = rec.get("pid") if rec else None

    if pid is not None and pid_alive(pid):
        kill_tree(pid)
        ok(f"{name} 已停止 (pid={pid})")
        return True

    # 记录缺失或进程已消失：若端口仍被占用，按端口反查并校验归属
    if not port_open(port):
        if rec:
            ok(f"{name} 已不在运行")
        else:
            ok(f"{name} 未运行（无记录）")
        return False

    fallback = pid_on_port(port)
    if fallback is None:
        warn(f"{name}: 端口 {port} 被占用但无法定位 PID，请手动处理")
        return False
    cmd = command_line_of(fallback)
    missing = [m for m in markers if m.lower() not in cmd.lower()]
    if missing:
        warn(
            f"{name}: 端口 {port} 的占用进程 (pid={fallback}) 命令行缺少标记 {missing}，"
            "判定为无关进程，**不终止**"
        )
        warn(f"  命令行: {cmd[:160]}")
        return False
    kill_tree(fallback)
    ok(f"{name} 已停止 (pid={fallback}，端口反查)")
    return True


def stop_infra() -> None:
    print(f"\n{C.BOLD}▸ Docker 基础设施{C.END}")
    r = subprocess.run(
        ["docker", "compose", "stop"], cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode == 0:
        ok("容器已停止（数据卷保留，下次启动数据仍在）")
    else:
        warn(f"停止失败: {(r.stderr or r.stdout or '').strip()[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="停止合同审查系统的本地服务")
    ap.add_argument("--infra", action="store_true", help="同时停止 Docker 基础设施")
    args = ap.parse_args()

    print(f"{C.BOLD}合同审查系统 · 停止中{C.END}")

    state: dict = {}
    if RUN_DIR.exists():
        for f in RUN_DIR.glob("*.json"):
            try:
                state[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                warn(f"忽略损坏的状态文件: {f.name}")

    print(f"\n{C.BOLD}▸ 原生服务{C.END}")
    for name, (port, markers) in SERVICES.items():
        stop_service(name, port, markers, state)

    # 清理状态文件（进程已收，记录无意义）
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR, ignore_errors=True)
        print(f"  {C.DIM}已清理 .run/ 状态文件{C.END}")

    if args.infra:
        stop_infra()

    print(f"\n{C.OK}已收尾。{C.END}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
