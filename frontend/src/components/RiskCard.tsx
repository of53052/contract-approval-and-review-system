/**
 * 风险卡片（架构 §14.1 图二右栏）。
 *
 * 展示：等级 / 标题 / 成因 / 法律依据 / AI 建议（可编辑）/ 依据链（双来源留痕）。
 * 交互：点击卡片 → 正文定位；采纳 / 编辑建议 → PATCH。
 */
import { useEffect, useRef, useState } from "react";
import {
  Button,
  Card,
  Collapse,
  Divider,
  Flex,
  Input,
  Space,
  Tag,
  Tooltip,
  Typography,
  App as AntApp,
} from "antd";
import { EditOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { updateRisk } from "../api";
import { apiError } from "../api/client";
import type { RiskItem } from "../types";
import { CATEGORY_LABEL, RISK_META } from "../constants";

interface Props {
  risk: RiskItem;
  active: boolean;
  flash: boolean;
  onLocate: (risk: RiskItem) => void;
}

const MERGED_BY_LABEL: Record<RiskItem["merged_by"], string> = {
  rule: "规则引擎",
  llm: "AI 研判",
  both: "规则 + AI 双确认",
};

export default function RiskCard({ risk, active, flash, onLocate }: Props) {
  const qc = useQueryClient();
  const { message } = AntApp.useApp();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(risk.suggestion_edited ?? risk.suggestion ?? "");
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!editing) setDraft(risk.suggestion_edited ?? risk.suggestion ?? "");
  }, [risk.suggestion, risk.suggestion_edited, editing]);

  // 被正文反向定位时滚进可视区
  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [active]);

  const saveMut = useMutation({
    mutationFn: (body: { suggestion_edited?: string | null; adopted?: boolean | null }) =>
      updateRisk(risk.id, body),
    onSuccess: () => {
      message.success("已保存");
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["risks"] });
      qc.invalidateQueries({ queryKey: ["report-preview"] });
    },
    onError: (e) => message.error(apiError(e)),
  });

  const meta = RISK_META[risk.risk_level];
  const anchor = risk.anchors[0];
  const firstEvidence = risk.evidences[0];
  // 展示优先级：法务编辑版 > AI 建议
  const shownSuggestion = risk.suggestion_edited ?? risk.suggestion;

  return (
    <div ref={ref}>
      <Card
        size="small"
        className={flash ? "risk-card-flash" : undefined}
        style={{
          borderLeft: `3px solid ${active ? "#1677ff" : "transparent"}`,
          background: active ? "#f0f7ff" : undefined,
          cursor: "pointer",
        }}
        onClick={() => onLocate(risk)}
        title={
          <Flex align="center" gap={6} wrap>
            <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
              {meta.badge} {meta.label}
            </Tag>
            <Typography.Text strong>{risk.title}</Typography.Text>
          </Flex>
        }
        extra={
          <Space size={4}>
            <Tooltip title={MERGED_BY_LABEL[risk.merged_by]}>
              <Tag color={risk.merged_by === "both" ? "blue" : "default"}>
                {risk.merged_by === "both" ? "双来源" : risk.merged_by === "rule" ? "规则" : "AI"}
              </Tag>
            </Tooltip>
            {risk.adopted && <Tag color="success">已采纳</Tag>}
          </Space>
        }
      >
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <Flex gap={6} wrap>
            <Tag>{CATEGORY_LABEL[risk.category] || risk.category}</Tag>
            {anchor ? (
              <Tag color="blue">
                第 {anchor.page_no} 页 · {anchor.anchor_level}
              </Tag>
            ) : (
              <Tooltip title="无法定位到原文，需人工核查">
                <Tag color="warning">未锚定</Tag>
              </Tooltip>
            )}
          </Flex>

          <Typography.Paragraph style={{ marginBottom: 0 }} type="secondary">
            {risk.reason}
          </Typography.Paragraph>

          {risk.legal_basis && (
            <Typography.Text style={{ fontSize: 12 }}>
              <Typography.Text type="secondary">依据：</Typography.Text>
              {risk.legal_basis}
            </Typography.Text>
          )}

          <Divider style={{ margin: "4px 0" }} />

          {editing ? (
            <Space direction="vertical" style={{ width: "100%" }} size={6}>
              <Input.TextArea
                rows={4}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onClick={(e) => e.stopPropagation()}
                placeholder="修改后的条款建议"
              />
              <Flex gap={6}>
                <Button
                  type="primary"
                  size="small"
                  loading={saveMut.isPending}
                  onClick={(e) => {
                    e.stopPropagation();
                    saveMut.mutate({ suggestion_edited: draft, adopted: true });
                  }}
                >
                  保存并采纳
                </Button>
                <Button size="small" onClick={(e) => { e.stopPropagation(); setEditing(false); }}>
                  取消
                </Button>
              </Flex>
            </Space>
          ) : (
            <Space direction="vertical" size={4} style={{ width: "100%" }}>
              <Typography.Text style={{ fontSize: 12 }} type="secondary">
                修改建议{risk.suggestion_edited ? "（法务已编辑）" : ""}
              </Typography.Text>
              <Typography.Paragraph style={{ marginBottom: 0 }}>
                {shownSuggestion || "（暂无建议）"}
              </Typography.Paragraph>
              <Flex gap={6}>
                <Button
                  size="small"
                  icon={<EditOutlined />}
                  onClick={(e) => { e.stopPropagation(); setEditing(true); }}
                >
                  编辑
                </Button>
                <Button
                  size="small"
                  type={risk.adopted ? "default" : "primary"}
                  loading={saveMut.isPending}
                  onClick={(e) => {
                    e.stopPropagation();
                    saveMut.mutate({ adopted: !risk.adopted });
                  }}
                >
                  {risk.adopted ? "取消采纳" : "采纳建议"}
                </Button>
                {risk.suggestion_edited && (
                  <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    onClick={(e) => {
                      e.stopPropagation();
                      saveMut.mutate({ suggestion_edited: "" });
                    }}
                  >
                    还原 AI 版
                  </Button>
                )}
              </Flex>
            </Space>
          )}

          {risk.evidences.length > 0 && (
            <div onClick={(e) => e.stopPropagation()}>
            <Collapse
              size="small"
              ghost
              items={[
                {
                  key: "ev",
                  label: `依据链（${risk.evidences.length} 条）`,
                  children: (
                    <Space direction="vertical" size={4} style={{ width: "100%" }}>
                      {risk.evidences.map((ev) => (
                        <div key={ev.id}>
                          <Tag color={ev.evidence_type === "rule" ? "geekblue" : "purple"}>
                            {ev.evidence_type === "rule" ? "规则" : "AI"}
                          </Tag>
                          {ev.need_review && <Tag color="warning">待人工复核</Tag>}
                          {ev.title && <Typography.Text strong>{ev.title}</Typography.Text>}
                          <Typography.Paragraph
                            style={{ marginBottom: 0, fontSize: 12 }}
                            type="secondary"
                          >
                            {ev.detail}
                          </Typography.Paragraph>
                        </div>
                      ))}
                    </Space>
                  ),
                },
              ]}
            />
            </div>
          )}

          {firstEvidence?.need_review && (
            <Typography.Text type="warning" style={{ fontSize: 12 }}>
              该依据未经知识库校验，请人工复核。
            </Typography.Text>
          )}
        </Space>
      </Card>
    </div>
  );
}
