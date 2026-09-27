"""Alembic 迁移环境。

要点：
- 连接串**不在 alembic.ini 中硬编码**，统一从项目根 `.env` 经 `app.core.config` 读取，
  避免凭据进入版本库（docs/architecture.md §15.1）。
- 导入 `app.models` 以注册全部 18 张表，autogenerate 才能感知它们。
- `compare_type` / `compare_server_default` 打开，让类型与默认值变更也能被检出。
- 生成脚本时按 docs/data-model.md §1.1 的命名约定输出索引/约束名。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 alembic 能 import app.*（alembic/env.py -> backend/）
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402
import app.models  # noqa: E402,F401  导入即注册全部表

config = context.config

# 用 .env 中的连接串覆盖 alembic.ini 的占位值（% 需转义，否则 ConfigParser 会报错）
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库。"""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        # 索引/约束名已由 MetaData 的 naming_convention 固化，无需重复渲染
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：直连数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
