/**
 * 审查大盘页（架构 §14.1 图一）。
 *
 * 数据源：`GET /api/contracts`（列表页字段由后端一次聚合，前端不做 N+1）。
 * 审查中的任务靠 React Query 轮询刷新，无需手动刷新按钮。
 */
import { useState } from "react";
import {
  Button,
  Card,
  Empty,
  Flex,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  App as AntApp,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { DownloadOutlined, ReloadOutlined, UploadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import dayjs from "dayjs";
import {
  deleteContract,
  listContracts,
  originalUrl,
  syncTodos,
  uploadContract,
} from "../api";
import { apiError } from "../api/client";
import type { ContractListItem } from "../types";
import {
  BUSINESS_TYPE_LABEL,
  CONCLUSION_META,
  RISK_META,
  STATUS_META,
  WRITEBACK_META,
  formatAmount,
} from "../constants";

export default function Dashboard() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { message, modal } = AntApp.useApp();
  const [status, setStatus] = useState<string | undefined>();
  const [riskLevel, setRiskLevel] = useState<string | undefined>();
  const [keyword, setKeyword] = useState("");

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["contracts", status, riskLevel],
    queryFn: () => listContracts({ status, risk_level: riskLevel }),
    // 有任务在解析/审查中时每 3 秒轮询，全部完成则停止轮询
    refetchInterval: (q) => {
      const rows = q.state.data?.items;
      const busy = rows?.some((r) => r.status === "parsing" || r.status === "reviewing");
      return busy ? 3000 : false;
    },
  });

  const uploadMut = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      form.append("business_type", "purchase");
      form.append("auto_review", "true");
      return uploadContract(form);
    },
    onSuccess: (c) => {
      message.success(`已上传并开始审查：${c.title}`);
      qc.invalidateQueries({ queryKey: ["contracts"] });
    },
    onError: (e) => message.error(apiError(e)),
  });

  const syncMut = useMutation({
    mutationFn: syncTodos,
    onSuccess: (rows) => {
      message.success(
        rows.length ? `同步到 ${rows.length} 份新合同` : "没有新的待办（或已全部同步）",
      );
      qc.invalidateQueries({ queryKey: ["contracts"] });
    },
    onError: (e) => message.error(apiError(e)),
  });

  const onDelete = (row: ContractListItem) => {
    modal.confirm({
      title: "确认删除该合同？",
      content: `「${row.title}」将被软删除，列表不再显示（数据保留在库中）。`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await deleteContract(row.id);
          message.success("已删除");
          qc.invalidateQueries({ queryKey: ["contracts"] });
        } catch (e) {
          message.error(apiError(e));
        }
      },
    });
  };

  const columns: ColumnsType<ContractListItem> = [
    {
      title: "合同名称",
      dataIndex: "title",
      key: "title",
      ellipsis: true,
      render: (title: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Link onClick={() => navigate(`/contracts/${row.id}`)}>
            {title}
          </Typography.Link>
          {row.contract_no && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {row.contract_no}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: "申请人",
      dataIndex: "applicant",
      key: "applicant",
      width: 110,
      render: (v: string | null, row) => (
        <Space direction="vertical" size={0}>
          <span>{v || "—"}</span>
          {row.applicant_dept && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {row.applicant_dept}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: "业务类型",
      dataIndex: "business_type",
      key: "business_type",
      width: 100,
      render: (v: string) => BUSINESS_TYPE_LABEL[v] || v,
    },
    {
      title: "金额",
      dataIndex: "amount",
      key: "amount",
      width: 150,
      align: "right",
      render: (_: unknown, row) => formatAmount(row.amount, row.currency),
    },
    {
      title: "审查状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (s: ContractListItem["status"], row) => {
        const meta = STATUS_META[s] ?? { label: s, color: "default" };
        const tip =
          s === "blocked"
            ? row.blocked_reason || "解析或审查受阻，可在工作台重试"
            : s === "parsing" || s === "reviewing"
              ? `已解析 ${row.parsed_pages}/${row.total_pages ?? "?"} 页`
              : undefined;
        const tag = <Tag color={meta.color}>{meta.label}</Tag>;
        return tip ? <Tooltip title={tip}>{tag}</Tooltip> : tag;
      },
    },
    {
      title: "风险",
      key: "risk",
      width: 170,
      render: (_: unknown, row) => {
        if (!row.overall_risk) return <Typography.Text type="secondary">—</Typography.Text>;
        const meta = RISK_META[row.overall_risk];
        return (
          <Space size={4} wrap>
            <Tag color={meta.color}>
              {meta.badge} {meta.label}
            </Tag>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              高 {row.high_risk_count} / 中 {row.medium_risk_count} / 低 {row.low_risk_count}
            </Typography.Text>
          </Space>
        );
      },
    },
    {
      title: "结论",
      dataIndex: "conclusion",
      key: "conclusion",
      width: 120,
      render: (c: ContractListItem["conclusion"]) => {
        if (!c) return <Typography.Text type="secondary">—</Typography.Text>;
        const meta = CONCLUSION_META[c];
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "回写",
      dataIndex: "writeback_status",
      key: "writeback_status",
      width: 100,
      render: (s: ContractListItem["writeback_status"]) => {
        const meta = WRITEBACK_META[s] ?? { label: s, color: "default" };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 140,
      render: (v: string) => dayjs(v).format("MM-DD HH:mm"),
    },
    {
      title: "操作",
      key: "action",
      width: 150,
      fixed: "right",
      render: (_: unknown, row) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={() => navigate(`/contracts/${row.id}`)}>
            工作台
          </Button>
          <Tooltip title="下载原件">
            <Button
              type="link"
              size="small"
              icon={<DownloadOutlined />}
              href={originalUrl(row.id)}
              target="_blank"
            />
          </Tooltip>
          <Button type="link" size="small" danger onClick={() => onDelete(row)}>
            删除
          </Button>
        </Space>
      ),
    },
  ];

  const rows = (data?.items ?? []).filter(
    (r) => !keyword || r.title.includes(keyword) || (r.contract_no ?? "").includes(keyword),
  );

  return (
    <div style={{ padding: 24 }}>
      <Card
        title="合同审查大盘"
        extra={
          <Space>
            <Input.Search
              allowClear
              placeholder="搜索合同名称 / 编号"
              style={{ width: 220 }}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
            <Select
              allowClear
              placeholder="审查状态"
              style={{ width: 130 }}
              value={status}
              onChange={setStatus}
              options={Object.entries(STATUS_META).map(([value, m]) => ({
                value,
                label: m.label,
              }))}
            />
            <Select
              allowClear
              placeholder="风险等级"
              style={{ width: 120 }}
              value={riskLevel}
              onChange={setRiskLevel}
              options={Object.entries(RISK_META).map(([value, m]) => ({
                value,
                label: m.label,
              }))}
            />
            <Button icon={<ReloadOutlined />} loading={isFetching} onClick={() => refetch()}>
              刷新
            </Button>
            <Button
              loading={syncMut.isPending}
              onClick={() => syncMut.mutate()}
            >
              同步审批待办
            </Button>
            <Upload
              accept=".docx,.pdf"
              showUploadList={false}
              beforeUpload={(file) => {
                uploadMut.mutate(file);
                return false; // 阻止 antd 默认上传，走我们自己的 mutation
              }}
            >
              <Button type="primary" icon={<UploadOutlined />} loading={uploadMut.isPending}>
                上传合同
              </Button>
            </Upload>
          </Space>
        }
      >
        <Table<ContractListItem>
          rowKey="id"
          loading={isLoading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 1280 }}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          locale={{
            emptyText: (
              <Empty
                description="还没有合同。点右上角「同步审批待办」或「上传合同」开始。"
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              />
            ),
          }}
          summary={(page) =>
            page.length ? (
              <Table.Summary.Row>
                <Table.Summary.Cell index={0} colSpan={3}>
                  <Flex justify="flex-end">
                    <Typography.Text type="secondary">本页合计</Typography.Text>
                  </Flex>
                </Table.Summary.Cell>
                <Table.Summary.Cell index={3} align="right">
                  <Typography.Text strong>
                    {formatAmount(
                      String(
                        page.reduce((s, r) => s + (r.amount ? Number(r.amount) : 0), 0),
                      ),
                      "CNY",
                    )}
                  </Typography.Text>
                </Table.Summary.Cell>
                <Table.Summary.Cell index={4} colSpan={6} />
              </Table.Summary.Row>
            ) : null
          }
        />
      </Card>
    </div>
  );
}
