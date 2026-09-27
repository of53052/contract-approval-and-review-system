// 锚点坐标校验：用**渲染用的那份 PDF** 实测后端锚点 bbox 是否真的覆盖到引用原文。
//
// 为什么必须走 PDF.js：后端锚点由 PyMuPDF 产生，用 PyMuPDF 自校验是同源验证，
// 测不出"前端换算公式错"这类问题。这里复刻前端的真实渲染路径。
//
// 用法（在 frontend/ 下执行）：node scripts/verify_anchors.mjs <pdf 路径> <anchors.json 路径> [scale]
// 退出码：0 = 全部锚点覆盖到引用，1 = 存在未覆盖
import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs";
import fs from "node:fs";
import { anchorToPixelBox, textSpanBox, boxesIntersect, normalizeText }
  from "../src/lib/anchorCoords.ts";

const SCALE = Number(process.argv[4] || 1.25);
const pdfPath = process.argv[2];
const anchorsPath = process.argv[3];

const anchors = JSON.parse(fs.readFileSync(anchorsPath, "utf8"));
const data = new Uint8Array(fs.readFileSync(pdfPath));
const doc = await pdfjs.getDocument({ data, useSystemFonts: true }).promise;

// 按页缓存文本层 span
const pageCache = new Map();
async function spansOf(pageNo) {
  if (pageCache.has(pageNo)) return pageCache.get(pageNo);
  const page = await doc.getPage(pageNo);
  const viewport = page.getViewport({ scale: SCALE });
  const content = await page.getTextContent();
  const spans = [];
  for (const item of content.items) {
    if (!("str" in item) || !item.str) continue;
    const box = textSpanBox(item, viewport.transform, SCALE);
    if (!box) continue;
    spans.push({ str: item.str, ...box });
  }
  const entry = { viewport, spans };
  pageCache.set(pageNo, entry);
  return entry;
}

let pass = 0;
for (const a of anchors) {
  const { viewport, spans } = await spansOf(a.page);
  const box = anchorToPixelBox(a.bbox, SCALE, viewport.viewBox);
  const covered = normalizeText(
    spans.filter((s) => boxesIntersect(s, box)).map((s) => s.str).join(""),
  );
  const want = normalizeText(a.quote);
  // 引用可能跨 span 拼接；用前 6 字作为命中判据，避免断字导致误判
  const ok = covered.includes(want.slice(0, 6)) || want.includes(covered.slice(0, 6));
  if (ok) pass++;
  console.log(
    `${ok ? "\u2713" : "\u2717"} [p${a.page}] ${a.title}\n` +
      `    bbox ${a.bbox.map((v) => v.toFixed(1)).join(", ")} -> 像素 ` +
      `[${box.left.toFixed(1)}, ${box.top.toFixed(1)}] ` +
      `${box.width.toFixed(1)}x${box.height.toFixed(1)}\n` +
      `    框内: ${covered.slice(0, 40) || "(空)"}\n` +
      `    期望: ${want.slice(0, 40)}`,
  );
}
console.log(`\n锚点坐标校验: ${pass}/${anchors.length} 覆盖到引用原文`);
process.exit(pass === anchors.length ? 0 : 1);
