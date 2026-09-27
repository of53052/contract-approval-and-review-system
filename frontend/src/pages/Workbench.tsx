/**
 * 智能审查工作台（架构 §11.4 / §14.1 图二）。
 *
 * 布局：左栏合同正文（PDF.js）| 右栏审查结果（风险卡片）
 *      底部：协同回写栏
 *
 * 双向锚定：
 * - 点风险卡片 → `focusAnchor` 更新 → PdfViewer 跳页 + 滚动 + 高亮
 * - 点正文高亮 → `onPickRisk` → 右栏卡片滚动到可视区并高亮
 */
import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Flex,
  Segmented,
  Space,
  Spin,
  Tag,
  Typography,
  App as AntApp,
} from "antd";
import { ArrowLeftOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import {
  getContract,
  getTaskProgress,
  listClauses,
  listEvents,
  listMetadata,
  listRisks,
  originalUrl,
  retryTask,
} from "../api";
import { apiError } from "../api/client";
import type { RiskItem } from "../types";
import PdfViewer from "../components/PdfViewer";
import RiskCard from "../components/RiskCard";
import WritebackBar from "../components/WritebackBar";
import { BUSINESS_TYPE_LABEL, RISK_META, formatBytes, formatAmount } from "../constants";

export default function Workbench() {
  const { id } = useParams<{ id: string }>();
  const contractId = Number(id);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { message } = AntApp.useApp();

  const [activeRiskId, setActiveRiskId] = useState<number | null>(null);
  const [flashRiskId, setFlashRiskId] = useState<number | null>(null);
  const [levelFilter, setLevelFilter] = useState<string>("all");

  const contractQ = useQuery({
    queryKey: ["contract", contractId],
    queryFn: () => getContract(contractId),
    enabled: Number.isFinite(contractId),
  });

  const taskId = contractQ.data?.task_id ?? null;
  const status = contractQ.data?.status;

  // 审查未完成时轮询进度
  const progressQ = useQuery({
    queryKey: ["task-progress", taskId],
    queryFn: () => getTaskProgress(taskId!),
    enabled: !!taskId && (status === "pending" || status === "parsing" || status === "reviewing"),
    refetchInterval: 2000,
  });

  // 审查完成（或状态变化）后刷新合同详情
  useEffect(() => {
    if (progressQ.data && progressQ.data.status !== status) {
      qc.invalidateQueries({ queryKey: ["contract", contractId] });
    }
  }, [progressQ.data, status, contractId, qc]);

  const risksQ = useQuery({
    queryKey: ["risks", contractId],
    queryFn: () => listRisks(contractId),
    enabled: Number.isFinite(contractId),
  });

  const clausesQ = useQuery({
    queryKey: ["clauses", contractId],
    queryFn: () => listClauses(contractId),
    enabled: Number.isFinite(contractId),
  });

  const metadataQ = useQuery({
    queryKey: ["metadata", contractId],
    queryFn: () => listMetadata(contractId),
    enabled: Number.isFinite(contractId),
  });

  const eventsQ = useQuery({
    queryKey: ["events", contractId],
    queryFn: () => listEvents(contractId),
    enabled: Number.isFinite(contractId),
  });

  const retryMut = useMutation({
    mutationFn: () => retryTask(taskId!),
    onSuccess: () => {
      message.success("已提交重试，正在重新解析");
      qc.invalidateQueries({ queryKey: ["contract", contractId] });
      qc.invalidateQueries({ queryKey: ["task-progress", taskId] });
    },
    onError: (e) => message.error(apiError(e)),
  });

  const risks = risksQ.data ?? [];
  const filtered = useMemo(
    () => (levelFilter === "all" ? risks : risks.filter((r) => r.risk_level === levelFilter)),
    [risks, levelFilter],
  );

  /** 右侧卡片 → 正文 */
  const locateFromCard = (risk: RiskItem) => {
    setActiveRiskId(risk.id);
    // 无锚点的风险项只高亮卡片，不跳正文
  };

  /** 正文 → 右侧卡片 */
  const pickFromPdf = (riskId: number) => {
    setActiveRiskId(riskId);
    setFlashRiskId(riskId);
    window.setTimeout(() => setFlashRiskId(null), 1200);
  };

  // 计算传给 PdfViewer 的聚焦锚点
  const focusAnchor = useMemo(() => {
    if (activeRiskId == null) return null;
    const risk = risks.find((r) => r.id === activeRiskId);
    const a = risk?.anchors[0];
    if (!risk || !a) return null;
    return {
      riskId: risk.id,
      pageNo: a.page_no,
      bbox: [a.bbox_x0, a.bbox_y0, a.bbox_x1, a.bbox_y1],
    };
  }, [activeRiskId, risks]);

  if (contractQ.isLoading) {
    return (
      <Flex justify="center" align="center" style={{ height: "calc(100vh - 64px)" }}>
        <Flex align="center" gap={8}>
          <Spin />
          <Typography.Text type="secondary">加载合同…</Typography.Text>
        </Flex>
      </Flex>
    );
  }

  if (contractQ.isError || !contractQ.data) {
    return (
      <div style={{ padding: 24 }}>
        <Alert
          type="error"
          showIcon
          message="合同加载失败"
          description={apiError(contractQ.error)}
          action={<Button onClick={() => navigate("/")}>返回大盘</Button>}
        />
      </div>
    );
  }

  const c = contractQ.data;
  const busy = c.status === "pending" || c.status === "parsing" || c.status === "reviewing";
  const live = progressQ.data?.live_progress;

  return (
    <Flex vertical style={{ height: "calc(100vh - 64px)" }}>
      {/* 顶部信息条 */}
      <Card size="small" style={{ borderRadius: 0 }}>
        <Flex justify="space-between" align="center" gap={16} wrap>
          <Space size={12} wrap>
            <Button
              type="text"
              size="small"
              icon={<ArrowLeftOutlined />}
              onClick={() => navigate("/")}
            >
              返回
            </Button>
            <Typography.Text strong>{c.title}</Typography.Text>
            <Tag>{BUSINESS_TYPE_LABEL[c.business_type] || c.business_type}</Tag>
            <Typography.Text type="secondary">
              {formatAmount(c.amount, c.currency)}
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {c.file_name} · {formatBytes(c.file_size)}
            </Typography.Text>
            {c.external_id && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                审批单 {c.external_id}
              </Typography.Text>
            )}
          </Space>
          <Space>
            {busy && (
              <Tag color="processing">
                {c.status === "parsing" ? "解析中" : "审查中"}
                {live ? ` ${live}` : ` ${c.parsed_pages}/${c.total_pages ?? "?"}`}
              </Tag>
            )}
            <Button
              size="small"
              href={originalUrl(contractId)}
              target="_blank"
            >
              下载原件
            </Button>
          </Space>
        </Flex>
      </Card>

      {c.status === "blocked" && (
        <Alert
          type="error"
          showIcon
          style={{ borderRadius: 0 }}
          message={`审查受阻：${c.blocked_reason ?? "未知原因"}`}
          description={
            <Flex gap={8} align="center">
              <Typography.Text>{c.summary || "可在修复后重试。"}</Typography.Text>
              <Button
                size="small"
                type="primary"
                icon={<ReloadOutlined />}
                loading={retryMut.isPending}
                onClick={() => retryMut.mutate()}
              >
                重试
              </Button>
            </Flex>
          }
        />
      )}

      {/* 双栏主体 */}
      <Flex style={{ flex: 1, minHeight: 0 }}>
        <div style={{ flex: "1 1 58%", minWidth: 0, padding: 8 }}>
          <PdfViewer
            contractId={contractId}
            risks={risks}
            metadata={metadataQ.data}
            focusAnchor={focusAnchor}
            onPickRisk={pickFromPdf}
          />
        </div>

        <div
          style={{
            flex: "1 1 42%",
            minWidth: 360,
            overflow: "auto",
            padding: 8,
            borderLeft: "1px solid #f0f0f0",
            background: "#fafafa",
          }}
        >
          <Flex justify="space-between" align="center" style={{ marginBottom: 8 }}>
            <Segmented
              size="small"
              value={levelFilter}
              onChange={(v) => setLevelFilter(String(v))}
              options={[
                { label: `全部 ${risks.length}`, value: "all" },
                {
                  label: `高 ${risks.filter((r) => r.risk_level === "high").length}`,
                  value: "high",
                },
                {
                  label: `中 ${risks.filter((r) => r.risk_level === "medium").length}`,
                  value: "medium",
                },
                {
                  label: `低 ${risks.filter((r) => r.risk_level === "low").length}`,
                  value: "low",
                },
              ]}
            />
            {clausesQ.data && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {clausesQ.data.length} 个条款
              </Typography.Text>
            )}
          </Flex>

          {risksQ.isLoading ? (
            <Flex justify="center" style={{ paddingTop: 60 }}>
              <Spin />
            </Flex>
          ) : filtered.length === 0 ? (
            <Empty
              description={
                busy ? "审查进行中，稍候刷新…" : "没有匹配的风险项"
              }
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              style={{ paddingTop: 60 }}
            />
          ) : (
            <Flex vertical gap={8}>
              {filtered.map((r) => (
                <RiskCard
                  key={r.id}
                  risk={r}
                  active={r.id === activeRiskId}
                  flash={r.id === flashRiskId}
                  onLocate={locateFromCard}
                />
              ))}
            </Flex>
          )}

          {/* 审计轨迹：解释"任务为什么变成 blocked" */}
          {eventsQ.data && eventsQ.data.length > 0 && (
            <Card size="small" title="审查轨迹" style={{ marginTop: 12 }}>
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                {eventsQ.data.map((e) => (
                  <Typography.Text key={e.id} style={{ fontSize: 12 }} type="secondary">
                    [{e.event_type}] {e.from_status ?? "-"} → {e.to_status ?? "-"}
                    {e.detail ? ` · ${e.detail}` : ""}
                  </Typography.Text>
                ))}
              </Space>
            </Card>
          )}

          {c.overall_risk && (
            <div style={{ marginTop: 12 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                综合等级：{RISK_META[c.overall_risk].label}
              </Typography.Text>
            </div>
          )}
        </div>
      </Flex>

      <WritebackBar
        contract={c}
        selectedRiskId={activeRiskId}
        onWrittenBack={() => {
          qc.invalidateQueries({ queryKey: ["contract", contractId] });
          qc.invalidateQueries({ queryKey: ["contracts"] });
        }}
      />
    </Flex>
  );
}
