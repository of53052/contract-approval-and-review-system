"""mock 审批服务的数据存储。

用 JSON 文件而非数据库：本服务只是**扮演外部系统的桩**，
引入 DB 会让"启动演示"多一个失败点（architecture.md §13.1 的同款权衡）。
数据量是个位数的审批单，JSON 足够。

**线程安全**：FastAPI 默认在线程池里跑同步端点，写文件用锁保护。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from app.schemas import Attachment, Comment, TodoItem

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
ATTACHMENT_DIR = DATA_DIR / "attachments"
TODOS_FILE = DATA_DIR / "todos.json"
COMMENTS_FILE = DATA_DIR / "comments.json"

_lock = threading.Lock()


class Store:
    """待办与评论的存储。

    启动时从 JSON 载入，变更后写回。**不做缓存层**——
    文件很小，每次读盘的开销远小于调试时"改了文件不生效"的困惑。
    """

    def __init__(self) -> None:
        self._todos: dict[str, TodoItem] = {}
        self._comments: list[Comment] = []
        self._comment_seq = 0
        self.load()

    # ---------------- 载入与落盘 ----------------

    def load(self) -> None:
        """从 JSON 载入待办与评论。文件不存在时视为空。"""
        if TODOS_FILE.exists():
            raw = json.loads(TODOS_FILE.read_text(encoding="utf-8"))
            self._todos = {item["approval_no"]: TodoItem(**item) for item in raw}
            logger.info("载入 %s 个待办审批单", len(self._todos))
        else:
            logger.warning("待办数据文件不存在: %s", TODOS_FILE)

        if COMMENTS_FILE.exists():
            raw = json.loads(COMMENTS_FILE.read_text(encoding="utf-8"))
            self._comments = [Comment(**c) for c in raw]
            # 评论 ID 是 c{序号}，取最大值续号
            seqs = [
                int(c.comment_id[1:]) for c in self._comments
                if c.comment_id.startswith("c") and c.comment_id[1:].isdigit()
            ]
            self._comment_seq = max(seqs, default=0)
            logger.info("载入 %s 条评论", len(self._comments))

    def _save_comments(self) -> None:
        """写回评论文件。调用方必须已持锁。"""
        payload = [c.model_dump(mode="json") for c in self._comments]
        COMMENTS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------------- 待办 ----------------

    def list_todos(self, status: str | None = None) -> list[TodoItem]:
        """列出待办审批单，可按状态过滤。"""
        items = list(self._todos.values())
        if status:
            items = [i for i in items if i.status == status]
        return sorted(items, key=lambda i: i.submitted_at, reverse=True)

    def get_todo(self, approval_no: str) -> TodoItem | None:
        return self._todos.get(approval_no)

    def set_status(self, approval_no: str, status: str) -> TodoItem | None:
        """更新审批单状态（模拟审批人操作）。"""
        with _lock:
            todo = self._todos.get(approval_no)
            if todo is None:
                return None
            todo.status = status  # type: ignore[assignment]
            return todo

    # ---------------- 附件 ----------------

    def attachment_path(self, approval_no: str, attachment_id: str) -> Path | None:
        """解析附件的物理路径。

        命名约定：`{approval_no}_{attachment_id}{后缀}`，
        后缀从待办里的附件记录取，保证与 `file_name` 一致。
        """
        todo = self._todos.get(approval_no)
        if todo is None:
            return None
        att = next((a for a in todo.attachments if a.attachment_id == attachment_id), None)
        if att is None:
            return None
        suffix = Path(att.file_name).suffix
        path = ATTACHMENT_DIR / f"{approval_no}_{attachment_id}{suffix}"
        return path if path.exists() else None

    # ---------------- 评论 ----------------

    def add_comment(
        self, approval_no: str, author: str, content: str,
        idempotency_key: str | None = None,
    ) -> tuple[Comment, bool]:
        """写入评论。返回 (评论, 是否命中幂等)。

        **幂等**：`idempotency_key` 非空且已存在时，直接返回已有记录，
        不新建。这是回写幂等的服务端兜底——后端自己也查 `writeback_log`，
        两层都有才能防住"重试请求已到达但响应丢失"的场景。
        """
        with _lock:
            if idempotency_key:
                existing = next(
                    (c for c in self._comments
                     if c.approval_no == approval_no
                     and c.idempotency_key == idempotency_key),
                    None,
                )
                if existing is not None:
                    logger.info("评论幂等命中: %s / %s", approval_no, idempotency_key)
                    return existing, True

            self._comment_seq += 1
            comment = Comment(
                comment_id=f"c{self._comment_seq}",
                approval_no=approval_no,
                author=author,
                content=content,
                idempotency_key=idempotency_key,
                created_at=datetime.now(),
            )
            self._comments.append(comment)
            self._save_comments()
            logger.info("评论已写入: %s -> %s", comment.comment_id, approval_no)
            return comment, False

    def list_comments(self, approval_no: str) -> list[Comment]:
        return [c for c in self._comments if c.approval_no == approval_no]


#: 进程内单例。FastAPI 启动时载入一次。
store = Store()
