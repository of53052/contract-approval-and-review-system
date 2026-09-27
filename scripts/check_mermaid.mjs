import fs from 'fs';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.DOMParser = dom.window.DOMParser;
globalThis.Node = dom.window.Node;
globalThis.Element = dom.window.Element;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.SVGElement = dom.window.SVGElement;
globalThis.XMLSerializer = dom.window.XMLSerializer;
globalThis.getComputedStyle = dom.window.getComputedStyle;
try { Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true }); } catch {}

const { default: mermaid } = await import('mermaid');

const content = fs.readFileSync(process.argv[2], 'utf8');
const re = /```mermaid\s*\n([\s\S]*?)```/g;
const blocks = [];
let m;
while ((m = re.exec(content)) !== null) blocks.push(m[1].trim());

console.log(`共发现 ${blocks.length} 个 mermaid 代码块\n`);
mermaid.initialize({ startOnLoad: false, suppressErrorRendering: true, securityLevel: 'loose' });

let pass = 0; const failures = [];
for (let i = 0; i < blocks.length; i++) {
  const code = blocks[i];
  const head = code.split('\n')[0].trim();
  try {
    await mermaid.parse(code);
    console.log(`  [${String(i+1).padStart(2)}] ✅ ${head}`);
    pass++;
  } catch (e) {
    const msg = String(e.message || e).split('\n').slice(0, 6).join('\n       ');
    console.log(`  [${String(i+1).padStart(2)}] ❌ ${head}\n       ${msg}`);
    failures.push({ i: i+1, head, msg });
  }
}
console.log(`\n结果: ${pass} 通过, ${failures.length} 失败`);
process.exit(failures.length > 0 ? 1 : 0);
