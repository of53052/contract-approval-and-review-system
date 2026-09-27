/**
 * 报告 Markdown 渲染（零依赖）。
 *
 * **为什么不引 react-markdown / marked**：`AGENTS.md` 禁止擅自引入第三方依赖。
 * 而这份 Markdown 是**本系统自己生成**的（`report_service.render_markdown`），
 * 语法集合固定且很窄——标题、表格、无序列表、引用块、`<details>`、加粗、
 * 行内代码。为这点语法装一个通用 Markdown 引擎不划算，也会带来 XSS 面。
 *
 * ⚠️ **不使用 `dangerouslySetInnerHTML`**：报告正文里含合同原文与 LLM 产出，
 * 直接把 HTML 注入 DOM 等于把 XSS 的口子交给数据源。这里解析成 React 元素树，
 * 文本一律经 React 转义，从根上避免注入。
 *
 * 支持范围（与 `render_markdown` 的输出严格对应）：
 * - `#` / `##` / `###` 标题
 * - `| a | b |` 管道表格（含分隔行 `|---|---|`）
 * - `- xxx` 无序列表
 * - `> xxx` 引用块
 * - `<details><summary>…</summary>…</details>`（报告里的"依据链"折叠）
 * - `**加粗**`、`` `行内代码` ``
 * - 其余按普通段落处理
 */

import type { ReactNode } from "react";
import { Typography } from "antd";

/** 行内元素：加粗 + 行内代码。文本自动转义（React 负责）。 */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  // 用一次扫描同时切分 **粗体** 与 `代码`，避免嵌套正则的贪婪问题
  const pattern = /\*\*(.+?)\*\*|`([^`]+)`/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    if (match[1] !== undefined) {
      out.push(<strong key={`${keyPrefix}-b${i}`}>{match[1]}</strong>);
    } else {
      out.push(
        <code key={`${keyPrefix}-c${i}`} className="report-inline-code">
          {match[2]}
        </code>,
      );
    }
    last = match.index + match[0].length;
    i += 1;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** 管道表格 → <table>。`rows` 已去掉表头与分隔行。 */
function renderTable(header: string[], rows: string[][], key: string): ReactNode {
  return (
    <table className="report-table" key={key}>
      <thead>
        <tr>
          {header.map((h, i) => (
            <th key={i}>{renderInline(h, `${key}-h${i}`)}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, ri) => (
          <tr key={ri}>
            {r.map((cell, ci) => (
              <td key={ci}>{renderInline(cell, `${key}-${ri}-${ci}`)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** 按 `|` 切分表格行，去掉首尾空段。 */
function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((c) => c.trim());
}

/** 是否是表格分隔行（`|---|---|`）。 */
function isSeparatorRow(line: string): boolean {
  return /^\|?[\s:-]*-[\s:|-]*\|?$/.test(line.trim()) && line.includes("-");
}

/**
 * 把报告 Markdown 渲染成 React 节点。
 *
 * 逐行状态机：表格与 `<details>` 都是多行块，需要攒够再输出。
 */
export function renderReportMarkdown(md: string): ReactNode {
  const lines = md.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // ---------- 空行 ----------
    if (!trimmed) {
      i += 1;
      continue;
    }

    // ---------- <details> 折叠块 ----------
    if (trimmed.startsWith("<details>")) {
      const summaryMatch = trimmed.match(/<summary>(.*?)<\/summary>/);
      const summary = summaryMatch ? summaryMatch[1] : "展开";
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("</details>")) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // 跳过 </details>
      blocks.push(
        <details className="report-details" key={`d${key++}`}>
          <summary>{summary}</summary>
          <div className="report-details-body">
            {renderReportMarkdown(body.join("\n"))}
          </div>
        </details>,
      );
      continue;
    }

    // ---------- 表格 ----------
    if (trimmed.startsWith("|") && i + 1 < lines.length && isSeparatorRow(lines[i + 1])) {
      const header = splitRow(trimmed);
      i += 2; // 跳过表头与分隔行
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      blocks.push(renderTable(header, rows, `t${key++}`));
      continue;
    }

    // ---------- 标题 ----------
    const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      const text = heading[2];
      const node = (
        <div className={`report-h report-h${level}`} key={`h${key++}`}>
          {renderInline(text, `h${key}`)}
        </div>
      );
      blocks.push(node);
      i += 1;
      continue;
    }

    // ---------- 引用块 ----------
    if (trimmed.startsWith(">")) {
      const quoted: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quoted.push(lines[i].trim().replace(/^>\s?/, ""));
        i += 1;
      }
      blocks.push(
        <blockquote className="report-quote" key={`q${key++}`}>
          {quoted.map((q, qi) => (
            <div key={qi}>{renderInline(q, `q${key}-${qi}`)}</div>
          ))}
        </blockquote>,
      );
      continue;
    }

    // ---------- 无序列表 ----------
    if (/^[-*]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*]\s+/, ""));
        i += 1;
      }
      blocks.push(
        <ul className="report-list" key={`u${key++}`}>
          {items.map((it, ii) => (
            <li key={ii}>{renderInline(it, `u${key}-${ii}`)}</li>
          ))}
        </ul>,
      );
      continue;
    }

    // ---------- 水平分隔线 ----------
    if (/^-{3,}$/.test(trimmed)) {
      blocks.push(<hr className="report-hr" key={`r${key++}`} />);
      i += 1;
      continue;
    }

    // ---------- 普通段落（合并连续行，保留软换行） ----------
    const para: string[] = [];
    while (i < lines.length) {
      const t = lines[i].trim();
      if (!t || t.startsWith("#") || t.startsWith(">") || t.startsWith("|") ||
          t.startsWith("<details>") || /^[-*]\s+/.test(t) || /^-{3,}$/.test(t)) {
        break;
      }
      para.push(lines[i]);
      i += 1;
    }
    blocks.push(
      <Typography.Paragraph
        className="report-para"
        key={`p${key++}`}
        style={{ marginBottom: 8, whiteSpace: "pre-wrap" }}
      >
        {renderInline(para.join("\n"), `p${key}`)}
      </Typography.Paragraph>,
    );
  }

  return <>{blocks}</>;
}
