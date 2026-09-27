/** 后端接口封装。所有函数只负责发请求，不做业务判断。 */
import { api } from "./client";
import type {
  Annotation,
  BatchResult,
  BlacklistItem,
  Clause,
  ContractDetail,
  ContractListItem,
  ContractMetadataItem,
  Page,
  ExportFormat,
  ExportRecord,
  ExportRecordItem,
  ReportPreview,
  RiskItem,
  Rule,
  RuleIn,
  RuleOptions,
  RuleTemplate,
  RuleUpdateIn,
  StandardClause,
  StandardClauseIn,
  StandardClauseUpdateIn,
  TaskEvent,
  TaskProgress,
  WritebackResult,
  WritebackStatusOut,
} from "../types";

/** 合同列表。**返回分页信封**（后端 `Page[ContractListItem]`），不是裸数组。 */
export const listContracts = async (params: {
  status?: string;
  risk_level?: string;
  page?: number;
  page_size?: number;
}): Promise<Page<ContractListItem>> =>
  (await api.get("/api/contracts", { params })).data;

export const getContract = async (id: number): Promise<ContractDetail> =>
  (await api.get(`/api/contracts/${id}`)).data;

export const uploadContract = async (form: FormData): Promise<ContractDetail> =>
  (await api.post("/api/contracts/upload", form)).data;

export const deleteContract = async (id: number) =>
  (await api.delete(`/api/contracts/${id}`)).data;

/** 批量软删除。逐条返回结果，部分失败时前端展示失败明细。 */
export const batchDeleteContracts = async (ids: number[]): Promise<BatchResult> =>
  (await api.post("/api/contracts/batch/delete", { ids })).data;

/** 批量重试（按合同 ID）。非 blocked 的条目会在 results 里标失败。 */
export const batchRetryTasks = async (contractIds: number[]): Promise<BatchResult> =>
  (await api.post("/api/tasks/batch/retry", { ids: contractIds })).data;

export const listExportRecords = async (contractId: number): Promise<ExportRecordItem[]> =>
  (await api.get(`/api/contracts/${contractId}/report/exports`)).data;

export const listClauses = async (id: number): Promise<Clause[]> =>
  (await api.get(`/api/contracts/${id}/clauses`)).data;

export const listMetadata = async (id: number): Promise<ContractMetadataItem[]> =>
  (await api.get(`/api/contracts/${id}/metadata`)).data;

export const listEvents = async (id: number): Promise<TaskEvent[]> =>
  (await api.get(`/api/contracts/${id}/events`)).data;

export const listRisks = async (contractId: number): Promise<RiskItem[]> =>
  (await api.get("/api/risks", { params: { contract_id: contractId } })).data;

export const updateRisk = async (
  riskId: number,
  body: { suggestion_edited?: string | null; adopted?: boolean | null },
): Promise<RiskItem> => (await api.patch(`/api/risks/${riskId}`, body)).data;

export const createAnnotation = async (body: {
  author: string;
  content: string;
  author_role?: string | null;
  risk_item_id?: number | null;
}): Promise<Annotation> => (await api.post("/api/risks/annotations", body)).data;

export const listAnnotations = async (contractId: number): Promise<Annotation[]> =>
  (await api.get("/api/risks/annotations/list", { params: { contract_id: contractId } }))
    .data;

export const getTaskProgress = async (taskId: number): Promise<TaskProgress> =>
  (await api.get(`/api/tasks/${taskId}/progress`)).data;

export const retryTask = async (taskId: number): Promise<unknown> =>
  (await api.post(`/api/tasks/${taskId}/retry`)).data;

export const syncTodos = async (): Promise<unknown[]> =>
  (await api.post("/api/tasks/sync-todos", null, { params: { auto_review: true } })).data;

export const previewReport = async (contractId: number): Promise<ReportPreview> =>
  (await api.get(`/api/contracts/${contractId}/report/preview`)).data;

export const exportReport = async (
  contractId: number,
  format: ExportFormat = "markdown",
): Promise<ExportRecord> =>
  (await api.post(`/api/contracts/${contractId}/report/export`, null, {
    params: { format },
  })).data;

export const writeback = async (
  contractId: number,
  author: string,
): Promise<WritebackResult> =>
  (await api.post(`/api/contracts/${contractId}/writeback`, { author })).data;

export const writebackStatus = async (
  contractId: number,
): Promise<WritebackStatusOut> =>
  (await api.get(`/api/contracts/${contractId}/writeback/status`)).data;

// ==================== 规则配置（PRD 2.4.3 / 2.4.5）====================

export const listRuleOptions = async (): Promise<RuleOptions> =>
  (await api.get("/api/rules/options")).data;

export const listRuleTemplates = async (): Promise<RuleTemplate[]> =>
  (await api.get("/api/rules/templates")).data;

export const updateRuleTemplate = async (
  templateId: number,
  body: { name?: string; description?: string; enabled?: boolean },
): Promise<RuleTemplate> =>
  (await api.patch(`/api/rules/templates/${templateId}`, body)).data;

export const createRule = async (body: RuleIn): Promise<Rule> =>
  (await api.post("/api/rules/rules", body)).data;

export const updateRule = async (ruleId: number, body: RuleUpdateIn): Promise<Rule> =>
  (await api.patch(`/api/rules/rules/${ruleId}`, body)).data;

/** 删除规则。被历史依据引用时后端返回 409，需 `force` 才强删。 */
export const deleteRule = async (ruleId: number, force = false): Promise<unknown> =>
  (await api.delete(`/api/rules/rules/${ruleId}`, { params: { force } })).data;

export const listStandardClauses = async (): Promise<StandardClause[]> =>
  (await api.get("/api/rules/standard-clauses")).data;

export const createStandardClause = async (
  body: StandardClauseIn,
): Promise<StandardClause> => (await api.post("/api/rules/standard-clauses", body)).data;

export const updateStandardClause = async (
  clauseId: number,
  body: StandardClauseUpdateIn,
): Promise<StandardClause> =>
  (await api.patch(`/api/rules/standard-clauses/${clauseId}`, body)).data;

export const deleteStandardClause = async (clauseId: number): Promise<unknown> =>
  (await api.delete(`/api/rules/standard-clauses/${clauseId}`)).data;

export const listBlacklist = async (): Promise<BlacklistItem[]> =>
  (await api.get("/api/rules/blacklist")).data;

/** 合同 PDF 的直链（供 PDF.js 与 <a download> 使用）。 */
export const pdfUrl = (contractId: number) => `/api/contracts/${contractId}/pdf`;
export const originalUrl = (contractId: number) =>
  `/api/contracts/${contractId}/file`;
