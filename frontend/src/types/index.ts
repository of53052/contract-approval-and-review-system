/**
 * 与后端 `app/schemas` 一一对应的类型定义。
 *
 * 刻意手写而非从 OpenAPI 生成：阶段一接口少，手写更直观；
 * 后端字段改名时前端会编译报错，这正是我们要的（架构 §14.2 类型安全）。
 */

/** 后端分页信封（app/schemas/common.py 的 Page[T]）。 */
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export type RiskLevel = "high" | "medium" | "low";
export type TaskStatus =
  | "pending" | "parsing" | "reviewing" | "completed" | "blocked" | "failed";
export type WritebackStatus = "not_written" | "writing" | "success" | "failed";
export type ReviewConclusion = "pass" | "rectify" | "reject";
export type AnchorLevel = "exact" | "fuzzy" | "paragraph" | "none";

export interface ContractListItem {
  id: number;
  title: string;
  contract_no: string | null;
  business_type: string;
  amount: string | null;
  currency: string | null;
  applicant: string | null;
  applicant_dept: string | null;
  counterparty_name: string | null;
  task_id: number | null;
  status: TaskStatus;
  overall_risk: RiskLevel | null;
  conclusion: ReviewConclusion | null;
  high_risk_count: number;
  medium_risk_count: number;
  low_risk_count: number;
  writeback_status: WritebackStatus;
  blocked_reason: string | null;
  parsed_pages: number;
  total_pages: number | null;
  created_at: string;
}

export interface ContractDetail extends ContractListItem {
  file_format: string;
  file_name: string;
  file_size: number;
  pdf_object_key: string | null;
  source: string;
  external_id: string | null;
  summary: string | null;
}

/** 元数据项（`GET /api/contracts/{id}/metadata`）。
 * `anchors` 是该字段在原文中的位置，供正文区高亮（PRD 2.4.3）。 */
export interface ContractMetadataItem {
  id: number;
  meta_key: string;
  meta_value: string | null;
  value_normalized: string | null;
  value_type: string;
  confidence: number;
  need_review: boolean;
  anchors: Anchor[];
}

export interface Clause {
  id: number;
  clause_type: string;
  clause_no: string | null;
  title: string | null;
  content: string;
  page_no: number;
  page_end: number | null;
  para_index: number;
  char_start: number | null;
  char_end: number | null;
  bbox_x0: number | null;
  bbox_y0: number | null;
  bbox_x1: number | null;
  bbox_y1: number | null;
  seq: number;
  source: string;
}

export interface Anchor {
  id: number;
  page_no: number;
  bbox_x0: number;
  bbox_y0: number;
  bbox_x1: number;
  bbox_y1: number;
  char_start: number | null;
  char_end: number | null;
  quote_text: string | null;
  source: string;
  anchor_level: AnchorLevel;
  confidence: number | null;
}

export interface Evidence {
  id: number;
  evidence_type: "rule" | "llm";
  title: string | null;
  detail: string;
  raw_snippet: string | null;
  rule_id: number | null;
  need_review: boolean;
}

export interface RiskItem {
  id: number;
  title: string;
  risk_level: RiskLevel;
  category: string;
  reason: string;
  legal_basis: string | null;
  suggestion: string | null;
  suggestion_edited: string | null;
  adopted: boolean;
  merged_by: "rule" | "llm" | "both";
  is_global: boolean;
  unanchored: boolean;
  seq: number;
  clause_id: number | null;
  /** 命中条款原文，供"条款差异对比"展示原文侧 */
  clause_content: string | null;
  anchors: Anchor[];
  evidences: Evidence[];
}

export interface Annotation {
  id: number;
  contract_id: number;
  risk_item_id: number | null;
  author: string;
  author_role: string | null;
  content: string;
  created_at: string;
}

export interface TaskEvent {
  id: number;
  event_type: string;
  from_status: string | null;
  to_status: string | null;
  operator: string | null;
  detail: string | null;
  created_at: string;
}

export interface TaskProgress {
  task_id: number;
  status: TaskStatus;
  parsed_pages: number;
  total_pages: number | null;
  live_progress: string | null;
}

export interface ReportPreview {
  contract_id: number;
  markdown: string;
  char_count: number;
}

/** 批量操作中单条的结果。 */
export interface BatchItemResult {
  id: number;
  ok: boolean;
  detail: string | null;
}

/** 批量操作结果。`failed > 0` 时前端应展示 `results` 里的失败明细。 */
export interface BatchResult {
  total: number;
  succeeded: number;
  failed: number;
  results: BatchItemResult[];
}

/** 导出记录（`GET /api/contracts/{id}/report/exports`）。 */
export interface ExportRecordItem {
  id: number;
  format: string;
  object_key: string;
  file_size: number | null;
  created_by: string | null;
  created_at: string;
  download_url: string;
}

export interface ExportRecord {
  record_id: number;
  object_key: string;
  file_size: number;
  format: string;
  download_url: string;
}

export interface WritebackResult {
  status: WritebackStatus;
  log_id: number;
  comment_id: string | null;
  deduplicated: boolean;
  http_status: number | null;
  duration_ms: number | null;
  error_detail: string | null;
}

export interface WritebackStatusOut {
  writeback_status: WritebackStatus;
  latest_log_id: number | null;
  latest_status: string | null;
  error_detail: string | null;
  created_at: string | null;
}
