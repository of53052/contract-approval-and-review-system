"""生成示例合同 DOCX（采购 / 销售 / 服务 / 劳动）。

**为什么要一个生成脚本**：`samples/` 下的 `.docx` 是二进制产物，直接提交无法
审阅其内容与改法。本脚本把三份示例合同的**正文**固化在代码里（可 diff、可评审），
一键重建产物，也便于将来新增场景。

**为什么采购样本也由本脚本生成**：它原先是在 Word 里手工创建的，内容只存在于
二进制里。本批次把它一并纳入脚本——否则"采购内容"与"销售/劳动内容"一个可审阅
一个不可审阅，且扫描件样本是从采购 DOCX 派生的，源头不可重建。

⚠️ 重新生成采购样本（`--force`）后，`samples/scanned/` 的扫描件必须一并重新生成
   （`scripts/make_scanned_sample.py`），两者内容否则会脱节；且扫描件断言里按
   OCR 实测结果书写的 `anchor_quote` 可能需同步调整。

**默认不覆盖正文一致的产物**：DOCX 是二进制，用 python-docx 重写会让字节
（进而 `file_hash`）改变，即使正文一模一样。采购样本是既有基线，无谓的字节
变动会牵动扫描件与演示数据，因此只在"缺失或不一致"时才写。

用法：
    backend\\.venv\\Scripts\\python.exe scripts\\make_sample_docx.py
    backend\\.venv\\Scripts\\python.exe scripts\\make_sample_docx.py --check
    backend\\.venv\\Scripts\\python.exe scripts\\make_sample_docx.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = PROJECT_ROOT / "samples"

#: 公司名与统一社会信用代码一律虚构（architecture.md §15.4）。
#: 每份样本的第一行是文档标题（Title 样式），其余为正文段落。
SAMPLES: dict[str, dict] = {
    "purchase": {
        "file": "设备采购合同-高风险样本.docx",
        "title": "设备采购合同",
        "body": [
            "合同编号：CG-2026-0912",
            "甲方（采购方）：某某科技有限公司",
            "乙方（供应商）：某某设备制造有限公司",
            "统一社会信用代码：91110108MA01ABCD2X",
            "统一社会信用代码：91320594MA1EFGH3Y7",
            "第一条 标的物",
            "乙方向甲方提供自动输送线 CL-X120，数量 1 套。",
            "第二条 合同金额",
            "合同总金额：人民币 2,299,000.00 元。",
            "第三条 付款方式",
            "货物到货即付全款，甲方应在到货后 3 日内支付全部款项。",
            "第四条 验收标准",
            "甲方应在收到货物后 10 个工作日内完成验收。",
            "第五条 违约责任",
            "乙方逾期交货的，应承担甲方全部损失，赔偿责任无上限。",
            "第六条 保密义务",
            "双方对因履行本合同知悉的商业秘密负有保密义务。",
            "第七条 争议解决",
            "因本合同产生的争议，提交香港仲裁。",
            "第八条 知识产权",
            "本项目产生的知识产权归供应商所有。",
            "第九条 不可抗力",
            "因不可抗力不能履行的，受影响方应及时通知对方。",
            "合同期限：自 2026 年 10 月 1 日起至 2027 年 9 月 30 日止。",
        ],
    },
    "sales": {
        "file": "产品销售合同-高风险样本.docx",
        "title": "产品销售合同",
        "body": [
            "合同编号：XS-2026-0077",
            "甲方（供方）：某某科技有限公司",
            "乙方（客户）：某某商贸有限公司",
            "统一社会信用代码：91110108MA01ABCD2X",
            "统一社会信用代码：91330100MA2KLMN4P8",
            "第一条 产品与数量",
            "甲方向乙方供应智能终端设备 MX-200，数量 500 台。",
            "第二条 合同金额",
            "合同总金额：人民币 5,000,000.00 元。",
            "第三条 付款方式",
            "乙方应在收到货物后 30 日内支付全部款项。",
            "第四条 验收标准",
            "乙方应在收到货物后 10 个工作日内完成验收。",
            # PRD 2.4.9 场景二的核心事实：两方责任严重不对等
            "第五条 违约责任",
            "乙方逾期付款的，每日按应付未付金额的万分之一支付滞纳金；"
            "甲方逾期交货的，应承担乙方全部损失，赔偿责任无上限。",
            "第六条 保密义务",
            "双方对因履行本合同知悉的商业秘密负有保密义务。",
            "第七条 争议解决",
            "因本合同产生的争议，提交香港仲裁。",
            "第八条 不可抗力",
            "因不可抗力不能履行的，受影响方应及时通知对方。",
            "合同期限：自 2026 年 10 月 1 日起至 2027 年 9 月 30 日止。",
        ],
    },
    "service": {
        "file": "技术服务合同-高风险样本.docx",
        "title": "技术服务合同",
        "body": [
            "合同编号：FW-2026-0031",
            "甲方（委托方）：某某科技有限公司",
            "乙方（服务方）：某某软件服务有限公司",
            "统一社会信用代码：91110108MA01ABCD2X",
            "统一社会信用代码：91330100MA2PQRST5W",
            "第一条 服务内容",
            "乙方向甲方提供企业管理系统定制开发与运维服务。",
            "第二条 合同金额",
            "合同总金额：人民币 1,800,000.00 元。",
            "第三条 付款方式",
            "合同签订后 5 日内，甲方应一次性支付全部服务费用。",
            # 服务/外包场景的核心风险：成果归属对方 + 付款不与验收挂钩
            "第四条 服务成果与知识产权",
            "乙方提供的服务成果及交付物，其所有权归乙方。",
            "第五条 验收标准",
            "甲方应在乙方交付后 10 个工作日内完成验收。",
            # 责任不对等：委托方仅万分之一滞纳金，服务方无上限赔偿
            "第六条 违约责任",
            "甲方逾期付款的，每日按应付未付金额的万分之一支付滞纳金；"
            "乙方逾期交付的，应承担甲方全部损失，赔偿责任无上限。",
            "第七条 保密义务",
            "双方对因履行本合同知悉的商业秘密负有保密义务。",
            "第八条 争议解决",
            "因本合同产生的争议，提交香港仲裁。",
            "第九条 不可抗力",
            "因不可抗力不能履行的，受影响方应及时通知对方。",
            "合同期限：自 2026 年 10 月 1 日起至 2027 年 9 月 30 日止。",
        ],
    },
    "labor": {
        "file": "劳动合同-高风险样本.docx",
        "title": "劳动合同",
        "body": [
            "合同编号：LD-2026-0001",
            "甲方（用人单位）：某某科技有限公司",
            "乙方（劳动者）：李四",
            "第一条 合同期限",
            "本合同为固定期限劳动合同，期限自 2026 年 10 月 1 日起至 2027 年 9 月 30 日止。",
            "第二条 试用期",
            "试用期为 3 个月，试用期工资为劳动合同约定工资的 50%。",
            "第三条 工作内容与劳动报酬",
            "乙方担任软件工程师，甲方于每月 10 日前支付乙方上月工资，月工资为人民币 12000 元。",
            "第四条 社会保险",
            "乙方自愿放弃甲方为其缴纳社会保险，甲方每月另行支付社保补贴 800 元。",
            "第五条 工作时间",
            "乙方每周工作六天，甲方不另行支付加班费。",
            "第六条 保密与竞业限制",
            "乙方离职后 2 年内不得从事同业竞争业务。",
            "第七条 违约责任",
            "乙方提前离职的，应向甲方支付违约金人民币 50000 元。",
        ],
    },
}


def _write_sample(spec: dict, out_dir: Path) -> Path:
    """按规格生成一份 DOCX。

    中文字体显式设为宋体：不设字体时 WPS/LibreOffice 可能回退到不含中文字形的
    字体，转换出的 PDF 会变成方块，OCR 也随之失效（示例必须代表正常输入）。
    """
    import docx
    from docx.oxml.ns import qn

    doc = docx.Document()
    # 正文默认字体（东亚字形需单独设 w:eastAsia，python-docx 不代管）
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = docx.shared.Pt(12)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    doc.add_paragraph(spec["title"], style="Title")
    for line in spec["body"]:
        doc.add_paragraph(line)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / spec["file"]
    doc.save(path)
    return path


def _text_of(path: Path) -> list[str]:
    """提取 DOCX 段落文本，用于 --check 比对。"""
    import docx
    return [p.text for p in docx.Document(str(path)).paragraphs]


def main() -> int:
    ap = argparse.ArgumentParser(description="生成示例合同 DOCX")
    ap.add_argument("--check", action="store_true",
                    help="只校验现有产物的正文是否与脚本内定义一致")
    ap.add_argument("--force", action="store_true",
                    help="正文一致时也重写产物（会改变 file_hash，采购样本慎用）")
    ap.add_argument("--only", default=None,
                    help="只处理该业务类型（purchase/sales/service/labor）")
    args = ap.parse_args()

    targets = {k: v for k, v in SAMPLES.items() if args.only in (None, k)}
    if not targets:
        print(f"✗ 未知的业务类型: {args.only}")
        return 1

    rc = 0
    for bt, spec in targets.items():
        path = SAMPLES_DIR / bt / spec["file"]
        want = [spec["title"]] + spec["body"]

        if args.check:
            if not path.exists():
                print(f"✗ {bt}: 产物不存在（{path.relative_to(PROJECT_ROOT)}）")
                rc = 1
                continue
            got = _text_of(path)
            if got == want:
                print(f"✓ {bt}: 正文与脚本定义一致（{len(want)} 段）")
            else:
                print(f"✗ {bt}: 正文与脚本定义不一致")
                for i, (a, b) in enumerate(zip(got, want)):
                    if a != b:
                        print(f"    第 {i} 段 产物={a!r} 定义={b!r}")
                rc = 1
            continue

        # 已存在且正文一致 → 不重写：字节变动会牵动 file_hash 与下游派生产物
        if path.exists() and not args.force and _text_of(path) == want:
            print(f"= {bt}: 跳过（正文已一致，{path.relative_to(PROJECT_ROOT)}）")
            continue

        p = _write_sample(spec, SAMPLES_DIR / bt)
        print(f"✓ 已生成 {p.relative_to(PROJECT_ROOT)}"
              f"（{p.stat().st_size:,} 字节，{len(want)} 段）")
        rebuilt = True

    if not args.check:
        print("\n⚠️ 采购样本若被重建（--force），扫描件必须一并重建："
              "python scripts\\make_scanned_sample.py")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
