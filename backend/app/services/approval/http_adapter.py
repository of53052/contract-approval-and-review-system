"""基于 HTTP 的审批系统适配器。

对接 mock 审批服务（`mock-approval/`），也适用于任何实现了
同样四个接口的真实审批系统——**只改配置不改代码**。

设计依据：docs/architecture.md §3（M1~M4）、§11.1（审批触发流程）。
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from app.core.config import settings
from app.services.approval.base import (
    ApprovalError,
    ApprovalNotFound,
)
from app.services.approval.types import (
    DownloadedFile,
    RemoteAttachment,
    RemoteTodo,
    WriteCommentResult,
)

logger = logging.getLogger(__name__)

#: 单次请求超时（秒）。下载附件可能较大，给宽一些。
_TIMEOUT_DEFAULT = 10.0
_TIMEOUT_DOWNLOAD = 60.0


class HttpApprovalAdapter:
    """通过 HTTP 调用审批系统。

    复用 `httpx.Client`（连接池），避免每次请求重新握手。
    """

    def __init__(self, base_url: str | None = None, *, timeout: float = _TIMEOUT_DEFAULT) -> None:
        self.base_url = (base_url or settings.mock_approval_base_url).rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    @property
    def name(self) -> str:
        return "http"

    def close(self) -> None:
        self._client.close()

    # ---------------- 内部：统一错误处理 ----------------

    def _request(self, method: str, url: str, **kwargs):
        """发请求并把 httpx 异常翻译成领域异常。

        这样业务层不需要知道 httpx 的存在——它只处理 `ApprovalError`。
        """
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ApprovalError(f"审批系统请求失败: {type(exc).__name__}: {exc}") from exc

        if resp.status_code == 404:
            raise ApprovalNotFound(f"审批系统返回 404: {url}", http_status=404)
        if resp.status_code >= 400:
            raise ApprovalError(
                f"审批系统返回 {resp.status_code}: {resp.text[:200]}",
                http_status=resp.status_code,
            )
        return resp

    # ---------------- M1 待办 ----------------

    def health(self) -> tuple[bool, str]:
        try:
            resp = self._client.get("/health")
            if resp.status_code == 200:
                data = resp.json()
                return True, f"审批系统可达 | todos={data.get('todos')}"
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - 自检需捕获全部异常
            return False, f"{type(exc).__name__}: {exc}"

    def list_todos(self, status: str | None = None) -> list[RemoteTodo]:
        params = {"status": status} if status else None
        resp = self._request("GET", "/api/todos", params=params)
        return [self._to_todo(item) for item in resp.json()]

    def get_todo(self, approval_no: str) -> RemoteTodo:
        resp = self._request("GET", f"/api/todos/{approval_no}")
        return self._to_todo(resp.json())

    @staticmethod
    def _to_todo(item: dict) -> RemoteTodo:
        """外部 JSON → 领域类型。字段映射集中在此处。"""
        return RemoteTodo(
            approval_no=item["approval_no"],
            title=item["title"],
            applicant=item["applicant"],
            applicant_dept=item["applicant_dept"],
            business_type=item["business_type"],
            counterparty=item["counterparty"],
            status=item["status"],
            submitted_at=datetime.fromisoformat(item["submitted_at"]),
            attachments=[
                RemoteAttachment(
                    attachment_id=a["attachment_id"],
                    file_name=a["file_name"],
                    file_size=a["file_size"],
                    content_type=a["content_type"],
                    download_url=a["download_url"],
                )
                for a in item.get("attachments", [])
            ],
        )

    # ---------------- M2 附件 ----------------

    def download_attachment(
        self, approval_no: str, attachment_id: str
    ) -> DownloadedFile:
        url = f"/api/todos/{approval_no}/attachments/{attachment_id}"
        try:
            resp = self._client.get(url, timeout=_TIMEOUT_DOWNLOAD)
        except httpx.HTTPError as exc:
            raise ApprovalError(f"附件下载失败: {type(exc).__name__}: {exc}") from exc

        if resp.status_code == 404:
            raise ApprovalNotFound(f"附件不存在: {approval_no}/{attachment_id}", http_status=404)
        if resp.status_code >= 400:
            raise ApprovalError(
                f"附件下载返回 {resp.status_code}", http_status=resp.status_code
            )

        # 文件名从 Content-Disposition 取；缺失时退回 URL 末段
        disposition = resp.headers.get("content-disposition", "")
        file_name = _filename_from_disposition(disposition) or f"{attachment_id}.bin"
        return DownloadedFile(
            file_name=file_name,
            content=resp.content,
            content_type=resp.headers.get("content-type", "application/octet-stream"),
        )

    # ---------------- M3 评论写入 ----------------

    def write_comment(
        self,
        approval_no: str,
        author: str,
        content: str,
        *,
        idempotency_key: str | None = None,
    ) -> WriteCommentResult:
        payload = {
            "author": author,
            "content": content,
            "idempotency_key": idempotency_key,
        }
        try:
            resp = self._client.post(f"/api/todos/{approval_no}/comments", json=payload)
        except httpx.HTTPError as exc:
            raise ApprovalError(f"评论写入失败: {type(exc).__name__}: {exc}") from exc

        if resp.status_code == 404:
            raise ApprovalNotFound(f"审批单不存在: {approval_no}", http_status=404)
        if resp.status_code >= 400:
            raise ApprovalError(
                f"评论写入返回 {resp.status_code}: {resp.text[:200]}",
                http_status=resp.status_code,
            )

        data = resp.json()
        return WriteCommentResult(
            comment_id=data["comment_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            deduplicated=bool(data.get("deduplicated", False)),
            http_status=resp.status_code,
        )


def _filename_from_disposition(disposition: str) -> str | None:
    """从 Content-Disposition 头解析文件名。

    只处理 `filename=` 与 `filename*=UTF-8''` 两种形式——
    mock 服务用前者；真实系统用后者时也不至于解析失败。
    """
    if not disposition:
        return None
    for part in disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            value = part.split("=", 1)[1].strip()
            # 形如 UTF-8''%E8%AE%BE%E5%A4%87.docx
            if "''" in value:
                _, _, encoded = value.partition("''")
                from urllib.parse import unquote
                return unquote(encoded)
        if part.lower().startswith("filename="):
            value = part.split("=", 1)[1].strip().strip('"')
            if value:
                return value
    return None
