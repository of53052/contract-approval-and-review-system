"""MinIO 对象存储客户端与路径规划。

路径规划见 docs/data-model.md §7.3。
"""

from __future__ import annotations

from minio import Minio
from minio.error import S3Error

from app.core.config import settings

_client = Minio(
    settings.minio_endpoint,
    access_key=settings.minio_access_key,
    secret_key=settings.minio_secret_key,
    secure=settings.minio_secure,
)


def get_minio() -> Minio:
    """获取 MinIO 客户端。"""
    return _client


def ensure_buckets() -> list[str]:
    """确保所需 bucket 存在，返回本次新建的 bucket 名。

    启动时调用一次即可，幂等。
    """
    created: list[str] = []
    for bucket in (settings.minio_bucket_contracts, settings.minio_bucket_reports):
        if not _client.bucket_exists(bucket):
            _client.make_bucket(bucket)
            created.append(bucket)
    return created


# ==================== 对象路径规划 ====================
# 合同原件与转换后 PDF 用 contract_id 分目录，便于按合同整体清理。


def path_original(contract_id: int, ext: str) -> str:
    """合同原件路径。ext 不带点，如 'docx' / 'pdf'。"""
    return f"{contract_id}/original.{ext.lstrip('.')}"


def path_converted_pdf(contract_id: int) -> str:
    """DOCX 转换后的 PDF 路径（统一渲染用）。"""
    return f"{contract_id}/converted.pdf"


def path_report(contract_id: int, timestamp: str, ext: str) -> str:
    """导出报告路径。timestamp 形如 20260927_103000。"""
    return f"{contract_id}/{timestamp}.{ext.lstrip('.')}"


def check_connection() -> tuple[bool, str]:
    """连通性自检。返回 (是否成功, 详情)。"""
    try:
        buckets = [b.name for b in _client.list_buckets()]
        return True, f"MinIO 可达 | buckets={buckets or '无'}"
    except S3Error as exc:
        return False, f"S3Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - 自检需捕获全部异常
        return False, f"{type(exc).__name__}: {exc}"
