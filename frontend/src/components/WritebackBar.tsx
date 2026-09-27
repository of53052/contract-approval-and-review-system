/**
 * 底部协同回写栏（架构 §11.4 B1~B4）。
 *
 * 职责：展示综合结论 → 填写法务批注 → 一键回写审批系统 → 导出报告。
 * 回写是**幂等**的：后端按内容 hash 生成 idempotency_key，重复点击复用同一记录。
 */
import { useState } from "react";
import {
  Alert,
  Button,
  Divider,
  Flex,
  Input,
  Modal,
  Space,
  Tag,
  Typography,
  App as AntApp,
} from "antd";
import { ExportOutlined, SendOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createAnnotation,
  exportReport,
  previewReport,
  writeback,
  writebackStatus,
} from "../api";
import { apiError } from "../api/client";
import type { ContractDetail } from "../types";
import { CONCLUSION_META, RISK_META, WRITEBACK_META } from "../constants";

interface Props {
  contract: ContractDetail;
  /** 当前选中的风险项（批注会挂在它下面） */
  selectedRiskId: number | null;
  onWrittenBack: () => void;
}

export default function WritebackBar({ contract, selectedRiskId, onWrittenBack }: Props) {
  const qc = useQueryClient();
  const { message } = AntApp.useApp();
  const [comment, setComment] = useState("");
  const [author, setAuthor] = useState("法务审查人");

  const { data: wbStatus } = useQuery({
    queryKey: ["writeback-status", contract.id],
    queryFn: () => writebackStatus(contract.id),
    refetchInterval: (q) =>
      q.state.data?.writeback_status === "writing" ? 2000 : false,
  });

  const commentMut = useMutation({
    mutationFn: () =>
      createAnnotation({
        author,
        author_role: "法务审查人",
        content: comment,
        risk_item_id: selectedRiskId,
      }),
    onSuccess: () => {
      message.success("批注已保存");
      setComment("");
      qc.invalidateQueries({ queryKey: ["annotations", contract.id] });
      qc.invalidateQueries({ queryKey: ["report-preview", contract.id] });
    },
    onError: (e) => message.error(apiError(e)),
  });

  const writebackMut = useMutation({
    mutationFn: () => writeback(contract.id, author),
    onSuccess: (r) => {
      if (r.deduplicated) {
        message.info("该内容已回写过，复用原记录（幂等）");
      } else if (r.status === "success") {
        message.success(`已写回审批系统（评论 ${r.comment_id}，${r.duration_ms}ms）`);
      } else {
        message.error(`回写失败：${r.error_detail ?? "未知原因"}`);
      }
      qc.invalidateQueries({ queryKey: ["writeback-status", contract.id] });
      qc.invalidateQueries({ queryKey: ["contracts"] });
      onWrittenBack();
    },
    onError: (e) => message.error(apiError(e)),
  });

  const previewMut = useMutation({
    mutationFn: () => previewReport(contract.id),
    onSuccess: (r) => {
      Modal.info({
        title: `审查报告预览（${r.char_count} 字符）`,
        width: 760,
        content: (
          <pre
            style={{
              maxHeight: 460,
              overflow: "auto",
              background: "#fafafa",
              padding: 12,
              fontSize: 12,
              whiteSpace: "pre-wrap",
            }}
          >
            {r.markdown}
          </pre>
        ),
      });
    },
    onError: (e) => message.error(apiError(e)),
  });

  const exportMut = useMutation({
    mutationFn: () => exportReport(contract.id),
    onSuccess: (r) => {
      message.success(`报告已导出（${r.file_size} 字节）`);
      window.open(r.download_url, "_blank");
    },
    onError: (e) => message.error(apiError(e)),
  });

  const riskMeta = contract.overall_risk ? RISK_META[contract.overall_risk] : null;
  const conclMeta = contract.conclusion ? CONCLUSION_META[contract.conclusion] : null;
  const wbMeta = WRITEBACK_META[wbStatus?.writeback_status ?? contract.writeback_status];

  return (
    <div style={{ padding: 12, background: "#fff", borderTop: "1px solid #f0f0f0" }}>
      <Flex justify="space-between" align="flex-start" gap={16} wrap>
        <Space direction="vertical" size={4} style={{ minWidth: 260 }}>
          <Space wrap>
            <Typography.Text strong>综合风险：</Typography.Text>
            {riskMeta ? (
              <Tag color={riskMeta.color}>
                {riskMeta.badge} {riskMeta.label}
              </Tag>
            ) : (
              <Typography.Text type="secondary">审查未完成</Typography.Text>
            )}
            {conclMeta && <Tag color={conclMeta.color}>{conclMeta.label}</Tag>}
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {contract.summary || "（暂无摘要）"}
          </Typography.Text>
          <Space size={4}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              回写状态：
            </Typography.Text>
            <Tag color={wbMeta.color}>{wbMeta.label}</Tag>
            {wbStatus?.error_detail && (
              <Typography.Text type="danger" style={{ fontSize: 12 }}>
                {wbStatus.error_detail}
              </Typography.Text>
            )}
          </Space>
        </Space>

        <Space direction="vertical" size={6} style={{ flex: 1, minWidth: 320 }}>
          <Space>
            <Input
              size="small"
              style={{ width: 120 }}
              value={author}
              onChange={(e) => setAuthor(e.target.value)}
              placeholder="批注人"
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {selectedRiskId ? "批注将关联到选中风险项" : "未选中风险项，无法提交批注"}
            </Typography.Text>
          </Space>
          <Input.TextArea
            rows={2}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="填写法务批注意见（会写入报告与回写内容）"
          />
        </Space>

        <Space direction="vertical" size={6}>
          <Button
            onClick={() => commentMut.mutate()}
            loading={commentMut.isPending}
            disabled={!comment.trim() || !selectedRiskId}
          >
            保存批注
          </Button>
          <Button
            type="primary"
            icon={<SendOutlined />}
            loading={writebackMut.isPending}
            disabled={contract.status !== "completed"}
            onClick={() => writebackMut.mutate()}
          >
            写回审批意见
          </Button>
          <Space size={4}>
            <Button onClick={() => previewMut.mutate()} loading={previewMut.isPending}>
              预览报告
            </Button>
            <Button
              icon={<ExportOutlined />}
              onClick={() => exportMut.mutate()}
              loading={exportMut.isPending}
              disabled={contract.status !== "completed"}
            >
              导出
            </Button>
          </Space>
        </Space>
      </Flex>

      {wbStatus?.writeback_status === "failed" && (
        <>
          <Divider style={{ margin: "8px 0" }} />
          <Alert
            type="error"
            showIcon
            message="上次回写失败，可重试"
            description={wbStatus.error_detail}
          />
        </>
      )}
    </div>
  );
}
