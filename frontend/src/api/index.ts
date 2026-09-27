/** 后端接口封装。所有函数只负责发请求，不做业务判断。 */
import { api } from "./client";
import type {
  Annotation,
  Clause,
  ContractDetail,
  ContractListItem,
  Page,
  ExportRecord,
  ReportPreview,
  RiskItem,
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

export const listClauses = async (id: number): Promise<Clause[]> =>
  (await api.get(`/api/contracts/${id}/clauses`)).data;

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

export const exportReport = async (contractId: number): Promise<ExportRecord> =>
  (await api.post(`/api/contracts/${contractId}/report/export`)).data;

export const writeback = async (
  contractId: number,
  author: string,
): Promise<WritebackResult> =>
  (await api.post(`/api/contracts/${contractId}/writeback`, { author })).data;

export const writebackStatus = async (
  contractId: number,
): Promise<WritebackStatusOut> =>
  (await api.get(`/api/contracts/${contractId}/writeback/status`)).data;

/** 合同 PDF 的直链（供 PDF.js 与 <a download> 使用）。 */
export const pdfUrl = (contractId: number) => `/api/contracts/${contractId}/pdf`;
export const originalUrl = (contractId: number) =>
  `/api/contracts/${contractId}/file`;
