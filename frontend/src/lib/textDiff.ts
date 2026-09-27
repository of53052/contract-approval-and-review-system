/**
 * 极简字符级 diff（LCS），用于"条款差异对比"（PRD 2.4.5）。
 *
 * **为什么自己写而不引第三方**：项目约定不擅自引入新依赖（AGENTS.md），
 * 而这里只需要"把两段中文并排标出增删"这一个能力。用 `diff` / `diff-match-patch`
 * 会为几十行逻辑增加一个包与一条供应链风险。
 *
 * 复杂度 O(n·m)：条款正文通常几百字，量级完全可接受。
 * 超过 `MAX_CELLS` 时**放弃精细对比**，退化为"整段替换"——
 * 宁可少标几个相同的字，也不能让卡片渲染卡住主线程。
 */

export type DiffKind = "same" | "add" | "del";

export interface DiffSegment {
  kind: DiffKind;
  text: string;
}

/** DP 表格的单元格上限（约 1200 字 × 1200 字）。 */
const MAX_CELLS = 1_440_000;

/** 归一化：把连续空白折叠为单个空格，避免换行差异污染对比结果。 */
function normalize(s: string): string {
  return s.replace(/[\s\u3000]+/g, " ").trim();
}

/**
 * 比较两段文本，返回按顺序排列的片段序列。
 *
 * 调用方按 `kind` 上色即可：`same` 原样、`del` 标红、`add` 标绿。
 */
export function diffChars(before: string, after: string): DiffSegment[] {
  const a = normalize(before);
  const b = normalize(after);
  if (!a) return b ? [{ kind: "add", text: b }] : [];
  if (!b) return [{ kind: "del", text: a }];
  if (a === b) return [{ kind: "same", text: a }];

  if (a.length * b.length > MAX_CELLS) {
    // 退化路径：不做逐字对比，整段替换
    return [
      { kind: "del", text: a },
      { kind: "add", text: b },
    ];
  }

  // LCS 长度表：dp[i][j] = a[i:] 与 b[j:] 的最长公共子序列长度
  const n = a.length;
  const m = b.length;
  const dp: Uint32Array = new Uint32Array((n + 1) * (m + 1));
  const at = (i: number, j: number) => i * (m + 1) + j;
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[at(i, j)] =
        a[i] === b[j]
          ? dp[at(i + 1, j + 1)] + 1
          : Math.max(dp[at(i + 1, j)], dp[at(i, j + 1)]);
    }
  }

  // 回溯生成片段，相邻同类片段合并
  const out: DiffSegment[] = [];
  const push = (kind: DiffKind, ch: string) => {
    const last = out[out.length - 1];
    if (last && last.kind === kind) last.text += ch;
    else out.push({ kind, text: ch });
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      push("same", a[i]);
      i++;
      j++;
    } else if (dp[at(i + 1, j)] >= dp[at(i, j + 1)]) {
      push("del", a[i]);
      i++;
    } else {
      push("add", b[j]);
      j++;
    }
  }
  while (i < n) push("del", a[i++]);
  while (j < m) push("add", b[j++]);
  return out;
}
