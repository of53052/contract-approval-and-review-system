"""通用响应模型。"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页响应。"""

    items: list[T] = Field(description="当前页数据")
    total: int = Field(description="总条数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(description="每页条数")


class ErrorOut(BaseModel):
    """错误响应体。"""

    detail: str = Field(description="错误说明")


class OkOut(BaseModel):
    """简单成功响应。"""

    ok: bool = True
    detail: str | None = None
