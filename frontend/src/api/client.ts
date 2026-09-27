/**
 * axios 实例与统一错误提取。
 *
 * `baseURL` 默认留空走 Vite 代理的相对路径；部署到同域时也无需改动。
 */
import axios, { AxiosError } from "axios";

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
  timeout: 120_000, // 解析 + WPS 转换可能耗时数十秒
});

/** 把后端 `{detail: "..."}` 或网络错误转成可展示的中文消息。 */
export function apiError(e: unknown): string {
  const err = e as AxiosError<{ detail?: unknown }>;
  const detail = err?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI 参数校验错误是数组
    return detail
      .map((d) => (d as { msg?: string }).msg ?? JSON.stringify(d))
      .join("; ");
  }
  if (err?.response) return `HTTP ${err.response.status}`;
  if (err?.code === "ECONNABORTED") return "请求超时";
  return err?.message || "未知错误";
}
