"""环境自检脚本。

一键确认开发/演示环境是否就绪，避免"跑起来才发现连不上"。

用法：
    python scripts/check_env.py          # 检查全部
    python scripts/check_env.py --quiet  # 仅输出结论

退出码：0 = 全部通过，1 = 有失败项
"""

from __future__ import annotations

import argparse
import importlib.metadata
import sys
from pathlib import Path

# Windows 控制台默认代码页是 GBK（936），直接打印 ✓ / ✗ 会抛 UnicodeEncodeError。
# 必须在任何输出之前把 stdout 切到 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


# ==================== 输出工具 ====================

class C:
    """终端颜色。Windows 终端可能不支持，失败时降级为无颜色。"""
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    DIM = "\033[90m"
    END = "\033[0m"

    @classmethod
    def disable(cls) -> None:
        cls.OK = cls.FAIL = cls.WARN = cls.DIM = cls.END = ""


def ok(msg: str) -> None:
    print(f"  {C.OK}✓{C.END} {msg}")


def fail(msg: str) -> None:
    print(f"  {C.FAIL}✗{C.END} {msg}")


def warn(msg: str) -> None:
    print(f"  {C.WARN}!{C.END} {msg}")


def section(title: str) -> None:
    print(f"\n{C.DIM}[{title}]{C.END}")


# ==================== 检查项 ====================

def check_config() -> tuple[bool, list[str]]:
    """配置加载检查。返回 (是否通过, 警告列表)。"""
    section("配置")
    warnings: list[str] = []
    try:
        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001
        fail(f"配置加载失败: {type(exc).__name__}: {exc}")
        return False, warnings

    ok(f"环境: {settings.app_env}  日志级别: {settings.log_level}")
    ok(f"后端: http://{settings.app_host}:{settings.app_port}")
    ok(f"mock 审批: {settings.mock_approval_base_url}")

    # LLM 配置完整性
    if settings.use_real_llm:
        ok(f"LLM: {settings.llm_provider} | model={settings.llm_model}")
        ok(f"     base_url={settings.llm_base_url}")
    else:
        warn(f"LLM 回落到 mock（provider={settings.llm_provider}）")
        warnings.append("LLM 未配置完整，将使用 MockProvider")

    # 敏感项不应为空
    if not settings.mysql_password:
        fail("MYSQL_PASSWORD 为空")
        return False, warnings
    return True, warnings


def check_mysql() -> bool:
    section("MySQL")
    try:
        from app.core.database import check_connection
    except Exception as exc:  # noqa: BLE001
        fail(f"导入失败: {exc}")
        return False
    success, detail = check_connection()
    if success:
        ok(detail)
        return True
    fail(detail)
    return False


def check_redis() -> bool:
    section("Redis")
    try:
        from app.core.redis_client import check_connection
    except Exception as exc:  # noqa: BLE001
        fail(f"导入失败: {exc}")
        return False
    success, detail = check_connection()
    if success:
        ok(detail)
        return True
    fail(detail)
    return False


def check_minio() -> bool:
    section("MinIO")
    try:
        from app.core.minio_client import check_connection, ensure_buckets
    except Exception as exc:  # noqa: BLE001
        fail(f"导入失败: {exc}")
        return False
    success, detail = check_connection()
    if not success:
        fail(detail)
        return False
    ok(detail)
    created = ensure_buckets()
    if created:
        ok(f"已创建 bucket: {created}")
    else:
        ok("bucket 已存在")
    return True


def check_schema() -> tuple[bool, list[str]]:
    """数据层检查：迁移版本是否到位、种子数据是否写入。"""
    section("数据层")
    warnings: list[str] = []

    try:
        from sqlalchemy import inspect, text
        from app.core.database import engine
    except Exception as exc:  # noqa: BLE001
        fail(f"导入失败: {exc}")
        return False, warnings

    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
    except Exception as exc:  # noqa: BLE001
        fail(f"读取表清单失败: {exc}")
        return False, warnings

    from app.models import TABLE_ORDER

    missing = [t for t in TABLE_ORDER if t not in tables]
    if missing:
        fail(f"缺少表: {missing}")
        warn("请执行: cd backend && python -m alembic upgrade head")
        return False, warnings
    ok(f"18 张表齐备（含 alembic_version 共 {len(tables)} 张）")

    # 迁移版本：对比 head 与当前
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from alembic.runtime.migration import MigrationContext

        backend_dir = PROJECT_ROOT / "backend"
        cfg = Config(str(backend_dir / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend_dir / "alembic"))
        head = ScriptDirectory.from_config(cfg).get_current_head()

        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()

        if current == head:
            ok(f"迁移版本已是最新: {head}")
        else:
            fail(f"迁移版本落后: 当前={current} head={head}")
            warn("请执行: cd backend && python -m alembic upgrade head")
            return False, warnings
    except Exception as exc:  # noqa: BLE001
        warn(f"迁移版本检查跳过: {type(exc).__name__}: {exc}")
        warnings.append("迁移版本未校验")
        return True, warnings

    # 种子数据
    try:
        from app.core.database import SessionLocal
        from app.models import Rule, RuleTemplate, StandardClause, SubjectBlacklist

        with SessionLocal() as db:
            counts = {
                "规则模板": db.query(RuleTemplate).count(),
                "规则": db.query(Rule).count(),
                "标准条款": db.query(StandardClause).count(),
                "黑名单": db.query(SubjectBlacklist).count(),
            }
        empty = [k for k, v in counts.items() if v == 0]
        detail = " | ".join(f"{k}={v}" for k, v in counts.items())
        if empty:
            warn(f"种子数据未写入: {detail}")
            warn("请执行: python scripts/seed_data.py")
            warnings.append(f"种子数据缺失: {empty}")
        else:
            ok(f"种子数据: {detail}")
    except Exception as exc:  # noqa: BLE001
        warn(f"种子数据检查失败: {type(exc).__name__}: {exc}")
        warnings.append("种子数据未校验")

    return True, warnings


def check_host_capabilities() -> tuple[bool, list[str]]:
    """宿主能力检查：WPS COM / PyMuPDF / RapidOCR。"""
    section("宿主能力")
    warnings: list[str] = []

    # PyMuPDF
    try:
        import pymupdf
        ok(f"PyMuPDF {pymupdf.__version__}")
    except Exception as exc:  # noqa: BLE001
        fail(f"PyMuPDF 不可用: {exc}")
        return False, warnings

    # python-docx
    try:
        import docx
        ok(f"python-docx {docx.__version__}")
    except Exception as exc:  # noqa: BLE001
        fail(f"python-docx 不可用: {exc}")
        return False, warnings

    # RapidOCR（延迟加载，初始化较慢）
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        ok("RapidOCR 可导入（初始化耗时约 2.4s，此处不实例化）")
    except Exception as exc:  # noqa: BLE001
        fail(f"RapidOCR 不可用: {exc}")
        return False, warnings

    # WPS COM（Windows 专有）
    if sys.platform != "win32":
        warn("非 Windows 平台，WPS COM 不可用（DOCX 分页将降级）")
        warnings.append("非 Windows 平台，DOCX 将走 passthrough 降级")
        return True, warnings

    try:
        import pythoncom
        import win32com.client as win32

        pythoncom.CoInitialize()
        try:
            app = win32.Dispatch("Word.Application")
            path = app.Path
            version = app.Version
            app.Quit()
        finally:
            pythoncom.CoUninitialize()
        ok(f"WPS COM 可用: version={version}")
        print(f"      {C.DIM}{path}{C.END}")
    except Exception as exc:  # noqa: BLE001
        warn(f"WPS COM 不可用: {type(exc).__name__}: {exc}")
        warn("DOCX 分页将降级到 passthrough（段落级锚点）")
        warnings.append("WPS COM 不可用，DOCX 将降级")
    return True, warnings


def check_llm() -> tuple[bool, list[str]]:
    """LLM 连通性检查（真实调用，会消耗少量额度）。"""
    section("LLM 连通性")
    warnings: list[str] = []
    try:
        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001
        fail(f"配置加载失败: {exc}")
        return False, warnings

    if not settings.use_real_llm:
        warn("使用 MockProvider，跳过真实调用")
        return True, warnings

    try:
        import httpx

        resp = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": "回复 OK"}],
                "max_tokens": 5,
            },
            timeout=30.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            ok(f"模型响应正常: {content.strip()[:30]!r}")
            usage = data.get("usage", {})
            print(f"      {C.DIM}tokens: prompt={usage.get('prompt_tokens')} "
                  f"completion={usage.get('completion_tokens')}{C.END}")
        else:
            fail(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return False, warnings
    except Exception as exc:  # noqa: BLE001
        fail(f"{type(exc).__name__}: {exc}")
        return False, warnings
    return True, warnings


def check_dependencies() -> bool:
    """核心依赖导入检查。"""
    section("核心依赖")
    modules = [
        ("fastapi", "FastAPI"),
        ("uvicorn", "uvicorn"),
        ("sqlalchemy", "SQLAlchemy"),
        ("alembic", "Alembic"),
        ("pymysql", "PyMySQL"),
        ("redis", "redis"),
        ("minio", "minio"),
        ("openai", "openai"),
        ("httpx", "httpx"),
        ("pydantic_settings", "pydantic-settings"),
    ]
    all_ok = True
    for mod_name, label in modules:
        try:
            mod = __import__(mod_name)
        except Exception as exc:  # noqa: BLE001
            fail(f"{label} 导入失败: {exc}")
            all_ok = False
            continue
        # 版本号以包元数据为准：部分库（如 PyMySQL）的 __version__ 与
        # 实际发布版本不一致，用 __version__ 会显示错误版本号。
        try:
            version = importlib.metadata.version(mod_name)
        except importlib.metadata.PackageNotFoundError:
            version = getattr(mod, "__version__", "?")
        ok(f"{label} {version}")
    return all_ok


# ==================== 主流程 ====================

def main() -> int:
    parser = argparse.ArgumentParser(description="环境自检")
    parser.add_argument("--quiet", action="store_true", help="仅输出结论")
    parser.add_argument("--skip-llm", action="store_true", help="跳过 LLM 真实调用")
    args = parser.parse_args()

    if args.quiet:
        C.disable()

    print(f"{C.DIM}项目根: {PROJECT_ROOT}{C.END}")

    results: dict[str, bool] = {}
    all_warnings: list[str] = []

    passed, warns = check_config()
    results["配置"] = passed
    all_warnings.extend(warns)
    if not passed:
        return _summary(results, all_warnings)

    results["依赖"] = check_dependencies()
    results["MySQL"] = check_mysql()
    results["Redis"] = check_redis()
    results["MinIO"] = check_minio()

    passed, warns = check_schema()
    results["数据层"] = passed
    all_warnings.extend(warns)

    passed, warns = check_host_capabilities()
    results["宿主能力"] = passed
    all_warnings.extend(warns)

    if not args.skip_llm:
        passed, warns = check_llm()
        results["LLM"] = passed
        all_warnings.extend(warns)

    return _summary(results, all_warnings)


def _summary(results: dict[str, bool], warnings: list[str]) -> int:
    print(f"\n{C.DIM}{'─' * 50}{C.END}")
    failed = [k for k, v in results.items() if not v]
    for name, passed in results.items():
        mark = f"{C.OK}PASS{C.END}" if passed else f"{C.FAIL}FAIL{C.END}"
        print(f"  {name:10} {mark}")

    if warnings:
        print(f"\n{C.WARN}警告:{C.END}")
        for w in warnings:
            print(f"  - {w}")

    if failed:
        print(f"\n{C.FAIL}环境未就绪：{', '.join(failed)}{C.END}")
        return 1
    print(f"\n{C.OK}环境就绪 ✓{C.END}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
