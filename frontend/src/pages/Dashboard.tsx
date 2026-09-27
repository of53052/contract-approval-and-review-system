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
  Modal,
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
import {
  DownloadOutlined,
  FileTextOutlined,
  ReloadOutlined,
  SyncOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import dayjs from "dayjs";
import {
  batchDeleteContracts,
  batchRetryTasks,
  deleteContract,
  listContracts,
  listExportRecords,
  originalUrl,
  retryTask,
  syncTodos,
  uploadContract,
} from "../api";
import { apiError } from "../api/client";
import type { BatchResult, ContractListItem } from "../types";
import {
  BUSINESS_TYPE_LABEL,
  CONCLUSION_META,
  RISK_META,
  STATUS_META,
  WRITEBACK_META,
  formatAmount,
  formatBytes,
} from "../constants";

export default function Dashboard() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { message, modal } = AntApp.useApp();
  const [status, setStatus] = useState<string | undefined>();
  const [riskLevel, setRiskLevel] = useState<string | undefined>();
  const [keyword, setKeyword] = useState("");
  /** 勾选的行（批量操作的唯一输入源，不做"按筛选条件批量"）。 */
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  /** 打开导出记录弹窗的合同；null 表示关闭。 */
  const [exportFor, setExportFor] = useState<ContractListItem | null>(null);

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

  /**
   * 展示批量操作结果。
   *
   * 部分失败必须逐条列出来——用户勾了 N 条，只知道"失败 2 条"是没法处理的。
   * 全部成功时只弹一条轻提示，不打扰。
   */
  const showBatchResult = (res: BatchResult, action: string) => {
    if (res.failed === 0) {
      message.success(`${action}完成：${res.succeeded} 条`);
      return;
    }
    const failedRows = res.results.filter((r) => !r.ok);
    modal.info({
      title: `${action}完成：成功 ${res.succeeded} / 失败 ${res.failed}`,
      width: 480,
      content: (
        <div style={{ maxHeight: 260, overflow: "auto" }}>
          {failedRows.map((r) => (
            <div key={r.id} style={{ marginTop: 6 }}>
              <Typography.Text type="danger">#{r.id}</Typography.Text>{" "}
              <Typography.Text>{r.detail || "失败"}</Typography.Text>
            </div>
          ))}
        </div>
      ),
    });
  };

  const batchDeleteMut = useMutation({
    mutationFn: batchDeleteContracts,
    onSuccess: (res) => {
      showBatchResult(res, "批量删除");
      setSelectedIds([]);
      qc.invalidateQueries({ queryKey: ["contracts"] });
    },
    onError: (e) => message.error(apiError(e)),
  });

  const batchRetryMut = useMutation({
    mutationFn: batchRetryTasks,
    onSuccess: (res) => {
      showBatchResult(res, "批量重试");
      setSelectedIds([]);
      qc.invalidateQueries({ queryKey: ["contracts"] });
    },
    onError: (e) => message.error(apiError(e)),
  });

  /** 行内重试（PRD 2.4.7：管理员在列表页直接触发"重新审查"）。 */
  const retryMut = useMutation({
    mutationFn: (taskId: number) => retryTask(taskId),
    onSuccess: () => {
      message.success("已重新提交审查");
      qc.invalidateQueries({ queryKey: ["contracts"] });
    },
    onError: (e) => message.error(apiError(e)),
  });

  // 导出记录按需拉取：只有打开弹窗时才请求（enabled 由 exportFor 控制）
  const exportsQuery = useQuery({
    queryKey: ["exports", exportFor?.id],
    queryFn: () => listExportRecords(exportFor!.id),
    enabled: !!exportFor,
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
      width: 220,
      fixed: "right",
      render: (_: unknown, row) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={() => navigate(`/contracts/${row.id}`)}>
            工作台
          </Button>
          {/* PRD 2.4.7：阻塞任务在列表页即可一键重新审查，不必先进工作台 */}
          {row.status === "blocked" && row.task_id != null && (
            <Tooltip title={row.blocked_reason || "重新走一遍解析与审查流水线"}>
              <Button
                type="link"
                size="small"
                icon={<SyncOutlined />}
                loading={retryMut.isPending && retryMut.variables === row.task_id}
                onClick={() => retryMut.mutate(row.task_id!)}
              >
                重试
              </Button>
            </Tooltip>
          )}
          <Tooltip title="查看该合同的报告导出历史">
            <Button
              type="link"
              size="small"
              icon={<FileTextOutlined />}
              onClick={() => setExportFor(row)}
            />
          </Tooltip>
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
              // 扫描件（PDF）与图片都走 OCR 链路，入口一并放开
              accept=".docx,.pdf,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp"
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
        {/* 批量操作条：勾选后才出现，避免空按钮占位干扰视觉（PRD 2.4.5「批量操作」） */}
        {selectedIds.length > 0 && (
          <Flex
            align="center"
            justify="space-between"
            style={{
              marginBottom: 12,
              padding: "8px 12px",
              background: "#e6f4ff",
              border: "1px solid #91caff",
              borderRadius: 6,
            }}
          >
            <Typography.Text>
              已选 <Typography.Text strong>{selectedIds.length}</Typography.Text> 项
            </Typography.Text>
            <Space>
              <Button size="small" onClick={() => setSelectedIds([])}>
                取消选择
              </Button>
              <Button
                size="small"
                icon={<SyncOutlined />}
                loading={batchRetryMut.isPending}
                onClick={() =>
                  modal.confirm({
                    title: `批量重试 ${selectedIds.length} 份合同？`,
                    content: "仅对处于「阻塞」状态的任务生效，其余条目会逐条回报失败原因。",
                    okText: "重试",
                    cancelText: "取消",
                    onOk: () => batchRetryMut.mutateAsync(selectedIds),
                  })
                }
              >
                批量重试
              </Button>
              <Button
                size="small"
                danger
                loading={batchDeleteMut.isPending}
                onClick={() =>
                  modal.confirm({
                    title: `批量删除 ${selectedIds.length} 份合同？`,
                    content: "软删除，数据保留在库中但列表不再显示。",
                    okText: "删除",
                    okButtonProps: { danger: true },
                    cancelText: "取消",
                    onOk: () => batchDeleteMut.mutateAsync(selectedIds),
                  })
                }
              >
                批量删除
              </Button>
            </Space>
          </Flex>
        )}
        <Table<ContractListItem>
          rowKey="id"
          loading={isLoading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 1280 }}
          rowSelection={{
            selectedRowKeys: selectedIds,
            onChange: (keys) => setSelectedIds(keys as number[]),
            preserveSelectedRowKeys: true,
          }}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          locale={{
            emptyText: (
              <Empty
                description="还没有合同。点右上角「同步审批待办」或「上传合同」开始。"
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              />
            ),
          }}
        />
      </Card>

      {/* 导出记录列表（PRD 2.4.1「支持生成并导出审查意见报告」；接口早已就绪） */}
      <Modal
        open={!!exportFor}
        title={`导出记录：${exportFor?.title ?? ""}`}
        onCancel={() => setExportFor(null)}
        footer={null}
        width={620}
      >
        <Table
          rowKey="id"
          size="small"
          loading={exportsQuery.isLoading}
          dataSource={exportsQuery.data ?? []}
          pagination={false}
          locale={{
            emptyText: (
              <Empty
                description="还没有导出记录。到工作台点「导出报告」即可生成。"
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              />
            ),
          }}
          columns={[
            {
              title: "格式",
              dataIndex: "format",
              width: 90,
              render: (f: string) => <Tag>{f === "pdf" ? "PDF" : "Markdown"}</Tag>,
            },
            {
              title: "大小",
              dataIndex: "file_size",
              width: 90,
              render: (v: number | null) => (v ? formatBytes(v) : "—"),
            },
            {
              title: "导出人",
              dataIndex: "created_by",
              width: 90,
              render: (v: string | null) => v || "—",
            },
            {
              title: "时间",
              dataIndex: "created_at",
              width: 140,
              render: (v: string) => dayjs(v).format("MM-DD HH:mm:ss"),
            },
            {
              title: "操作",
              key: "action",
              width: 80,
              render: (_: unknown, row: { download_url: string }) => (
                <Button
                  type="link"
                  size="small"
                  icon={<DownloadOutlined />}
                  href={row.download_url}
                  target="_blank"
                >
                  下载
                </Button>
              ),
            },
          ]}
        />
      </Modal>
    </div>
  );
}
