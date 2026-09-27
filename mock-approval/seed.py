"""为 mock 审批服务写入演示数据。

用法（工作目录 mock-approval/）：
    python seed.py

把 `samples/` 下的示例合同复制到 `data/attachments/`，并生成 `data/todos.json`。

**幂等**：重复执行只覆盖待办清单与附件，**不清空已写入的评论**
（评论是回写演示的产物，清掉会让"回写成功"的验证失效）。
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    # 控制台默认 GBK，中文与 ✓ 会 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).resolve().parent / "data"
ATTACHMENT_DIR = DATA_DIR / "attachments"
TODOS_FILE = DATA_DIR / "todos.json"
SAMPLES_DIR = PROJECT_ROOT / "samples"

#: 演示待办清单。字段与 app/schemas.py 的 TodoItem 对应。
#: ⚠️ 公司名一律虚构（architecture.md §15.4）。
TODOS: list[dict] = [
    {
        "approval_no": "AP-2026-0001",
        "title": "设备采购合同（高风险样本）",
        "applicant": "张三",
        "applicant_dept": "供应链管理部",
        "business_type": "purchase",
        "counterparty": "某某设备制造有限公司",
        "status": "pending",
        "attachments": [
            {
                "attachment_id": "att1",
                "file_name": "设备采购合同-高风险样本.docx",
                "sample_path": "purchase/设备采购合同-高风险样本.docx",
            },
        ],
    },
]


def _content_type(suffix: str) -> str:
    return {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix.lower(), "application/octet-stream")


def main() -> int:
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    todos_out: list[dict] = []
    now = datetime.now()

    for idx, spec in enumerate(TODOS):
        attachments_out = []
        for att in spec["attachments"]:
            src = SAMPLES_DIR / att["sample_path"]
            if not src.exists():
                print(f"  ✗ 示例合同不存在，跳过: {src}", file=sys.stderr)
                continue
            suffix = src.suffix
            dst = ATTACHMENT_DIR / f"{spec['approval_no']}_{att['attachment_id']}{suffix}"
            shutil.copyfile(src, dst)
            attachments_out.append({
                "attachment_id": att["attachment_id"],
                "file_name": att["file_name"],
                "file_size": dst.stat().st_size,
                "content_type": _content_type(suffix),
                "download_url": (
                    f"/api/todos/{spec['approval_no']}"
                    f"/attachments/{att['attachment_id']}"
                ),
            })
            print(f"  ✓ 附件 {dst.name}（{dst.stat().st_size:,} 字节）")

        if not attachments_out:
            print(f"  ! 待办 {spec['approval_no']} 无可用附件，跳过", file=sys.stderr)
            continue

        todos_out.append({
            "approval_no": spec["approval_no"],
            "title": spec["title"],
            "applicant": spec["applicant"],
            "applicant_dept": spec["applicant_dept"],
            "business_type": spec["business_type"],
            "counterparty": spec["counterparty"],
            "status": spec["status"],
            "submitted_at": (now - timedelta(hours=idx * 3)).isoformat(),
            "attachments": attachments_out,
        })

    TODOS_FILE.write_text(
        json.dumps(todos_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n待办清单写入: {TODOS_FILE}（{len(todos_out)} 条）")
    if not (DATA_DIR / "comments.json").exists():
        (DATA_DIR / "comments.json").write_text("[]", encoding="utf-8")
        print("评论文件已初始化（空）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
