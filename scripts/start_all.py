# -*- coding: utf-8 -*-
"""一键启动：基础设施 → 数据初始化 → mock 审批 → 后端 → 前端。

设计依据：docs/architecture.md §4（部署架构）。README §2 的分步命令被本脚本收敛成一条。

用法（在项目根目录）：
    python scripts/start_all.py            # 全量启动
    python scripts/start_all.py --open     # 启动完自动打开浏览器
    python scripts/start_all.py --skip-seed  # 跳过建表与种子数据（快速重启）

**幂等**：已在监听且 /health 正常的服务会被跳过，不会重复拉起。
启动的进程 PID 写入 `.run/*.json`，由 `stop_all.py` 收尾。

**为什么不用 `docker compose up` 之外的编排**：阶段一只做基础设施容器化，
后端/前端原生运行（DOCX 分页依赖 WPS COM 的交互式桌面会话）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

def _force_utf8(*streams) -> None:
    """把输出流切到 UTF-8。

    Windows 控制台默认代码页是 GBK（936），直接打印中文或 ✓/✗ 会抛
    UnicodeEncodeError。用 `getattr` 取 `reconfigure` 而非直接属性访问：
    它是 CPython `TextIOWrapper` 的扩展方法，静态类型（`TextIO`）里没有声明，
    直接写 `sys.stderr.reconfigure(...)` 会触发 Pylance
    `reportAttributeAccessIssue`（stdout 因 `hasattr` 收窄才侥幸不报）。
    """

    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


_force_utf8(sys.stdout, sys.stderr)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
MOCK_DIR = PROJECT_ROOT / "mock-approval"
RUN_DIR = PROJECT_ROOT / ".run"
VENV_PY = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
ENV_FILE = PROJECT_ROOT / ".env"

#: 容器名 → 期望 healthy 的等待上限（秒）
CONTAINERS = {"cr-mysql": 120, "cr-redis": 60, "cr-minio": 90}

#: 原生服务：(名称, 端口, 探活路径)
SERVICES = [
    ("mock 审批", 8010, "/health"),
    ("后端", 8000, "/health"),
    ("前端", 5173, "/"),
]


# ==================== 输出 ====================

class C:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    DIM = "\033[90m"
    BOLD = "\033[1m"
    END = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {C.OK}✓{C.END} {msg}")


def fail(msg: str) -> None:
    print(f"  {C.FAIL}✗{C.END} {msg}")


def warn(msg: str) -> None:
    print(f"  {C.WARN}!{C.END} {msg}")


def step(msg: str) -> None:
    print(f"\n{C.BOLD}▸ {msg}{C.END}")


class StartupError(RuntimeError):
    """启动失败。调用方捕获后打印提示并退出，不静默继续。"""


# ==================== 通用工具 ====================

def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def http_ok(port: int, path: str = "/health", timeout: float = 2.0) -> bool:
    """探活：能连通且状态码 < 500 视为可用。"""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:  # noqa: BLE001 - 探活失败即"未就绪"
        return False


def wait_until(predicate, timeout: float, interval: float = 0.5) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


def spawn(args: list[str], cwd: Path, log: Path) -> int:
    """分离启动子进程，输出重定向到日志文件，返回 PID。

    用 DETACHED_PROCESS：服务不能随启动脚本退出而终止。
    """
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as fh:
        p = subprocess.Popen(
            args, cwd=str(cwd), stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    return p.pid


def record(name: str, pid: int, port: int, log: Path) -> None:
    RUN_DIR.mkdir(exist_ok=True)
    (RUN_DIR / f"{name}.json").write_text(
        json.dumps({"pid": pid, "port": port, "log": str(log)}, ensure_ascii=False),
        encoding="utf-8",
    )


def run_checked(args: list[str], cwd: Path, what: str, timeout: float = 300) -> None:
    """执行一个必须成功的同步命令，失败即抛 StartupError（含完整输出）。"""
    proc = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
        raise StartupError(f"{what} 失败（退出码 {proc.returncode}）:\n{tail}")


# ==================== 各阶段 ====================

def stage_preflight() -> None:
    step("前置检查")
    if not ENV_FILE.exists():
        example = PROJECT_ROOT / ".env.example"
        if not example.exists():
            raise StartupError("缺少 .env 且无 .env.example，无法确定配置")
        shutil.copyfile(example, ENV_FILE)
        warn(".env 不存在，已从 .env.example 复制（请按需修改端口与凭据）")

    if not VENV_PY.exists():
        raise StartupError(
            f"虚拟环境不存在: {VENV_PY}\n"
            "请先创建: cd backend && python -m venv .venv && .venv\\Scripts\\pip install -r requirements.txt"
        )
    ok(f"虚拟环境 {VENV_PY.relative_to(PROJECT_ROOT)}")

    if shutil.which("docker") is None:
        raise StartupError("未找到 docker 命令（基础设施依赖 Docker Desktop）")
    ok("docker 命令可用")

    if not (FRONTEND_DIR / "node_modules").exists():
        warn("前端依赖未安装，正在执行 npm install（首次约需 1 分钟）…")
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if npm is None:
            raise StartupError("未找到 npm，无法安装前端依赖")
        run_checked([npm, "install"], FRONTEND_DIR, "npm install")
    ok("前端依赖 node_modules")


def stage_infra() -> None:
    step("基础设施（Docker）")
    proc = subprocess.run(
        ["docker", "info"], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise StartupError("Docker 未运行，请先启动 Docker Desktop")

    run_checked(["docker", "compose", "up", "-d"], PROJECT_ROOT, "docker compose up")

    for name, timeout in CONTAINERS.items():
        def healthy(n: str = name) -> bool:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", n],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            return r.returncode == 0 and r.stdout.strip() == "healthy"

        if wait_until(healthy, timeout):
            ok(f"{name} healthy")
        else:
            raise StartupError(
                f"{name} 未在 {timeout}s 内变为 healthy，请执行 docker ps 查看"
            )


def stage_data() -> None:
    step("数据初始化（幂等）")
    # 建表：alembic 必须在 backend/ 下执行（script_location 用 %(here)s 定位）
    run_checked(
        [str(VENV_PY), "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        BACKEND_DIR, "alembic upgrade head",
    )
    ok("迁移已到 head")
    run_checked([str(VENV_PY), "scripts/seed_data.py"], PROJECT_ROOT, "种子数据")
    ok("规则库 / 标准条款 / 黑名单已就绪")
    run_checked([str(VENV_PY), "seed.py"], MOCK_DIR, "mock 审批种子数据")
    ok("mock 审批待办已就绪")


def stage_mock() -> None:
    step("mock 审批系统")
    if port_open(8010):
        if http_ok(8010):
            ok("已在运行，跳过")
            return
        raise StartupError("端口 8010 被占用但不是 mock 审批服务，请先释放该端口")
    log = PROJECT_ROOT / "logs" / "mock-approval.log"
    pid = spawn(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010"],
        MOCK_DIR, log,
    )
    if not wait_until(lambda: http_ok(8010), 30):
        raise StartupError(f"mock 审批启动超时，查看日志: {log}")
    record("mock-approval", pid, 8010, log)
    ok(f"已启动 (pid={pid})")


def stage_backend() -> None:
    step("后端 API")
    if port_open(8000):
        if http_ok(8000):
            ok("已在运行，跳过")
            return
        raise StartupError("端口 8000 被占用但不是后端服务，请先释放该端口")
    log = BACKEND_DIR / "uvicorn.log"
    pid = spawn(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        BACKEND_DIR, log,
    )
    if not wait_until(lambda: http_ok(8000), 60):
        raise StartupError(f"后端启动超时，查看日志: {log}")
    record("backend", pid, 8000, log)
    ok(f"已启动 (pid={pid})")


def stage_frontend() -> None:
    step("前端 dev server")
    if port_open(5173):
        ok("已在运行，跳过")
        return
    node = shutil.which("node")
    vite_js = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
    if node is None or not vite_js.exists():
        raise StartupError("未找到 node 或 vite，请检查前端依赖")
    log = FRONTEND_DIR / "vite.log"
    # 直接跑 vite.js：npm run dev 经 npm.cmd 批处理层在本机起不来（实测）
    pid = spawn([node, str(vite_js), "--host", "127.0.0.1", "--port", "5173"], FRONTEND_DIR, log)
    if not wait_until(lambda: port_open(5173), 60):
        raise StartupError(f"前端启动超时，查看日志: {log}")
    record("frontend", pid, 5173, log)
    ok(f"已启动 (pid={pid})")


def print_banner() -> None:
    print(f"\n{C.OK}{C.BOLD}{'=' * 62}{C.END}")
    print(f"{C.OK}{C.BOLD}  合同审批审查系统已就绪{C.END}")
    print(f"{C.OK}{C.BOLD}{'=' * 62}{C.END}")
    print(f"  工作台/大盘   {C.BOLD}http://127.0.0.1:5173{C.END}")
    print(f"  接口文档      http://127.0.0.1:8000/docs")
    print(f"  mock 审批     http://127.0.0.1:8010/docs")
    print(f"  MinIO 控制台  http://127.0.0.1:19001")
    print(f"\n  {C.DIM}演示步骤见 README §3；停止服务执行 python scripts/stop_all.py{C.END}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="一键启动合同审查系统")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    ap.add_argument("--skip-seed", action="store_true", help="跳过建表与种子数据")
    args = ap.parse_args()

    print(f"{C.BOLD}合同审查系统 · 启动中{C.END}")

    try:
        stage_preflight()
        stage_infra()
        if not args.skip_seed:
            stage_data()
        stage_mock()
        stage_backend()
        stage_frontend()
    except StartupError as exc:
        print()
        fail(str(exc))
        print(f"\n{C.WARN}启动中断。已启动的服务可用 python scripts/stop_all.py 清理。{C.END}")
        return 1
    except subprocess.TimeoutExpired as exc:
        fail(f"命令超时: {exc.cmd}")
        return 1

    print_banner()
    if args.open:
        import webbrowser
        webbrowser.open("http://127.0.0.1:5173")
    return 0


if __name__ == "__main__":
    sys.exit(main())
