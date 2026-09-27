/** 风险等级 / 状态 / 业务类型的展示映射。集中一处，避免各页面写法漂移。 */
import type { RiskLevel, TaskStatus, WritebackStatus, ReviewConclusion } from "./types";

export const RISK_META: Record<
  RiskLevel,
  { label: string; color: string; badge: string }
> = {
  high: { label: "高风险", color: "red", badge: "🔴" },
  medium: { label: "中风险", color: "orange", badge: "🟠" },
  low: { label: "低风险", color: "green", badge: "🟢" },
};

export const STATUS_META: Record<TaskStatus, { label: string; color: string }> = {
  pending: { label: "待处理", color: "default" },
  parsing: { label: "解析中", color: "processing" },
  reviewing: { label: "审查中", color: "processing" },
  completed: { label: "已完成", color: "success" },
  blocked: { label: "阻塞", color: "error" },
  failed: { label: "失败", color: "error" },
};

export const WRITEBACK_META: Record<
  WritebackStatus,
  { label: string; color: string }
> = {
  not_written: { label: "未回写", color: "default" },
  writing: { label: "回写中", color: "processing" },
  success: { label: "已回写", color: "success" },
  failed: { label: "回写失败", color: "error" },
};

export const CONCLUSION_META: Record<
  ReviewConclusion,
  { label: string; color: string }
> = {
  pass: { label: "通过", color: "success" },
  rectify: { label: "整改后复审", color: "warning" },
  reject: { label: "建议驳回", color: "error" },
};

export const BUSINESS_TYPE_LABEL: Record<string, string> = {
  purchase: "采购合同",
  sales: "销售合同",
  service: "服务合同",
  labor: "劳动合同",
};

export const CATEGORY_LABEL: Record<string, string> = {
  subject_qualification: "主体资质",
  amount_payment: "金额与付款",
  acceptance: "验收",
  liability: "违约责任",
  intellectual_property: "知识产权",
  confidentiality: "保密",
  jurisdiction: "争议管辖",
  force_majeure: "不可抗力",
  data_security: "数据安全",
  other: "其他",
};

/** 元数据键的中文标签（与后端 report_service._META_LABEL 保持一致）。 */
export const METADATA_LABEL: Record<string, string> = {
  contract_no: "合同编号",
  party_a_name: "甲方",
  party_b_name: "乙方",
  party_a_credit_code: "甲方统一社会信用代码",
  party_b_credit_code: "乙方统一社会信用代码",
  amount: "合同金额",
  currency: "币种",
  term: "履约期限",
  effective_condition: "生效条件",
  sign_date: "签订日期",
  sign_place: "签订地点",
};

/** 后端金额是 Decimal 序列化后的字符串，转成带千分位的展示。 */
export function formatAmount(amount: string | null, currency: string | null): string {
  if (!amount) return "—";
  const n = Number(amount);
  if (Number.isNaN(n)) return amount;
  const symbol = currency === "CNY" || !currency ? "¥" : `${currency} `;
  return symbol + n.toLocaleString("zh-CN", { minimumFractionDigits: 2 });
}

export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
}
