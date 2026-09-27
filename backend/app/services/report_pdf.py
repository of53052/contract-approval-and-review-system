"""审查报告 PDF 渲染（精排）。

设计依据：docs/architecture.md §18.2「PDF 报告精排」；PRD 2.4.8（报告结构）。

**为什么用 PyMuPDF 的 Story 而不是引第三方排版库**：
`pymupdf` 已是本项目依赖（解析链路在用），其自带 HTML/CSS 排版引擎
（`Story` + `DocumentWriter`）可直接把 HTML 流式排到多页 A4。
引入 reportlab / weasyprint / pandoc 都要新增依赖，而 `AGENTS.md` 明确
"禁止擅自引入新第三方依赖"；Story 能覆盖当前需求，就不必引。

**为什么先生成 HTML 而不是直接画 PDF**：
分页、避孤行、表格自适应交给 CSS 引擎，比手工算坐标稳；且 HTML 与
Markdown 共用同一份 `ReportBundle` 数据口径，改数据源时两边不会漂移。

**中文字体**：Story 需要真实字体文件，用 `@font-face` 指向系统字体的绝对路径。
Windows 上优先微软雅黑，缺失时逐级回退到黑体/宋体。**不把字体文件放进仓库**——
那会让 diff 里出现几 MB 二进制。
"""

from __future__ import annotations

import gc
import html
import logging
import tempfile
from datetime import datetime
from pathlib import Path

import pymupdf

from app.models.enums import RiskLevel
from app.services.report_service import (
    ReportBundle,
    _CONCLUSION_LABEL,
    _LEVEL_LABEL,
    _meta_value,
    _merged_by_label,
    _parse_method,
)

logger = logging.getLogger(__name__)

#: 候选中文字体（按优先级）。都是 Windows 自带，无需随仓库分发。
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
)

#: 风险等级 → 主题色。与前端 `constants.tsx` 的 RISK_META 同口径。
_LEVEL_COLOR = {
    RiskLevel.HIGH.value: "#cf1322",
    RiskLevel.MEDIUM.value: "#d46b08",
    RiskLevel.LOW.value: "#389e0d",
}
_LEVEL_BORDER = {
    RiskLevel.HIGH.value: "#ffa39e",
    RiskLevel.MEDIUM.value: "#ffd591",
    RiskLevel.LOW.value: "#b7eb8f",
}

#: 页面尺寸（A4）与页边距（PDF point，约 1.5cm）
_PAGE_SIZE = "a4"
_MARGIN = 42.5


def _pick_font() -> Path | None:
    """选一个可用的中文字体文件。全都缺失时返回 None（调用方降级）。"""
    for candidate in _FONT_CANDIDATES:
        p = Path(candidate)
        if p.exists():
            return p
    return None


def _esc(value: object) -> str:
    """HTML 转义。报告数据含合同原文与 LLM 产出，可能出现 `<` `&`。"""
    return html.escape("" if value is None else str(value))


def _font_face_css() -> str:
    """把系统中文字体注册成 CSS 字体族。"""
    font = _pick_font()
    if font is None:
        logger.warning("未找到中文字体，PDF 报告中的中文可能显示为方块")
        return ""
    # Windows 路径的反斜杠在 CSS url() 里会被当转义符，统一转正斜杠
    return f"@font-face {{ font-family: report-cjk; src: url({font.as_posix()}); }}"


#: 报告基础样式。**风险卡片与结论横幅的配色走内联样式**——
#: 它们的颜色随等级变化，写成全局规则会把所有卡片染成同一色。
_CSS = """
__FONT_FACE__
* { font-family: report-cjk, sans-serif; }
body { font-size: 10.5pt; color: #1f1f1f; line-height: 1.6; }
h1 { font-size: 17pt; margin: 0 0 4pt 0; }
h2 { font-size: 13pt; margin: 14pt 0 6pt 0; padding-bottom: 3pt;
     border-bottom: 1px solid #d9d9d9; }
.gen { color: #8c8c8c; font-size: 9pt; margin-bottom: 10pt; }
table.info { width: 100%; border-collapse: collapse; margin-bottom: 6pt;
             table-layout: fixed; }
table.info td { border: 1px solid #e8e8e8; padding: 4pt 6pt; vertical-align: top; }
/* ⚠️ 标签列宽必须用 **pt** 而非百分比：Story 的 CSS 引擎不支持
   百分比列宽，写百分比会让该列被压到最小宽度、中文标签逐字竖排（实测）。
   ⚠️ 也不能给单元格加 `background`：Story 分页时会把带背景的单元格
   在续页重画成一排**空白色块**（实测 10 个单元格 → 末页 10 个空框）。
   标签列改用颜色 + 加粗来区分。 */
table.info td.k { width: 92pt; color: #595959; }
.risk { border-radius: 3pt; padding: 7pt 9pt; margin-bottom: 8pt;
        page-break-inside: avoid; }
/* ⚠️ 标签行不用 <span> 加边框：Story 的 CSS 引擎不支持 inline 元素的
   padding / margin / border / display:inline-block（实测：加了反而每个
   标签各占一行）。改用纯文本 + 全角空格分隔，见 `_tag_line`。 */
.tags { font-size: 9pt; color: #595959; margin-top: 4pt; }
.lbl { color: #8c8c8c; }
/* ⚠️ 同 `.risk`：不用 background。Story 会把带背景的块在续页顶部重画。 */
.quote { border-left: 2pt solid #d9d9d9; padding: 4pt 8pt; margin-top: 3pt; }
.evidence { font-size: 9.5pt; color: #595959; margin-top: 3pt; }
.note { margin-top: 12pt; padding-top: 6pt; border-top: 1px solid #e8e8e8;
        color: #8c8c8c; font-size: 8.5pt; }
/* 附录独立起页。
   ⚠️ Story 的 CSS 引擎**不支持任何 keep-together 属性**（实测
   `page-break-inside: avoid` / `break-inside: avoid` / `white-space: nowrap`
   全都无效），因此无法让"标题 + 列表 + 页脚说明"在空间不足时整体挪到
   下一页——它们会被从中间切开。用 `page-break-before` 让附录整块起新页，
   既避开该限制，也符合"附录独立成页"的文档惯例。 */
.appendix { page-break-before: always; }
""".replace("__FONT_FACE__", _font_face_css())


def _conclusion_label(conclusion: str | None) -> str:
    return _CONCLUSION_LABEL.get(conclusion or "", conclusion or "—")


def _tag_line(items: list[str]) -> str:
    """把若干标签拼成一行文本。

    Story 不支持 inline 元素的盒模型（padding/border/inline-block），
    所以标签不做成"胶囊"样式，而是纯文本 + 全角空格分隔——排版稳定且易读。
    """
    return "　".join(_esc(i) for i in items if i)


def _banner(bundle: ReportBundle) -> str:
    """结论横幅：总风险等级 + 审查结论 + 核心摘要。"""
    t = bundle.task
    level = t.overall_risk or ""
    fg = _LEVEL_COLOR.get(level, "#595959")
    border = _LEVEL_BORDER.get(level, "#e8e8e8")
    label = _LEVEL_LABEL.get(level, level or "—")
    parts = [
        f'<div style="border:1px solid {border};border-left:3pt solid {fg};'
        f'padding:8pt 10pt;margin-bottom:8pt;">',
        f'<span style="font-size:13pt;font-weight:bold;color:{fg};">'
        f'{_esc(label)}</span>',
        f'<span style="margin-left:10pt;font-weight:bold;">'
        f'结论：{_esc(_conclusion_label(t.conclusion))}</span>',
    ]
    if t.summary:
        parts.append(
            f'<div style="margin-top:4pt;color:#595959;font-size:9.5pt;">'
            f'{_esc(t.summary)}</div>'
        )
    parts.append("</div>")
    return "".join(parts)


def _info_table(bundle: ReportBundle) -> str:
    """合同基本信息表。"""
    c = bundle.contract
    amount = (
        f"{c.amount:,.2f} {c.currency or ''}".strip() if c.amount is not None else "—"
    )
    rows = [
        ("合同名称", c.title),
        ("合同编号", c.contract_no or _meta_value(bundle, "contract_no") or "—"),
        ("送审部门", c.applicant_dept or "—"),
        ("申请人", c.applicant or "—"),
        ("相对方", c.counterparty_name or _meta_value(bundle, "party_b_name") or "—"),
        (
            "相对方信用代码",
            c.counterparty_code or _meta_value(bundle, "party_b_credit_code") or "—",
        ),
        ("合同金额", amount),
        ("履约期限", _meta_value(bundle, "term") or "—"),
        ("业务类型", c.business_type),
        ("文件名", c.file_name),
    ]
    body = "".join(
        f'<tr><td class="k">{_esc(k)}</td><td>{_esc(v)}</td></tr>' for k, v in rows
    )
    return f'<table class="info">{body}</table>'


def _anchor_desc(bundle: ReportBundle, risk_id: int, unanchored: int) -> str:
    """原文定位描述。与 Markdown 版同口径（页码 + 级别 + 引用片段）。"""
    anchors = bundle.anchors.get(risk_id, [])
    if unanchored or not anchors:
        return "⚠️ 无法定位到原文，需人工核查"
    level_label = {"exact": "精确", "fuzzy": "模糊", "paragraph": "段落级"}
    parts: list[str] = []
    for a in anchors:
        lv = level_label.get(a.anchor_level, a.anchor_level)
        quote = f"「{a.quote_text}」" if a.quote_text else ""
        parts.append(f"第 {a.page_no} 页（{lv}）{quote}")
    return "；".join(parts)


def _risk_block(bundle: ReportBundle, idx: int, risk) -> str:
    """单张风险卡片。"""
    level = risk.risk_level
    fg = _LEVEL_COLOR.get(level, "#595959")
    border = _LEVEL_BORDER.get(level, "#e8e8e8")
    tags = [
        _LEVEL_LABEL.get(level, level),
        f"分类：{risk.category}",
        f"来源：{_merged_by_label(risk.merged_by)}",
    ]
    clause = bundle.clauses.get(risk.clause_id) if risk.clause_id else None
    if clause is not None:
        clause_text = f"{clause.clause_no or ''} {clause.title or ''}".strip()
        tags.append(f"条款：{clause_text}")

    # ⚠️ 卡片**不能设 background**：Story 分页时会把带背景的块在续页顶部
    # 重画成一排碎片（实测：3 页的报告，p2/p3 顶部各有 5/12 个残块）。
    # 用"左侧竖线 + 细描边 + 彩色标题"表达等级，视觉区分同样清晰。
    out = [
        f'<div class="risk" style="border:1px solid {border};'
        f'border-left:3pt solid {fg};">',
        f'<div style="font-size:11.5pt;font-weight:bold;color:{fg};">'
        f'{idx}. {_esc(risk.title)}</div>',
        f'<div class="tags">{_tag_line(tags)}</div>',
    ]

    out.append(
        f'<div style="margin-top:4pt;"><span class="lbl">原文定位：</span>'
        f'{_esc(_anchor_desc(bundle, risk.id, risk.unanchored))}</div>'
    )
    out.append(
        f'<div style="margin-top:4pt;"><span class="lbl">风险成因：</span>'
        f'{_esc(risk.reason)}</div>'
    )
    if risk.legal_basis:
        out.append(
            f'<div style="margin-top:4pt;"><span class="lbl">法律依据：</span>'
            f'{_esc(risk.legal_basis)}</div>'
        )

    suggestion = risk.suggestion_edited or risk.suggestion
    if suggestion:
        tag = "推荐修改条款（法务已编辑）" if risk.suggestion_edited else "推荐修改条款"
        # 保留换行：合同条款的换行是有意义的，按行拆成多个 <div>
        quoted = "".join(
            f"<div>{_esc(line)}</div>" for line in suggestion.split("\n")
        )
        out.append(
            f'<div style="margin-top:4pt;"><span class="lbl">{_esc(tag)}：</span>'
            f'<div class="quote">{quoted}</div></div>'
        )
    if risk.adopted:
        out.append(
            '<div style="color:#389e0d;font-size:9.5pt;margin-top:3pt;">'
            "✅ 法务已采纳该建议</div>"
        )

    evs = bundle.evidences.get(risk.id, [])
    if evs:
        items = []
        for e in evs:
            src = "规则命中" if e.evidence_type == "rule" else "AI 研判"
            flag = "　⚠️ 待人工复核" if e.need_review else ""
            title = f"「{e.title}」" if e.title else ""
            items.append(
                f"<div>· <b>[{_esc(src)}]{_esc(flag)}</b> "
                f"{_esc(title)}{_esc(e.detail)}</div>"
            )
        out.append(
            '<div class="evidence"><span class="lbl">依据链：</span>'
            + "".join(items)
            + "</div>"
        )
    out.append("</div>")
    return "".join(out)


def render_html(bundle: ReportBundle) -> str:
    """把报告数据渲染成 HTML（供 PDF 排版用）。

    结构与 `render_markdown` 一致：合同基本信息 → 综合审查结论 →
    风险清单明细 → 附录。
    """
    c, t = bundle.contract, bundle.task
    out = [
        "<html><body>",
        f"<h1>合同审查报告：{_esc(c.title)}</h1>",
        f'<div class="gen">生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}'
        f"　|　审查任务 #{t.id}　|　合同 #{c.id}</div>",
        "<h2>一、合同基本信息</h2>",
        _info_table(bundle),
        "<h2>二、综合审查结论</h2>",
        _banner(bundle),
    ]

    counts = (
        f"高风险 {t.high_risk_count} 项、中风险 {t.medium_risk_count} 项、"
        f"低风险 {t.low_risk_count} 项"
    )
    out.append(f'<div><span class="lbl">风险分布：</span>{_esc(counts)}</div>')

    out.append("<h2>三、风险清单明细</h2>")
    if not bundle.risks:
        out.append("<div>未发现风险项。</div>")
    for idx, risk in enumerate(bundle.risks, start=1):
        out.append(_risk_block(bundle, idx, risk))

    appendix = [f"解析方式：{_parse_method(bundle)}", f"解析页数：{t.total_pages or '—'}"]
    if t.parse_duration_ms is not None:
        appendix.append(f"解析耗时：{t.parse_duration_ms} ms")
    if t.review_duration_ms is not None:
        appendix.append(f"审查耗时：{t.review_duration_ms} ms")
    appendix.append(f"任务状态：{t.status}（version={t.version}）")

    # 标题、列表、页脚说明包成一个整体：三者被拆到两页时，
    # 会留下"标题在上页、内容在下页"的断裂观感（实测）。
    out.append(
        '<div class="appendix">'
        "<h2>四、附录</h2>"
        + "".join(f"<div>{_esc(a)}</div>" for a in appendix)
        + '<div class="note">⚠️ 页码与位置基于解析时实际生效的排版引擎。'
        "DOCX 经 WPS 转换，页码可能与 Word 打开时不一致。<br/>"
        "本报告由合同审查系统自动生成，AI 产出的法律依据需人工复核。</div>"
        "</div>"
    )
    out.append("</body></html>")
    return "".join(out)


def render_pdf(bundle: ReportBundle) -> bytes:
    """把报告渲染成 PDF 字节流。

    **逐页放置**：`Story.place()` 返回 `more`，为 1 表示还有内容没放下，
    需新开一页继续。用 `DocumentWriter` 边写边排，避免整份报告驻留内存。

    与 `render_markdown` 是**平行实现**（各自独立成文），不共用中间产物：
    Markdown 要给人读原始文本、PDF 要精排，硬凑成一份中间表示反而两边都别扭。
    """
    story = pymupdf.Story(html=render_html(bundle), user_css=_CSS)
    mediabox = pymupdf.paper_rect(_PAGE_SIZE)
    where = mediabox + (_MARGIN, _MARGIN, -_MARGIN, -_MARGIN)

    # ⚠️ `DocumentWriter` 只能写**文件路径**（没有写内存的接口），
    # 因此落一个临时文件再处理。`delete=False` 是 Windows 上的必要选择：
    # writer 持有句柄期间文件无法删除。
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    page_count = 0
    try:
        writer = pymupdf.DocumentWriter(str(tmp_path))
        while True:
            dev = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
            page_count += 1
            if not more:
                break
            # 兜底：报告异常巨大时避免死循环（正常报告远小于此）
            if page_count >= 200:
                logger.warning("报告 PDF 超过 200 页，强制截断")
                break
        # ⚠️ close() 之后句柄仍被 writer 对象持有，Windows 上必须**先释放引用**
        # 再 GC，文件才可读/可删（实测：不 gc 直接读会 PermissionError）。
        writer.close()
        del writer, story, dev
        gc.collect()

        # 子集化字体：`@font-face` 会把整个 msyh.ttc（19MB）内嵌进 PDF，
        # 一份报告就几十兆。subset_fonts() 只保留用到的字形（实测 19.6MB → 45KB）。
        doc = pymupdf.open(str(tmp_path))
        try:
            doc.subset_fonts()
            data = doc.tobytes(deflate=True, garbage=3)
        finally:
            doc.close()
    finally:
        gc.collect()
        try:
            tmp_path.unlink(missing_ok=True)
        except PermissionError:
            # 极端情况下句柄未及时释放；临时目录由系统回收，不阻断导出
            logger.warning("临时 PDF 未能删除: %s", tmp_path)

    logger.info(
        "报告 PDF 渲染完成: contract=%s 页数=%s 字节=%s",
        bundle.contract.id, page_count, len(data),
    )
    return data
