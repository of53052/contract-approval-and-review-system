"""ORM 列类型与列工厂。

把 docs/data-model.md §1.1 的命名/类型约定固化为一组工厂函数，
避免 18 张表里重复书写类型细节导致漂移。

要点：
- `BIGINT UNSIGNED` 主键与 `DATETIME(3)` 毫秒时间戳，见原则 P7
- 枚举统一存 `VARCHAR`（原则 P2），列宽由 `enum_col()` 按枚举定义推导
- 金额用 `DECIMAL(18,2)`，**绝不用浮点**（见 docs/data-model.md §9.1）
- 时间列的默认值/更新语义集中在 `created_at_col` / `updated_at_col`
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

# ==================== 基础类型 ====================

#: 主键/外键类型：MySQL BIGINT UNSIGNED，无符号避免出现负值 id
PK_TYPE = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")

#: 无符号整数（非自增）：耗时、计数、字符偏移等，负值无意义
INT_UNSIGNED = Integer().with_variant(mysql.INTEGER(unsigned=True), "mysql")

#: 无符号大整数：文件字节数等可能超过 4GB 的量
BIGINT_UNSIGNED = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")

#: 毫秒精度时间戳。时区由容器参数 `--default-time-zone=+08:00` 统一保证
# 泛型 DateTime 本身没有 fsp 参数；MySQL 侧通过 variant 换成 DATETIME(3)
DATETIME_MS = DateTime().with_variant(mysql.DATETIME(fsp=3), "mysql")

#: 金额类型：18 位整数 + 2 位小数，精确十进制
MONEY = Numeric(18, 2)

#: 布尔：MySQL TINYINT(1)
BOOL_TINYINT = SmallInteger().with_variant(mysql.TINYINT(display_width=1), "mysql")

#: 大文本：条款正文、回写 payload（上限 16MB）
MEDIUM_TEXT = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")

#: JSON 列：MySQL 原生 JSON；SQLite 下降级为 TEXT（仅测试用，见 §9.1）
JSON_TYPE = mysql.JSON().with_variant(Text(), "sqlite")

E = TypeVar("E", bound=StrEnum)


# ==================== 列工厂 ====================

def pk_col() -> Mapped[Any]:
    """自增主键列。"""
    return mapped_column(PK_TYPE, primary_key=True, autoincrement=True)


def fk_col(
    target: str,
    *,
    nullable: bool = False,
    ondelete: str = "CASCADE",
    comment: str | None = None,
) -> Mapped[Any]:
    """外键列。

    默认 `ondelete='CASCADE'`：子实体随合同级联硬删（原则 P6，软删除仅用于 `contract`）。
    需要保护被引用行时显式传 `ondelete='RESTRICT'`。
    """
    return mapped_column(
        PK_TYPE,
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        comment=comment,
    )


def enum_col(
    enum_cls: type[E],
    *,
    nullable: bool = False,
    default: E | None = None,
    comment: str | None = None,
    length: int | None = None,
) -> Mapped[Any]:
    """枚举列，存字符串而非 MySQL ENUM（原则 P2）。

    列宽取 `ENUM_WIDTH` 中的固定值（按文档规定，留扩展余量），
    而非"当前枚举最长值"——后者会让新增枚举值触发 DDL 迁移。
    """
    from app.models.enums import ENUM_WIDTH

    width = length or ENUM_WIDTH.get(enum_cls.__name__)
    if width is None:
        raise ValueError(
            f"枚举 {enum_cls.__name__} 未在 ENUM_WIDTH 中登记列宽。"
            "请在 app/models/enums.py 的 ENUM_WIDTH 中补上，不要依赖自动推导。"
        )
    # 同时设 Python 侧与数据库侧默认值：只设前者时，绕过 ORM 直插
    # （脚本、种子数据、手工 SQL）会撞 NOT NULL 报错，已实测。
    return mapped_column(
        String(width),
        nullable=nullable,
        default=default.value if default is not None else None,
        server_default=text(f"'{default.value}'") if default is not None else None,
        comment=comment,
    )


def bool_col(
    *, default: int = 0, nullable: bool = False, comment: str | None = None
) -> Mapped[Any]:
    """布尔列。Python 侧与数据库侧都设默认值，避免直写 SQL 时缺省不一致。"""
    return mapped_column(
        BOOL_TINYINT,
        nullable=nullable,
        default=default,
        server_default=text(str(default)),
        comment=comment,
    )


def uint_col(
    *,
    default: int | None = None,
    nullable: bool = False,
    comment: str | None = None,
) -> Mapped[Any]:
    """无符号整数列。"""
    return mapped_column(
        INT_UNSIGNED,
        nullable=nullable,
        default=default,
        comment=comment,
    )


def money_col(*, nullable: bool = True, comment: str | None = None) -> Mapped[Any]:
    """金额列。用 DECIMAL 保证精确比较（规则引擎的阈值判断依赖它）。"""
    return mapped_column(MONEY, nullable=nullable, comment=comment)


def created_at_col() -> Mapped[Any]:
    """创建时间：写入后不再变动。

    默认值写 `CURRENT_TIMESTAMP(3)` 而非 `NOW(3)`：MySQL 会把 `NOW(3)`
    规范化成 `CURRENT_TIMESTAMP(3)` 再回读，两者写法不一致会让
    `alembic check` 永远报"检测到默认值变更"（已实测）。
    统一用规范化后的形式，保证迁移无漂移。
    """
    return mapped_column(
        DATETIME_MS, nullable=False, server_default=text("CURRENT_TIMESTAMP(3)")
    )


def updated_at_col() -> Mapped[Any]:
    """更新时间：数据库侧自动维护（ON UPDATE），ORM 侧不重复赋值。

    实现说明：SQLAlchemy 的 `server_onupdate` 只用于 ORM 读取时刷新，
    **不会渲染进 DDL**（已实测）。要真正生成
    `ON UPDATE CURRENT_TIMESTAMP(3)`，必须把整个子句写进 `server_default`。
    这样即使有人绕过 ORM 直接执行 UPDATE，时间戳也会被数据库刷新。
    """
    return mapped_column(
        DATETIME_MS,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
    )


def deleted_key_col() -> Mapped[Any]:
    """软删除唯一键用生成列。

    把 `deleted_at` 的 NULL 归一化为哨兵值，否则可空列进唯一键时
    NULL 之间互不冲突，去重将完全失效（实测结论，见 docs/data-model.md §5.1）。
    生成列**不可写入**，由数据库计算。
    """
    return mapped_column(
        DATETIME_MS,
        Computed("IFNULL(deleted_at, '1970-01-01 00:00:00.000')", persisted=True),
        comment="生成列：NULL 归一化哨兵，仅供唯一键使用，不可写入",
    )


def idx(name: str, *columns: Any) -> Index:
    """显式命名索引，名称与 docs/data-model.md §11 保持一致，便于对照核查。"""
    return Index(name, *columns)
