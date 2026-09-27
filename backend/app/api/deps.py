"""路由层共享依赖。"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session

from app.core.database import SessionLocal


def get_db() -> Generator[Session, None, None]:
    """请求级数据库会话。

    每个请求一个会话，请求结束关闭。**不在这里 commit**——
    提交由各路由显式控制，避免"读接口也提交"这种隐性行为。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
