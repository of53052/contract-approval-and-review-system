"""条款切分与元数据提取。

设计依据：docs/architecture.md §12（模块职责）、docs/data-model.md §5.4/§5.5。

**为什么关键词匹配放应用层而不是数据库**（§11 索引汇总的结论）：
MySQL 的 FULLTEXT 默认按空格分词，对中文等于不可用（除非上 ngram parser）。
因此切分与匹配都在应用层做，数据库只负责存取。

切分策略：按块（`ParsedBlock`）扫描，识别条款编号（第X条 / 3.2 / 一、）作为
条款起点；条款类型由标题与正文关键词判定。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.models.enums import ClauseType, MetadataKey
from app.services.parsing.types import BlockKind, DocumentText, ParsedBlock

logger = logging.getLogger(__name__)

#: 条款编号模式。覆盖三类常见写法：
#:   第X条 / 第X章 / 第X节（中文数字）
#:   3.2 / 3.2.1（阿拉伯数字层级）
#:   一、二、（中文数字 + 顿号）
_CLAUSE_NO_PATTERNS = (
    re.compile(r"^\s*(第[一二三四五六七八九十百零〇\d]+[条章节款项])"),
    re.compile(r"^\s*(\d+(?:\.\d+){0,3})[\s、.．]"),
    re.compile(r"^\s*([一二三四五六七八九十]+)[、.．]"),
)

#: 条款类型判定规则：标题/正文命中关键词即归为该类型。
#: 顺序即优先级——更具体的类型在前，避免被"其他"抢先。
CLAUSE_TYPE_KEYWORDS: tuple[tuple[ClauseType, tuple[str, ...]], ...] = (
    (ClauseType.LIABILITY, ("违约责任", "赔偿责任", "违约金", "损失赔偿")),
    (ClauseType.INTELLECTUAL_PROPERTY, ("知识产权", "著作权", "专利", "商标", "成果归属")),
    (ClauseType.JURISDICTION, ("争议解决", "管辖", "仲裁", "诉讼")),
    (ClauseType.CONFIDENTIALITY, ("保密", "商业秘密", "保密义务")),
    (ClauseType.DATA_SECURITY, ("数据安全", "个人信息", "数据保护")),
    (ClauseType.ACCEPTANCE, ("验收", "检验", "质检")),
    # 金额条款归入"金额支付"维度；"合同金额/合同价款"这类标题也要命中
    (ClauseType.PAYMENT, ("付款", "支付", "价款", "结算", "费用",
                          "合同金额", "总金额", "合同价格", "合同价款")),
    (ClauseType.FORCE_MAJEURE, ("不可抗力",)),
    (ClauseType.SUBJECT_MATTER, ("标的", "货物", "产品", "服务内容", "设备")),
)


@dataclass
class ClauseDraft:
    """切分出的条款（未落库）。"""

    clause_type: ClauseType
    clause_no: str | None
    title: str | None
    content: str
    page_no: int
    page_end: int
    para_index: int
    char_start: int | None
    char_end: int | None
    bbox: tuple[float, float, float, float] | None
    seq: int
    source: str


@dataclass
class MetadataDraft:
    """提取出的元数据项（未落库）。"""

    meta_key: MetadataKey
    meta_value: str
    value_normalized: str | None
    value_type: str
    confidence: float = 1.0
    need_review: bool = False


@dataclass
class SplitResult:
    clauses: list[ClauseDraft] = field(default_factory=list)
    metadata: list[MetadataDraft] = field(default_factory=list)


# ==================== 条款切分 ====================

def split_clauses(doc: DocumentText) -> list[ClauseDraft]:
    """把文档切分成条款。

    算法：遍历块，遇到"像条款起点"的块就开一个新条款；
    后续块归入当前条款，直到下一个起点。首个起点之前的内容
    归为前言条款（`ClauseType.OTHER`）。
    """
    drafts: list[ClauseDraft] = []
    buffer: list[ParsedBlock] = []
    current_no: str | None = None
    seq = 0

    def flush() -> None:
        nonlocal buffer, current_no, seq
        if not buffer:
            return
        first, last = buffer[0], buffer[-1]
        content = "\n".join(b.text for b in buffer)
        ctype = _classify(content, current_no)
        title = _extract_title(buffer[0]) if current_no else None
        drafts.append(
            ClauseDraft(
                clause_type=ctype,
                clause_no=current_no,
                title=title,
                content=content,
                page_no=first.page_no,
                page_end=last.page_end,
                para_index=first.para_index,
                char_start=first.char_start,
                char_end=last.char_end,
                bbox=first.bbox,
                seq=seq,
                source="native_text",
            )
        )
        seq += 1
        buffer = []

    for blk in doc.blocks:
        # 表格不参与条款切分：表格里的"违约责任"等字样是列名，不是条款
        if blk.kind == BlockKind.TABLE:
            continue
        no = _match_clause_no(blk.text)
        if no is not None:
            flush()
            current_no = no
        buffer.append(blk)
    flush()

    logger.info("条款切分完成: %s 条", len(drafts))
    return drafts


def _match_clause_no(text: str) -> str | None:
    """识别条款编号。"""
    for pat in _CLAUSE_NO_PATTERNS:
        m = pat.match(text)
        if m:
            return m.group(1)
    return None


def _extract_title(blk: ParsedBlock) -> str | None:
    """从条款起始块提取标题（去掉编号后的部分）。"""
    no = _match_clause_no(blk.text)
    if not no:
        return None
    rest = blk.text[len(no):].strip(" \t、.．:：")
    return rest[:255] or None


def _classify(content: str, clause_no: str | None) -> ClauseType:
    """按关键词判定条款类型。"""
    for ctype, keywords in CLAUSE_TYPE_KEYWORDS:
        if any(kw in content for kw in keywords):
            return ctype
    return ClauseType.OTHER


# ==================== 元数据提取 ====================

#: 元数据提取模式：(键, 正则, 归一化函数)
#: 只覆盖阶段一演示所需的关键字段，见 docs/data-model.md §3.5。
_METADATA_PATTERNS: tuple[tuple[MetadataKey, re.Pattern[str]], ...] = (
    (MetadataKey.PARTY_A_NAME, re.compile(r"甲方[（(]?[^）)]*[）)]?\s*[:：]\s*([^\n，,。；;]{2,60})")),
    (MetadataKey.PARTY_B_NAME, re.compile(r"乙方[（(]?[^）)]*[）)]?\s*[:：]\s*([^\n，,。；;]{2,60})")),
    (MetadataKey.CONTRACT_NO, re.compile(r"合同编号\s*[:：]\s*([A-Za-z0-9\-_/]{3,64})")),
    (MetadataKey.SIGN_DATE, re.compile(r"(?:签订|签署)日期\s*[:：]?\s*(\d{4}\s*[-年/]\s*\d{1,2}\s*[-月/]\s*\d{1,2}\s*日?)")),
    (MetadataKey.SIGN_PLACE, re.compile(r"(?:签订|签署)地点\s*[:：]\s*([^\n，,。；;]{2,60})")),
)

#: 金额模式：支持千分位与币种前缀。
#:
#: ⚠️ 括号说明部分必须用**有界**量词 `{0,20}`，不能用 `[^）)]*`：
#: 后者会贪婪匹配到行尾，回溯时只让出最后一位，导致
#: 「合同总金额：人民币 2,299,000.00 元」被捕获成 "0"（实测缺陷）。
_AMOUNT_RE = re.compile(
    r"(?:合同(?:总)?(?:金额|价款|价格)|总金额|价款)"
    r"(?:[（(][^）)]{0,20}[）)])?"
    r"\s*[:：为]?\s*"
    r"(?:人民币|RMB|￥|¥)?\s*"
    r"([\d,]+(?:\.\d{1,2})?)"
)

#: 统一社会信用代码：18 位
_CREDIT_CODE_RE = re.compile(r"([0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10})")

#: 币种
_CURRENCY_RE = re.compile(r"(人民币|美元|欧元|港币|日元|CNY|USD|EUR|HKD|JPY)")

#: 履行期限
_TERM_RE = re.compile(r"(?:履行|合同)期限\s*[:：]?\s*([^\n，,。；;]{2,60})")


def extract_metadata(doc: DocumentText) -> list[MetadataDraft]:
    """从全文提取元数据。

    只提取"能在原文找到"的字段；找不到的字段**不写库**，
    由全局校验器（`global_checker`）按"必需字段缺失"报风险。
    """
    text = doc.full_text
    out: list[MetadataDraft] = []
    seen: set[str] = set()

    def add(key: MetadataKey, raw: str, value_type: str,
            normalized: str | None = None) -> None:
        if key.value in seen:
            return
        cleaned = raw.strip()
        if not cleaned:
            return
        seen.add(key.value)
        out.append(MetadataDraft(
            meta_key=key,
            meta_value=cleaned[:1024],
            value_normalized=(normalized or cleaned)[:512],
            value_type=value_type,
        ))

    for key, pat in _METADATA_PATTERNS:
        m = pat.search(text)
        if m:
            raw = m.group(1)
            if key == MetadataKey.SIGN_DATE:
                add(key, raw, "date", _normalize_date(raw))
            else:
                add(key, raw, "string")

    # 金额：单独处理，需要归一化为数字串
    m = _AMOUNT_RE.search(text)
    if m:
        amount = _normalize_amount(m.group(1))
        if amount is not None:
            add(MetadataKey.AMOUNT, m.group(1), "decimal", str(amount))

    # 信用代码：取前两个分别作为甲乙方
    codes = _CREDIT_CODE_RE.findall(text)
    if codes:
        add(MetadataKey.PARTY_A_CREDIT_CODE, codes[0], "string")
        if len(codes) > 1:
            add(MetadataKey.PARTY_B_CREDIT_CODE, codes[1], "string")

    m = _CURRENCY_RE.search(text)
    if m:
        add(MetadataKey.CURRENCY, m.group(1), "string", _normalize_currency(m.group(1)))

    m = _TERM_RE.search(text)
    if m:
        add(MetadataKey.TERM, m.group(1), "string")

    logger.info("元数据提取完成: %s 项", len(out))
    return out


def _normalize_amount(raw: str) -> Decimal | None:
    """把金额字符串归一化为 Decimal。失败返回 None（不写库）。"""
    cleaned = raw.replace(",", "").strip()
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if value < 0:
        # 负金额拒绝写入（docs/data-model.md §5.1 不变量）
        return None
    return value.quantize(Decimal("0.01"))


def _normalize_date(raw: str) -> str:
    """把中文日期归一化为 ISO 格式。"""
    nums = re.findall(r"\d+", raw)
    if len(nums) >= 3:
        y, mo, d = nums[0], nums[1], nums[2]
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return raw.strip()


_CURRENCY_MAP = {
    "人民币": "CNY", "美元": "USD", "欧元": "EUR",
    "港币": "HKD", "日元": "JPY",
}


def _normalize_currency(raw: str) -> str:
    return _CURRENCY_MAP.get(raw, raw.upper())


def split_document(doc: DocumentText) -> SplitResult:
    """切分条款并提取元数据（一次调用完成两件事）。"""
    return SplitResult(
        clauses=split_clauses(doc),
        metadata=extract_metadata(doc),
    )
