"""MySQL 连接与会话管理。

设计要点：
- 用 SQLAlchemy 2.0 的声明式风格
- pool_pre_ping 应对容器重启后的失效连接
- Session 通过 FastAPI 依赖注入，不在业务层手动创建
"""

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


engine = create_engine(
    settings.database_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,  # 连接失效时自动重连，避免容器重启后报错
    pool_recycle=3600,   # 1 小时回收，规避 MySQL wait_timeout
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供数据库会话，请求结束后自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_connection() -> tuple[bool, str]:
    """连通性自检。返回 (是否成功, 详情)。供 scripts/check_env.py 使用。"""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT VERSION(), @@character_set_server, @@sql_mode, DATABASE()")
            ).fetchone()
        return True, f"MySQL {row[0]} | charset={row[1]} | db={row[3]}"
    except Exception as exc:  # noqa: BLE001 - 自检需捕获全部异常并转为可读信息
        return False, f"{type(exc).__name__}: {exc}"
