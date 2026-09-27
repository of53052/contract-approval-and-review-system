/**
 * 合同正文视窗（架构 §11.4、§14.3）。
 *
 * **统一渲染路径**：所有格式（DOCX/PDF/扫描件）都由后端收敛成 PDF，
 * 因此这里只需要一套 PDF.js 渲染逻辑。
 *
 * 双向锚定的实现：
 * - 风险卡片 → 正文：`focusAnchor` 变化时跳页 + 滚动 + 高亮（`active` 样式）
 * - 正文 → 风险卡片：点击高亮框或文本层 → `onPickRisk(riskId)`
 *
 * 坐标：后端 bbox 是 **PDF point、左上原点**，与 PDF.js 文本层同一坐标系。
 *
 * ⚠️ **不能用 `viewport.convertToViewportRectangle`**：该方法假定输入是
 * PDF 原始坐标系（左下原点）并会翻转 y，而后端锚点是左上原点。
 * 实测：对同一份合同，用该方法 6 个锚点只有 1 个落在引用文字上；
 * 直接按 `(坐标 - viewBox 原点) × scale` 换算则 6/6 精确命中。
 *
 * 该换算成立的前提是 `page.rotate === 0`。本项目的 PDF 由 WPS 转换或
 * 原生上传产生，均为正向页面；若将来出现旋转页，需在此按 rotate 分支处理。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Empty, Flex, InputNumber, Spin, Tag, Typography } from "antd";
import { ZoomInOutlined, ZoomOutOutlined } from "@ant-design/icons";
import * as pdfjsLib from "pdfjs-dist";
import workerSrc from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";
import type { RiskItem } from "../types";
import { anchorToPixelBox, textSpanBox } from "../lib/anchorCoords";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerSrc;

interface Props {
  /** 合同 ID，用于拼 PDF 直链 */
  contractId: number;
  /** 风险清单；只画有锚点的项 */
  risks: RiskItem[];
  /** 当前聚焦的锚点（来自右侧卡片点击） */
  focusAnchor: { riskId: number; pageNo: number; bbox: number[] } | null;
  /** 正文 → 卡片：点中某风险的高亮 */
  onPickRisk: (riskId: number) => void;
}

/** 一页的锚点盒子（已换算为像素） */
interface HighlightBox {
  riskId: number;
  level: RiskItem["risk_level"];
  left: number;
  top: number;
  width: number;
  height: number;
}

export default function PdfViewer({ contractId, risks, focusAnchor, onPickRisk }: Props) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [scale, setScale] = useState(1.25);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef<Map<number, HTMLDivElement>>(new Map());

  // 加载 PDF
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const task = pdfjsLib.getDocument({ url: `/api/contracts/${contractId}/pdf` });
    task.promise.then(
      (doc) => {
        if (cancelled) return;
        setPdf(doc);
        setPageCount(doc.numPages);
        setLoading(false);
      },
      (err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof Error ? err.message : "PDF 加载失败（可能转换未完成）",
        );
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
      task.destroy();
    };
  }, [contractId]);

  // 聚焦锚点：跳页 + 滚动到可视区
  useEffect(() => {
    if (!focusAnchor) return;
    const el = pageRefs.current.get(focusAnchor.pageNo);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [focusAnchor]);

  const zoom = (delta: number) =>
    setScale((s) => Math.min(3, Math.max(0.5, +(s + delta).toFixed(2))));

  return (
    <Flex vertical style={{ height: "100%" }} gap={8}>
      <Flex justify="space-between" align="center" style={{ paddingInline: 4 }}>
        <Typography.Text type="secondary">
          {loading ? "加载中…" : `${pageCount} 页 · 缩放 ${Math.round(scale * 100)}%`}
        </Typography.Text>
        <Flex gap={4} align="center">
          <Button size="small" icon={<ZoomOutOutlined />} onClick={() => zoom(-0.15)} />
          <InputNumber
            size="small"
            style={{ width: 64 }}
            min={50}
            max={300}
            step={10}
            value={Math.round(scale * 100)}
            formatter={(v) => `${v}%`}
            parser={(v) => Number((v ?? "").replace("%", ""))}
            onChange={(v) => v && setScale(v / 100)}
          />
          <Button size="small" icon={<ZoomInOutlined />} onClick={() => zoom(0.15)} />
        </Flex>
      </Flex>

      <div
        ref={containerRef}
        style={{
          flex: 1,
          overflow: "auto",
          background: "#e9ebee",
          padding: 12,
          borderRadius: 6,
        }}
      >
        {error && (
          <Alert
            type="warning"
            showIcon
            message="正文无法渲染"
            description={`${error}。审查结果仍可正常查看。`}
          />
        )}
        {loading && (
          <Flex justify="center" style={{ paddingTop: 80 }}>
            <Spin tip="正在渲染合同正文…" />
          </Flex>
        )}
        {!loading && !error && pdf && (
          <Flex vertical gap={12} align="center">
            {Array.from({ length: pageCount }, (_, i) => i + 1).map((pageNo) => (
              <PdfPage
                key={pageNo}
                pdf={pdf}
                pageNo={pageNo}
                scale={scale}
                risks={risks}
                activeRiskId={focusAnchor?.riskId ?? null}
                onPickRisk={onPickRisk}
                registerRef={(el) => {
                  if (el) pageRefs.current.set(pageNo, el);
                  else pageRefs.current.delete(pageNo);
                }}
              />
            ))}
          </Flex>
        )}
        {!loading && !error && !pdf && <Empty description="没有可渲染的正文" />}
      </div>
    </Flex>
  );
}

/** 单页渲染：canvas（内容）+ 文本层（可选中）+ 覆盖层（高亮框） */
function PdfPage({
  pdf,
  pageNo,
  scale,
  risks,
  activeRiskId,
  onPickRisk,
  registerRef,
}: {
  pdf: PDFDocumentProxy;
  pageNo: number;
  scale: number;
  risks: RiskItem[];
  activeRiskId: number | null;
  onPickRisk: (riskId: number) => void;
  registerRef: (el: HTMLDivElement | null) => void;
}) {
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);

  // 加载页面
  useEffect(() => {
    let cancelled = false;
    pdf.getPage(pageNo).then((p) => {
      if (!cancelled) setPage(p);
    });
    return () => {
      cancelled = true;
    };
  }, [pdf, pageNo]);

  // 渲染 canvas
  useEffect(() => {
    if (!page) return;
    const viewport = page.getViewport({ scale });
    setSize({ width: viewport.width, height: viewport.height });

    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // 高分屏下用 devicePixelRatio 提升清晰度
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(viewport.width * dpr);
    canvas.height = Math.floor(viewport.height * dpr);
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;

    const task = page.render({
      canvasContext: ctx,
      viewport,
      transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
    });
    task.promise.catch(() => {
      /* 渲染被取消（快速缩放）时忽略 */
    });
  }, [page, scale]);

  // 渲染文本层
  useEffect(() => {
    if (!page || !textRef.current) return;
    const container = textRef.current;
    container.innerHTML = "";
    const viewport = page.getViewport({ scale });

    page.getTextContent().then((content) => {
      // `styles` 是 fontName → 字体族的映射。用 PDF 自己的字体族而非
      // 通用 sans-serif：字体度量越接近，文本层与 canvas 的对齐越好
      // （文字已设为 transparent，此处只为让命中框与文字形状吻合）。
      const styles = (content as { styles?: Record<string, { fontFamily?: string }> })
        .styles;
      for (const item of content.items) {
        if (!("str" in item) || !item.str) continue;
        // 与官方文本层算法一致；实现抽到 lib/anchorCoords 供回归校验复用
        const box = textSpanBox(item, viewport.transform, scale);
        if (!box) continue;
        const span = document.createElement("span");
        span.textContent = item.str;
        span.style.left = `${box.left}px`;
        span.style.top = `${box.top}px`;
        span.style.fontSize = `${box.height}px`;
        span.style.fontFamily =
          styles?.[item.fontName]?.fontFamily || "sans-serif";
        span.dataset.x = String(box.left);
        span.dataset.y = String(box.top);
        span.dataset.w = String(box.width);
        span.dataset.h = String(box.height);
        container.appendChild(span);
      }
    });
  }, [page, scale]);

  // 计算本页高亮框（像素坐标）
  const boxes = useMemo<HighlightBox[]>(() => {
    if (!page) return [];
    const viewport = page.getViewport({ scale });
    // viewBox 是 PDF 坐标下的页面范围，通常为 [0, 0, w, h]
    const [vx0, vy0] = viewport.viewBox;
    const out: HighlightBox[] = [];
    for (const risk of risks) {
      for (const a of risk.anchors) {
        if (a.page_no !== pageNo) continue;
        const box = anchorToPixelBox(
          [a.bbox_x0, a.bbox_y0, a.bbox_x1, a.bbox_y1],
          scale,
          [vx0, vy0, vx0 + viewport.width / scale, vy0 + viewport.height / scale],
        );
        out.push({ riskId: risk.id, level: risk.risk_level, ...box });
      }
    }
    return out;
  }, [page, scale, risks, pageNo]);

  // 文本层点击 → 命中最近的高亮框 → 反向定位卡片
  const onTextClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const target = e.target as HTMLElement;
      if (target.tagName !== "SPAN" || !target.dataset.x) return;
      const x = Number(target.dataset.x);
      const y = Number(target.dataset.y);
      const w = Number(target.dataset.w);
      const h = Number(target.dataset.h);
      const hit = boxes.find(
        (b) =>
          x < b.left + b.width && x + w > b.left && y < b.top + b.height && y + h > b.top,
      );
      if (hit) onPickRisk(hit.riskId);
    },
    [boxes, onPickRisk],
  );

  return (
    <div
      ref={registerRef}
      style={{
        position: "relative",
        width: size.width || undefined,
        height: size.height || undefined,
        background: "#fff",
        boxShadow: "0 1px 4px rgba(0,0,0,0.15)",
        scrollMarginTop: 12,
      }}
    >
      <canvas ref={canvasRef} style={{ display: "block" }} />
      <div className="pdf-text-layer" ref={textRef} onClick={onTextClick} />
      {boxes.map((b, i) => (
        <div
          key={`${b.riskId}-${i}`}
          className={`pdf-highlight level-${b.level}${
            b.riskId === activeRiskId ? " active" : ""
          }`}
          style={{
            left: b.left,
            top: b.top,
            width: b.width,
            height: b.height,
            pointerEvents: "auto",
            cursor: "pointer",
          }}
          title="点击定位到右侧风险卡片"
          onClick={() => onPickRisk(b.riskId)}
        />
      ))}
      <Tag
        style={{ position: "absolute", right: 4, bottom: 4, opacity: 0.6 }}
        color="default"
      >
        {pageNo}
      </Tag>
    </div>
  );
}
