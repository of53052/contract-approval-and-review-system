/**
 * 锚点坐标换算：后端 PDF point（左上原点）→ PDF.js 视图像素。
 *
 * ⚠️ 这是**唯一实现**：前端组件与回归校验脚本都从这里导入，
 * 避免两处各写一份公式导致"校验通过但页面画错"。
 *
 * 为什么不用 `viewport.convertToViewportRectangle`：
 *   该方法假定输入是 PDF 原始坐标系（左下原点）并会翻转 y。
 *   本项目锚点统一为左上原点（见 docs/architecture.md §6.2），
 *   实测用该方法 6 个锚点只有 1 个落在引用文字上；用下面的公式则 6/6 命中。
 */

/** 视图像素框 */
export interface PixelBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

/**
 * 把锚点 bbox 换算成视图像素框。
 *
 * 成立前提：`page.rotate === 0`。本项目 PDF 由 WPS 转换或原生上传产生，
 * 均为正向页面；将来若出现旋转页，需在此按 rotate 分支处理。
 *
 * @param bbox `[x0, y0, x1, y1]`，PDF point，左上原点
 * @param scale 缩放倍率
 * @param viewBox PDF 坐标下的页面范围，通常 `[0, 0, w, h]`
 */
export function anchorToPixelBox(
  bbox: [number, number, number, number],
  scale: number,
  viewBox: [number, number, number, number],
): PixelBox {
  const [x0, y0, x1, y1] = bbox;
  const [vx0, vy0] = viewBox;
  return {
    left: (x0 - vx0) * scale,
    top: (y0 - vy0) * scale,
    width: (x1 - x0) * scale,
    height: (y1 - y0) * scale,
  };
}

/**
 * 复刻 PDF.js 文本层单个 span 的定位（与官方 text_layer 算法一致）。
 * 返回 null 表示该 item 字号为 0，应跳过。
 */
export function textSpanBox(
  item: { transform: number[]; width: number },
  viewportTransform: number[],
  scale: number,
): PixelBox | null {
  const m = viewportTransform;
  const t = item.transform;
  const tx = [
    m[0] * t[0] + m[2] * t[1],
    m[1] * t[0] + m[3] * t[1],
    m[0] * t[2] + m[2] * t[3],
    m[1] * t[2] + m[3] * t[3],
    m[0] * t[4] + m[2] * t[5] + m[4],
    m[1] * t[4] + m[3] * t[5] + m[5],
  ];
  const fontHeight = Math.hypot(tx[2], tx[3]);
  if (fontHeight === 0) return null;
  return {
    left: tx[4],
    top: tx[5] - fontHeight,
    width: item.width * scale,
    height: fontHeight,
  };
}

/** 两个矩形是否相交。 */
export function boxesIntersect(a: PixelBox, b: PixelBox): boolean {
  return (
    a.left < b.left + b.width &&
    a.left + a.width > b.left &&
    a.top < b.top + b.height &&
    a.top + a.height > b.top
  );
}

/** 归一化：去掉全部空白，便于比对引用片段。 */
export function normalizeText(s: string | null | undefined): string {
  return (s || "").replace(/[\s\u3000]+/g, "");
}
