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
 *
 * **缩放策略（按页宽自适应）**：默认把整页宽度放进可视区（`scaleMode="auto"`），
 * 换合同/拉窗口宽时自动重算；用户手动缩放后切到 `manual`，点"适应宽度"切回。
 *
 * `scale` 是**派生值**（见下方 `useMemo`），不是独立 state：若先用初始倍率渲染、
 * 再在 effect 里 `setScale` 纠正，同一 canvas 会在上一帧渲染未完成时被再次
 * `page.render()`，pdfjs 4.x 直接抛 "Cannot use the same canvas during multiple
 * render() operations"，留下"尺寸已换、内容未换"的残影——表现就是首屏内容错乱/颠倒。
 *
 * ⚠️ **只能改 `scale` 这一个比例值，绝不能用 CSS 拉伸 canvas**：canvas、PDF.js
 * 文本层、高亮框三者都由 `scale` 推导（见 `anchorToPixelBox`）。一旦用
 * `transform` / `width` 去拉伸容器，三者会各自按不同比例缩放，高亮与文字错位。
 * 同理不得对正文使用 `rotate` / `scaleY(-1)` 之类的变换——那会让内容颠倒。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Empty, Flex, InputNumber, Spin, Tag, Typography } from "antd";
import { ZoomInOutlined, ZoomOutOutlined } from "@ant-design/icons";
import * as pdfjsLib from "pdfjs-dist";
import workerSrc from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";
import type { ContractMetadataItem, RiskItem } from "../types";
import { anchorToPixelBox, textSpanBox } from "../lib/anchorCoords";
import { scrollElementToCenter } from "../lib/scroll";
import { METADATA_LABEL } from "../constants";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerSrc;

/** 缩放上下限。下限取 0.25：工作台左右分栏时可窄至 300px 出头，仅有 0.5 会放不下整页。 */
const MIN_SCALE = 0.25;
const MAX_SCALE = 3;

/** 未测到容器/页宽前的兜底倍率；测到后由自适应值取代（见 `scale` 派生）。 */
const DEFAULT_SCALE = 1.25;

/** 正文滚动容器的水平内边距（px）。**必须与此处 `padding` 取值一致**：
 *  适应页宽时要把它扣掉，否则会稳定多出一条横向滚动条。 */
const CONTAINER_PADDING = 12;

/** 缩放模式：auto = 跟随容器宽度自适应；manual = 用户手动缩放后不再自动改。 */
type ScaleMode = "auto" | "manual";

/** 把缩放倍数约束到合法区间。非数值一律回落到 1，避免"NaN% 缩放"。 */
function clampScale(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, +value.toFixed(3)));
}

/**
 * 按页宽算"适应宽度"的缩放倍数。
 *
 * @param availableWidth 可视区内容宽度（px，**已扣掉容器内边距**）
 * @param pageWidth 该页在 `scale=1` 下的宽度（PDF point，与 px 同尺度）
 */
function fitWidthScale(availableWidth: number, pageWidth: number): number {
  if (!(availableWidth > 0) || !(pageWidth > 0)) return 1;
  return clampScale(availableWidth / pageWidth);
}

interface Props {
  /** 合同 ID，用于拼 PDF 直链 */
  contractId: number;
  /** 风险清单；只画有锚点的项 */
  risks: RiskItem[];
  /** 元数据清单；只画有锚点的项（PRD 2.4.3「高亮标记提取的元数据字段」） */
  metadata?: ContractMetadataItem[];
  /** 当前聚焦的锚点（来自右侧卡片点击）。`seq` 每次点击自增，
   *  保证"重复点同一张卡片"也会重新触发滚动（否则对象不变、effect 不跑）。 */
  focusAnchor: { riskId: number; pageNo: number; bbox: number[]; seq: number } | null;
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

/** 一页的元数据盒子（已换算为像素） */
interface MetaBox {
  metaId: number;
  label: string;
  /** 提取置信度（OCR 来源 < 1.0）；原生文本层恒为 1 */
  confidence: number;
  /** 低置信度标记：渲染为橙色虚线 + "待核对"角标 */
  needReview: boolean;
  left: number;
  top: number;
  width: number;
  height: number;
}

export default function PdfViewer({
  contractId,
  risks,
  metadata,
  focusAnchor,
  onPickRisk,
}: Props) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pageCount, setPageCount] = useState(0);
  /** 手动模式下的倍率；auto 模式下被忽略（实际倍率由 `scale` 派生）。 */
  const [manualScale, setManualScale] = useState(DEFAULT_SCALE);
  const [scaleMode, setScaleMode] = useState<ScaleMode>("auto");
  /** 首屏页在 scale=1 下的宽度（PDF point）。用于算"适应宽度"的倍数。 */
  const [basePageWidth, setBasePageWidth] = useState(0);
  /** 可视区内容宽度（已扣内边距）。容器宽度变化时更新，驱动自动缩放。 */
  const [viewportWidth, setViewportWidth] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

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
        // 换合同即回到自动适配（新文件尺寸可能不同）
        setScaleMode("auto");
        // 取第 1 页的原始宽度作为"适应宽度"的基准。
        // 页宽用 scale=1 的 viewport.width：PDF point 与 px 同尺度，
        // 且已含 viewBox 偏移量，比 raw 的 mediaBox 更贴近实际渲染宽度。
        // ⚠️ 必须**等页宽测完再结束 loading**：`scale` 是按页宽派生的，若先
        // setLoading(false) 再补页宽，会先按兜底倍率渲染一帧、页宽到达后又渲染
        // 一帧——同一 canvas 上一帧未完成时被二次 render，正是首屏错乱/颠倒的成因。
        // 用 finally 保证成功、失败都不会卡住 loading。
        doc.getPage(1)
          .then(
            (p) => {
              if (!cancelled) setBasePageWidth(p.getViewport({ scale: 1 }).width);
            },
            () => {
              /* 取页宽失败只影响自动缩放，不阻断正文渲染 */
            },
          )
          .finally(() => {
            if (!cancelled) setLoading(false);
          });
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

  // 有锚点的元数据项数量（用于头部图例）
  const metaCount = useMemo(
    () => (metadata ?? []).filter((m) => m.anchors.length > 0).length,
    [metadata],
  );

  /** 可视区内容宽度 = 容器内容盒宽度 - 左右内边距。 */
  const measure = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const inner = el.clientWidth - CONTAINER_PADDING * 2;
    // 仅在"取整像素后确实变了"时更新，避免子像素抖动引发渲染循环
    setViewportWidth((prev) => (Math.abs(prev - inner) >= 1 ? inner : prev));
  }, []);

  // 监听容器尺寸：窗口缩放、分栏拖拽、侧栏折叠都能被 ResizeObserver 捕获
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure]);

  // 实际渲染倍率：**派生值而非 state**。
  // 容器宽度变化（窗口缩放 / 分栏拖拽 / 侧栏折叠）会让 viewportWidth 变，这里
  // 随之重算；用户手动缩放后 scaleMode="manual"，改由 manualScale 决定。
  // 因为是派生值，首帧就已是最终倍率，不存在"先按旧倍率渲染再纠正"的中间态。
  const scale = useMemo(() => {
    if (scaleMode === "manual") return manualScale;
    if (basePageWidth > 0 && viewportWidth > 0) {
      return fitWidthScale(viewportWidth, basePageWidth);
    }
    return DEFAULT_SCALE;
  }, [scaleMode, manualScale, basePageWidth, viewportWidth]);

  /** 手动缩放：切到 manual，不再被容器宽度覆盖。 */
  const setScaleManually = (next: number) => {
    setScaleMode("manual");
    setManualScale(clampScale(next));
  };

  /** 回到"适应宽度"（auto 模式；此后容器宽度变化会继续跟随）。 */
  const fitToWidth = () => setScaleMode("auto");

  const zoom = (delta: number) => setScaleManually(scale + delta);

  return (
    <Flex vertical style={{ height: "100%" }} gap={8}>
      <Flex justify="space-between" align="center" style={{ paddingInline: 4 }}>
        <Flex align="center" gap={8}>
          <Typography.Text type="secondary">
            {loading ? "加载中…" : `${pageCount} 页 · 缩放 ${Math.round(scale * 100)}%`}
          </Typography.Text>
          {metaCount > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              <span className="pdf-meta-legend" /> 提取字段 {metaCount} 项
            </Typography.Text>
          )}
        </Flex>
        <Flex gap={4} align="center">
          <Button size="small" icon={<ZoomOutOutlined />} onClick={() => zoom(-0.15)} />
          <InputNumber
            size="small"
            style={{ width: 64 }}
            min={Math.round(MIN_SCALE * 100)}
            max={Math.round(MAX_SCALE * 100)}
            step={10}
            value={Math.round(scale * 100)}
            formatter={(v) => `${v}%`}
            parser={(v) => Number((v ?? "").replace("%", ""))}
            onChange={(v) => v && setScaleManually(v / 100)}
          />
          <Button size="small" icon={<ZoomInOutlined />} onClick={() => zoom(0.15)} />
          <Button
            size="small"
            onClick={fitToWidth}
            type={scaleMode === "auto" ? "primary" : "default"}
            disabled={basePageWidth <= 0}
          >
            适应宽度
          </Button>
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
          <Flex justify="center" align="center" gap={8} style={{ paddingTop: 80 }}>
            <Spin />
            <Typography.Text type="secondary">正在渲染合同正文…</Typography.Text>
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
                metadata={metadata}
                activeRiskId={focusAnchor?.riskId ?? null}
                onPickRisk={onPickRisk}
                focusAnchor={focusAnchor}
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
  metadata,
  activeRiskId,
  onPickRisk,
  focusAnchor,
}: {
  pdf: PDFDocumentProxy;
  pageNo: number;
  scale: number;
  risks: RiskItem[];
  metadata?: ContractMetadataItem[];
  activeRiskId: number | null;
  onPickRisk: (riskId: number) => void;
  focusAnchor: { riskId: number; pageNo: number; bbox: number[]; seq: number } | null;
}) {
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  /** 本页高亮框 DOM，key 为 `${riskId}:${index}`；用于滚动居中。 */
  const boxRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  /** 上一次见到的 focusAnchor.seq，避免 scale 变化时重复滚动。 */
  const prevSeqRef = useRef<number | null>(null);

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
      /* 渲染被取消（快速缩放 / 组件卸载）时忽略 */
    });
    // ⚠️ 必须在 cleanup 里取消：pdfjs 4.x 禁止同一 canvas 并发渲染（会抛
    // "Cannot use the same canvas during multiple render() operations"）。
    // React StrictMode 的挂载→卸载→再挂载、以及连续的 scale 变化都会让本
    // effect 重跑，不取消就会留下"尺寸已换、内容未换"的错乱残影。
    return () => task.cancel();
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

  // 计算本页的元数据盒子（像素坐标）。
  //
  // 与风险高亮分开渲染：元数据用**虚线细框 + 上方小标签**，风险用半透明
  // 实底。两者可重叠（如"合同金额"落在"付款方式"条款内），视觉上必须能区分，
  // 否则用户分不清哪个是风险、哪个只是提取到的字段。
  const metaBoxes = useMemo<MetaBox[]>(() => {
    if (!page || !metadata?.length) return [];
    const viewport = page.getViewport({ scale });
    const [vx0, vy0] = viewport.viewBox;
    const out: MetaBox[] = [];
    for (const m of metadata) {
      for (const a of m.anchors) {
        if (a.page_no !== pageNo) continue;
        const box = anchorToPixelBox(
          [a.bbox_x0, a.bbox_y0, a.bbox_x1, a.bbox_y1],
          scale,
          [vx0, vy0, vx0 + viewport.width / scale, vy0 + viewport.height / scale],
        );
        out.push({
          metaId: m.id,
          label: METADATA_LABEL[m.meta_key] || m.meta_key,
          confidence: m.confidence,
          needReview: m.need_review,
          ...box,
        });
      }
    }
    return out;
  }, [page, scale, metadata, pageNo]);

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

  // 聚焦锚点：把命中本页的高亮框滚到容器垂直居中。
  //
  // ⚠️ 必须放在 PdfPage 内而不是父组件：高亮框要等 `page` 加载完、
  // `boxes` 算出来才存在，父组件此刻拿不到它的 DOM，只能退而滚整页。
  //
  // 触发条件是"seq 出现新值"，不是"boxes 变了"：scale 变化会让 boxes
  // 重算并重跑本 effect，但用户的手动缩放不该把视图拽回锚点。
  // 用"比较上一次的 seq"而非"是否处理过某个值"——本组件会随页码
  // 增删重挂载，后者会把历史 seq 误判成一次新定位。
  useEffect(() => {
    const seqChanged = prevSeqRef.current !== focusAnchor?.seq;
    prevSeqRef.current = focusAnchor?.seq ?? null;
    if (!seqChanged || !focusAnchor || focusAnchor.pageNo !== pageNo) return;
    const idx = boxes.findIndex((b) => b.riskId === focusAnchor.riskId);
    if (idx < 0) return;
    const el = boxRefs.current.get(`${focusAnchor.riskId}:${idx}`);
    if (!el) return;
    scrollElementToCenter(el);
  }, [focusAnchor, pageNo, boxes]);

  return (
    <div
      style={{
        position: "relative",
        width: size.width || undefined,
        height: size.height || undefined,
        background: "#fff",
        boxShadow: "0 1px 4px rgba(0,0,0,0.15)",
      }}
    >
      <canvas ref={canvasRef} style={{ display: "block" }} />
      <div className="pdf-text-layer" ref={textRef} onClick={onTextClick} />
      {metaBoxes.map((b, i) => (
        <div
          key={`meta-${b.metaId}-${i}`}
          className={`pdf-meta-highlight${b.needReview ? " need-review" : ""}`}
          style={{
            left: b.left,
            top: b.top,
            width: b.width,
            height: b.height,
          }}
          title={
            b.needReview
              ? `提取字段：${b.label}（识别置信度 ${(b.confidence * 100).toFixed(0)}%，请人工核对）`
              : `提取字段：${b.label}`
          }
        >
          <span className="pdf-meta-label">
            {b.label}
            {b.needReview && " ⚠"}
          </span>
        </div>
      ))}
      {boxes.map((b, i) => (
        <div
          key={`${b.riskId}-${i}`}
          ref={(el) => {
            const k = `${b.riskId}:${i}`;
            if (el) boxRefs.current.set(k, el);
            else boxRefs.current.delete(k);
          }}
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
