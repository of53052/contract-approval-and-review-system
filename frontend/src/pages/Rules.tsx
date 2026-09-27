/**
 * 合规规则与模板页（PRD 2.4.3 / 2.4.5；架构 §14.1 图三）。
 *
 * 布局：左栏合同类型模板树 | 中栏规则列表 | 右栏规则详情（抽屉编辑）
 *      下方：标准示范条款库 + 主体黑名单
 *
 * **本页是规则库的写入口**。后端在写入时校验配置（`rule_config.validate_rule`），
 * 配错的规则会以 400 + 具体原因拒绝，而不是落库后静默失效——
 * 因为规则引擎遇到坏规则只会 `logger.error` 后跳过，用户无从察觉。
 */
import { useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Drawer,
  Empty,
  Flex,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Tree,
  Typography,
  App as AntApp,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createRule,
  createStandardClause,
  deleteRule,
  deleteStandardClause,
  listBlacklist,
  listRuleOptions,
  listRuleTemplates,
  listStandardClauses,
  updateRule,
  updateRuleTemplate,
  updateStandardClause,
} from "../api";
import { apiError } from "../api/client";
import type {
  Rule,
  RuleConditionIn,
  RuleIn,
  RuleTemplate,
  StandardClause,
  StandardClauseIn,
} from "../types";
import { BUSINESS_TYPE_LABEL, RISK_META } from "../constants";

/** 规则类型 → 需要额外填写的 config 字段。表单按类型动态渲染。 */
const CONFIG_HINT: Record<string, string> = {
  keyword: "关键词之间是「或」关系；多个关键词填在下方条件里，或用逗号分隔。",
  regex: "正则表达式直接写在条件里（operator = regex）。",
  threshold: "阈值规则需要 metric / threshold / direction 三个参数。",
  presence: "存在性检查：可要求必备条款类型、必备元数据键，或指定条款内必须出现的文本。",
  blacklist: "黑名单：填关键词列表，或把 source 设为 subject_blacklist 走主体库。",
};

export default function Rules() {
  const qc = useQueryClient();
  const { message, modal } = AntApp.useApp();

  const [activeTemplateId, setActiveTemplateId] = useState<number | null>(null);
  const [keyword, setKeyword] = useState("");
  const [tab, setTab] = useState<string>("clauses");
  /** 编辑中的规则；`"new"` 表示新建。 */
  const [editing, setEditing] = useState<Rule | "new" | null>(null);
  /** 编辑中的示范条款；`"new"` 表示新建。 */
  const [editingClause, setEditingClause] = useState<StandardClause | "new" | null>(null);
  /** 编辑中的模板（改名称 / 描述 / 启用）。 */
  const [editingTemplate, setEditingTemplate] = useState<RuleTemplate | null>(null);

  const optionsQ = useQuery({ queryKey: ["rule-options"], queryFn: listRuleOptions });
  const templatesQ = useQuery({ queryKey: ["rule-templates"], queryFn: listRuleTemplates });
  const clausesQ = useQuery({ queryKey: ["standard-clauses"], queryFn: listStandardClauses });
  const blacklistQ = useQuery({ queryKey: ["blacklist"], queryFn: listBlacklist });

  const templates = templatesQ.data ?? [];
  const activeTemplate = useMemo(
    () => templates.find((t) => t.id === activeTemplateId) ?? templates[0] ?? null,
    [templates, activeTemplateId],
  );

  const invalidate = () => qc.invalidateQueries({ queryKey: ["rule-templates"] });

  // ---------------- 规则启停（最常用的操作，做行内开关） ----------------
  const toggleRuleMut = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      updateRule(id, { enabled }),
    onSuccess: (r) => {
      message.success(`规则「${r.name}」已${r.enabled ? "启用" : "停用"}`);
      invalidate();
    },
    onError: (e) => message.error(apiError(e)),
  });

  const deleteRuleMut = useMutation({
    mutationFn: ({ id, force }: { id: number; force: boolean }) => deleteRule(id, force),
    onSuccess: () => {
      message.success("规则已删除");
      invalidate();
    },
    // 被历史依据引用时后端返回 409 并说明原因，这里引导用户改用停用或强删
    onError: (e) => {
      const detail = apiError(e);
      if (!detail.includes("历史风险依据")) {
        message.error(detail);
        return;
      }
      modal.confirm({
        title: "该规则已被历史审查结果引用",
        content: detail,
        okText: "仍要删除",
        okButtonProps: { danger: true },
        cancelText: "改为停用",
        onOk: async () => {
          try {
            await deleteRuleMut.mutateAsync({ id: deleteRuleMut.variables!.id, force: true });
          } catch (err) {
            message.error(apiError(err));
          }
        },
        onCancel: async () => {
          const id = deleteRuleMut.variables!.id;
          const row = allRules.find((r) => r.id === id);
          if (row) toggleRuleMut.mutate({ id, enabled: false });
        },
      });
    },
  });

  const allRules = useMemo(
    () => templates.flatMap((t) => t.rules),
    [templates],
  );

  const ruleColumns: ColumnsType<Rule> = [
    {
      title: "规则",
      dataIndex: "name",
      width: 240,
      render: (name: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{name}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.code}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "风险等级",
      dataIndex: "risk_level",
      width: 100,
      render: (lv: Rule["risk_level"]) => {
        const m = RISK_META[lv];
        return (
          <Tag color={m?.color}>
            {m?.badge} {m?.label ?? lv}
          </Tag>
        );
      },
    },
    {
      title: "类型",
      dataIndex: "rule_type",
      width: 110,
      render: (t: string) =>
        optionsQ.data?.rule_type.find((o) => o.value === t)?.label ?? t,
    },
    {
      title: "条件",
      key: "conditions",
      // 条件里的正则可能很长（如管辖地规则），不加约束会把「启用」列挤出去
      render: (_: unknown, row) =>
        row.conditions.length === 0 ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : (
          <Space direction="vertical" size={0} style={{ maxWidth: 460 }}>
            {row.conditions.map((c) => {
              const text = `${c.field} ${
                optionsQ.data?.operator.find((o) => o.value === c.operator)?.label ?? c.operator
              }${c.value ? ` ${c.value}` : ""}`;
              return (
                <Typography.Text
                  key={c.id}
                  style={{ fontSize: 12, display: "block" }}
                  type="secondary"
                  ellipsis={{ tooltip: text }}
                >
                  {text}
                </Typography.Text>
              );
            })}
          </Space>
        ),
    },
    {
      title: "启用",
      dataIndex: "enabled",
      width: 80,
      render: (v: boolean, row) => (
        <Switch
          size="small"
          checked={v}
          loading={toggleRuleMut.isPending && toggleRuleMut.variables?.id === row.id}
          onChange={(checked) => toggleRuleMut.mutate({ id: row.id, enabled: checked })}
        />
      ),
    },
    {
      title: "操作",
      key: "action",
      width: 120,
      render: (_: unknown, row) => (
        <Space size={0}>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => setEditing(row)}
          >
            编辑
          </Button>
          <Popconfirm
            title="删除该规则？"
            description="若已被历史审查引用，会提示改用停用。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => deleteRuleMut.mutate({ id: row.id, force: false })}
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const clauseColumns: ColumnsType<StandardClause> = [
    {
      title: "条款类型",
      dataIndex: "clause_type",
      width: 110,
      render: (t: string) => (
        <Tag>{optionsQ.data?.clause_type.find((o) => o.value === t)?.label ?? t}</Tag>
      ),
    },
    {
      title: "适用类型",
      dataIndex: "contract_type",
      width: 100,
      render: (t: string | null) =>
        t ? BUSINESS_TYPE_LABEL[t] ?? t : <Typography.Text type="secondary">通用</Typography.Text>,
    },
    { title: "标题", dataIndex: "title", width: 180, ellipsis: true },
    {
      title: "正文",
      dataIndex: "content",
      ellipsis: true,
      render: (c: string) => (
        <Tooltip title={c}>
          <Typography.Text type="secondary">{c}</Typography.Text>
        </Tooltip>
      ),
    },
    {
      title: "来源",
      dataIndex: "source",
      width: 200,
      render: (s: string | null) => s ?? <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      title: "操作",
      key: "action",
      width: 120,
      render: (_: unknown, row) => (
        <Space size={0}>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => setEditingClause(row)}
          >
            编辑
          </Button>
          <Popconfirm
            title="删除该示范条款？"
            description="它同时是防幻觉的法条白名单来源，删除后相关内容可能被判「待人工复核」。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={async () => {
              try {
                await deleteStandardClause(row.id);
                message.success("已删除");
                qc.invalidateQueries({ queryKey: ["standard-clauses"] });
              } catch (e) {
                message.error(apiError(e));
              }
            }}
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const filteredRules = (activeTemplate?.rules ?? []).filter(
    (r) =>
      !keyword ||
      r.name.includes(keyword) ||
      r.code.toLowerCase().includes(keyword.toLowerCase()),
  );

  return (
    <div style={{ padding: 24 }}>
      <Card
        title="合规规则与模板"
        extra={
          <Space>
            <Input.Search
              allowClear
              placeholder="搜索规则名称 / 编码"
              style={{ width: 220 }}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
            <Button
              icon={<ReloadOutlined />}
              loading={templatesQ.isFetching}
              onClick={() => templatesQ.refetch()}
            >
              刷新
            </Button>
          </Space>
        }
      >
        <Row gutter={16}>
          {/* ---------- 左：合同类型 ---------- */}
          <Col flex="220px">
            <Card size="small" title="合同类型" styles={{ body: { padding: 8 } }}>
              <Tree
                blockNode
                selectedKeys={activeTemplate ? [String(activeTemplate.id)] : []}
                onSelect={(keys) => {
                  if (keys.length) setActiveTemplateId(Number(keys[0]));
                }}
                treeData={templates.map((t) => ({
                  key: String(t.id),
                  title: (
                    <Flex justify="space-between" align="center" gap={4}>
                      <span style={{ opacity: t.enabled ? 1 : 0.45 }}>
                        {BUSINESS_TYPE_LABEL[t.contract_type] ?? t.name}
                      </span>
                      <Space size={2}>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {t.rules.length}
                        </Typography.Text>
                        <Button
                          type="text"
                          size="small"
                          icon={<EditOutlined style={{ fontSize: 12 }} />}
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingTemplate(t);
                          }}
                        />
                      </Space>
                    </Flex>
                  ),
                }))}
              />
            </Card>
          </Col>

          {/* ---------- 中：规则列表 ---------- */}
          <Col flex="auto">
            <Card
              size="small"
              title={
                activeTemplate
                  ? `${BUSINESS_TYPE_LABEL[activeTemplate.contract_type] ?? activeTemplate.name} · 规则（${filteredRules.length}）`
                  : "规则"
              }
              extra={
                <Button
                  type="primary"
                  size="small"
                  icon={<PlusOutlined />}
                  disabled={!activeTemplate}
                  onClick={() => setEditing("new")}
                >
                  新建规则
                </Button>
              }
            >
              {activeTemplate && activeTemplate.rules.length === 0 && (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="该合同类型下还没有规则"
                  description="没有规则时审查只跑 LLM 引擎，确定性规则全部不生效。可点「新建规则」补充。"
                />
              )}
              <Table<Rule>
                rowKey="id"
                size="small"
                loading={templatesQ.isLoading}
                columns={ruleColumns}
                dataSource={filteredRules}
                pagination={false}
                locale={{
                  emptyText: (
                    <Empty
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description="没有匹配的规则"
                    />
                  ),
                }}
              />
            </Card>
          </Col>
        </Row>

        {/* ---------- 下：示范条款 / 黑名单 ---------- */}
        <Card
          size="small"
          style={{ marginTop: 16 }}
          title={
            <Segmented
              size="small"
              value={tab}
              onChange={(v) => setTab(String(v))}
              options={[
                { label: `标准示范条款库 ${clausesQ.data?.length ?? 0}`, value: "clauses" },
                { label: `主体黑名单 ${blacklistQ.data?.length ?? 0}`, value: "blacklist" },
              ]}
            />
          }
          extra={
            tab === "clauses" && (
              <Button
                size="small"
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => setEditingClause("new")}
              >
                新建示范条款
              </Button>
            )
          }
        >
          {tab === "clauses" ? (
            <Table<StandardClause>
              rowKey="id"
              size="small"
              loading={clausesQ.isLoading}
              columns={clauseColumns}
              dataSource={clausesQ.data ?? []}
              pagination={{ pageSize: 8, showSizeChanger: false }}
            />
          ) : (
            <>
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 12 }}
                message="本表是 mock 数据，非真实工商信息"
                description="PRD 要求「主体被列入经营异常」判定，但未提供数据源。演示用虚构数据，不得用于真实业务判断。"
              />
              <Table
                rowKey="id"
                size="small"
                loading={blacklistQ.isLoading}
                dataSource={blacklistQ.data ?? []}
                pagination={false}
                columns={[
                  { title: "主体名称", dataIndex: "subject_name" },
                  { title: "统一社会信用代码", dataIndex: "credit_code", width: 200 },
                  {
                    title: "状态",
                    dataIndex: "status",
                    width: 130,
                    render: (s: string) => <Tag color="error">{s}</Tag>,
                  },
                  { title: "详情", dataIndex: "detail" },
                ]}
              />
            </>
          )}
        </Card>
      </Card>

      {/* ---------- 模板编辑 ---------- */}
      {editingTemplate && (
        <TemplateModal
          key={editingTemplate.id}
          template={editingTemplate}
          onClose={() => setEditingTemplate(null)}
          onSaved={() => {
            setEditingTemplate(null);
            invalidate();
          }}
        />
      )}

      {/* ---------- 规则编辑抽屉 ---------- */}
      {editing !== null && (
        <RuleDrawer
          key={editing === "new" ? "new" : editing.id}
          rule={editing === "new" ? null : editing}
          templateId={activeTemplate?.id ?? null}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            invalidate();
          }}
        />
      )}

      {/* ---------- 示范条款编辑抽屉 ---------- */}
      {editingClause !== null && (
        <ClauseDrawer
          key={editingClause === "new" ? "new-clause" : editingClause.id}
          clause={editingClause === "new" ? null : editingClause}
          onClose={() => setEditingClause(null)}
          onSaved={() => {
            setEditingClause(null);
            qc.invalidateQueries({ queryKey: ["standard-clauses"] });
          }}
        />
      )}
    </div>
  );
}

// ==================== 模板编辑 ====================

interface TemplateModalProps {
  template: RuleTemplate;
  onClose: () => void;
  onSaved: () => void;
}

function TemplateModal({ template, onClose, onSaved }: TemplateModalProps) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();

  const saveMut = useMutation({
    mutationFn: async () => {
      const v = await form.validateFields();
      return updateRuleTemplate(template.id, {
        name: v.name,
        description: v.description ?? "",
        enabled: v.enabled,
      });
    },
    onSuccess: () => {
      message.success("模板已更新");
      onSaved();
    },
    onError: (e) => message.error(apiError(e)),
  });

  return (
    <Modal
      open
      title={`编辑模板：${template.name}`}
      onCancel={onClose}
      okText="保存"
      cancelText="取消"
      confirmLoading={saveMut.isPending}
      onOk={() => saveMut.mutate()}
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          name: template.name,
          description: template.description,
          enabled: template.enabled,
        }}
      >
        <Form.Item
          name="name"
          label="模板名称"
          rules={[{ required: true, message: "请输入模板名称" }]}
        >
          <Input />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input.TextArea rows={2} />
        </Form.Item>
        <Form.Item
          name="enabled"
          label="启用"
          valuePropName="checked"
          extra="停用后该合同类型的规则不再参与审查（模板整体失效）"
        >
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  );
}

// ==================== 规则编辑抽屉 ====================

interface RuleDrawerProps {
  rule: Rule | null;
  templateId: number | null;
  onClose: () => void;
  onSaved: () => void;
}

/** config 的形态随 rule_type 变化，用受控字段而非裸 JSON 编辑器。 */
function RuleDrawer({ rule, templateId, onClose, onSaved }: RuleDrawerProps) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const optionsQ = useQuery({ queryKey: ["rule-options"], queryFn: listRuleOptions });
  const opts = optionsQ.data;
  const [ruleType, setRuleType] = useState<string>(rule?.rule_type ?? "keyword");
  /**
   * 条件列表。
   *
   * **新建时默认为空**，不预置"半填"的模板行：预置行的 value 为空，
   * 而 `contains` 类运算符要求必须给值，用户填完关键词直接保存就会撞上
   * "第 1 个条件必须提供比较值"——一个与他的操作无关的报错。
   * 空列表则让提示落在真正缺失的东西上（如"关键词规则没有任何关键词"）。
   */
  const [conditions, setConditions] = useState<RuleConditionIn[]>(
    rule
      ? rule.conditions.map((c) => ({
          field: c.field,
          operator: c.operator,
          value: c.value,
          value_type: c.value_type,
        }))
      : [],
  );

  const saveMut = useMutation({
    mutationFn: async () => {
      const v = await form.validateFields();
      const config = buildConfig(ruleType, v);
      const body: RuleIn = {
        template_id: templateId!,
        code: v.code,
        name: v.name,
        category: v.category,
        risk_level: v.risk_level,
        rule_type: ruleType,
        config,
        result_template: v.result_template ?? null,
        suggestion_template: v.suggestion_template ?? null,
        enabled: v.enabled,
        seq: v.seq ?? 0,
        conditions: conditions.filter((c) => c.field && c.operator),
      };
      return rule ? updateRule(rule.id, body) : createRule(body);
    },
    onSuccess: (r) => {
      message.success(rule ? `规则「${r.name}」已更新` : `规则「${r.name}」已创建`);
      onSaved();
    },
    onError: (e) => message.error(apiError(e)),
  });

  return (
    <Drawer
      open
      width={640}
      title={rule ? `编辑规则：${rule.name}` : "新建规则"}
      onClose={onClose}
      extra={
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" loading={saveMut.isPending} onClick={() => saveMut.mutate()}>
            保存
          </Button>
        </Space>
      }
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          code: rule?.code,
          name: rule?.name,
          category: rule?.category ?? "liability",
          risk_level: rule?.risk_level ?? "medium",
          enabled: rule?.enabled ?? true,
          seq: rule?.seq ?? 0,
          result_template: rule?.result_template,
          suggestion_template: rule?.suggestion_template,
          // config 展开成表单字段，避免用户手写 JSON
          keywords: (rule?.config?.keywords as string[] | undefined)?.join("，"),
          match_all: rule?.config?.match_all ?? false,
          metric: rule?.config?.metric,
          threshold: rule?.config?.threshold,
          direction: rule?.config?.direction ?? "gt",
          required_clause_type: rule?.config?.required_clause_type,
          required_keys: rule?.config?.required_keys,
          required_pattern: rule?.config?.required_pattern,
          within_clause_type: rule?.config?.within_clause_type,
          blacklist: (rule?.config?.blacklist as string[] | undefined)?.join("，"),
          source_is_subject: rule?.config?.source === "subject_blacklist",
        }}
      >
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item
              name="code"
              label="规则编码"
              rules={[{ required: true, message: "请输入规则编码" }]}
              extra="大写字母 / 数字 / 下划线，模板内唯一"
            >
              <Input placeholder="LIABILITY_UNCAPPED" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="name"
              label="规则名称"
              rules={[{ required: true, message: "请输入规则名称" }]}
            >
              <Input placeholder="违约责任无上限" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="category" label="风险分类" rules={[{ required: true }]}>
              <Select options={opts?.category} />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="risk_level" label="风险等级" rules={[{ required: true }]}>
              <Select options={opts?.risk_level} />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="seq" label="执行顺序">
              <InputNumber min={0} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
          <Col span={24}>
            <Form.Item
              name="rule_type"
              label="规则类型"
              initialValue={ruleType}
              rules={[{ required: true }]}
            >
              <Select
                options={opts?.rule_type}
                onChange={(v) => setRuleType(String(v))}
              />
            </Form.Item>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {CONFIG_HINT[ruleType]}
            </Typography.Text>
          </Col>
        </Row>

        {/* ---- 按类型渲染 config ---- */}
        {ruleType === "keyword" && (
          <Row gutter={12}>
            <Col span={18}>
              <Form.Item name="keywords" label="关键词（逗号分隔，任一命中即触发）">
                <Input placeholder="无上限，不设上限，不受限制" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="match_all" label="要求全部命中" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>
        )}

        {ruleType === "threshold" && (
          <Row gutter={12}>
            <Col span={10}>
              <Form.Item name="metric" label="指标" rules={[{ required: true }]}>
                <Select options={opts?.metric} />
              </Form.Item>
            </Col>
            <Col span={7}>
              <Form.Item name="threshold" label="阈值" rules={[{ required: true }]}>
                <InputNumber style={{ width: "100%" }} step={0.01} placeholder="0.20" />
              </Form.Item>
            </Col>
            <Col span={7}>
              <Form.Item name="direction" label="比较方向" rules={[{ required: true }]}>
                <Select
                  options={[
                    { value: "gt", label: "大于" },
                    { value: "gte", label: "大于等于" },
                    { value: "lt", label: "小于" },
                    { value: "lte", label: "小于等于" },
                    { value: "eq", label: "等于" },
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>
        )}

        {ruleType === "presence" && (
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="required_clause_type"
                label="必备条款类型"
                extra="可多选；缺失即报风险"
              >
                <Select mode="multiple" allowClear options={opts?.clause_type} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="required_keys" label="必备元数据" extra="可多选">
                <Select mode="multiple" allowClear options={opts?.metadata_key} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="required_pattern"
                label="必须出现的文本"
                extra="填了「限定条款类型」时，语义变为「该类型条款内必须出现」"
              >
                <Input placeholder="验收" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="within_clause_type" label="限定条款类型" extra="需与上一项同时填写">
                <Select allowClear options={opts?.clause_type} />
              </Form.Item>
            </Col>
          </Row>
        )}

        {ruleType === "blacklist" && (
          <Row gutter={12}>
            <Col span={18}>
              <Form.Item name="blacklist" label="黑名单条目（逗号分隔，命中即触发）">
                <Input placeholder="境外仲裁，香港仲裁，乙方所在地法院" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="source_is_subject" label="改用主体库" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>
        )}

        {/* ---- 条件（结构化） ---- */}
        <Form.Item
          label="触发条件"
          extra="条件之间是「且」关系；「或」请拆成多条规则。关键词/阈值/存在性规则可以不填条件，用上面的 config 表达即可。"
        >
          <Space direction="vertical" style={{ width: "100%" }} size={8}>
            {conditions.map((c, i) => (
              <Flex key={i} gap={8}>
                <Select
                  style={{ flex: "0 0 190px" }}
                  value={c.field}
                  onChange={(v) =>
                    setConditions((prev) =>
                      prev.map((x, j) => (j === i ? { ...x, field: String(v) } : x)),
                    )
                  }
                  options={[
                    { value: "clause.content", label: "条款正文" },
                    { value: "clause.clause_type", label: "条款类型" },
                    { value: "clause.clause_no", label: "条款编号" },
                    { value: "clause.title", label: "条款标题" },
                    ...(opts?.metadata_key ?? []).map((o) => ({
                      value: `metadata.${o.value}`,
                      label: `元数据 · ${o.label}`,
                    })),
                  ]}
                />
                <Select
                  style={{ flex: "0 0 150px" }}
                  value={c.operator}
                  onChange={(v) =>
                    setConditions((prev) =>
                      prev.map((x, j) => (j === i ? { ...x, operator: v } : x)),
                    )
                  }
                  options={opts?.operator}
                />
                <Input
                  placeholder={
                    c.operator === "exists" || c.operator === "not_exists" ? "（无需填值）" : "比较值"
                  }
                  disabled={c.operator === "exists" || c.operator === "not_exists"}
                  value={c.value ?? ""}
                  onChange={(e) =>
                    setConditions((prev) =>
                      prev.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)),
                    )
                  }
                />
                <Button
                  danger
                  type="text"
                  icon={<DeleteOutlined />}
                  onClick={() => setConditions((prev) => prev.filter((_, j) => j !== i))}
                />
              </Flex>
            ))}
            <Button
              type="dashed"
              icon={<PlusOutlined />}
              onClick={() =>
                setConditions((prev) => [
                  ...prev,
                  { field: "clause.content", operator: "contains", value: "", value_type: "string" },
                ])
              }
            >
              添加条件
            </Button>
          </Space>
        </Form.Item>

        <Form.Item name="result_template" label="结论模板" extra="可用 {threshold} / {clause_no} / {value} 占位">
          <Input.TextArea rows={2} placeholder="违约责任未设赔偿上限，我方责任敞口不可预估。" />
        </Form.Item>
        <Form.Item name="suggestion_template" label="推荐修改条款模板">
          <Input.TextArea rows={3} placeholder="建议增加责任上限条款：…" />
        </Form.Item>
        <Form.Item name="enabled" label="启用" valuePropName="checked">
          <Switch />
        </Form.Item>
      </Form>
    </Drawer>
  );
}

/** 把表单字段收敛成规则引擎认识的 config 结构。 */
function buildConfig(ruleType: string, v: Record<string, unknown>): Record<string, unknown> | null {
  const splitList = (s: unknown): string[] =>
    String(s ?? "")
      .split(/[,，]/)
      .map((x) => x.trim())
      .filter(Boolean);

  if (ruleType === "keyword") {
    const keywords = splitList(v.keywords);
    return { keywords, match_all: Boolean(v.match_all) };
  }
  if (ruleType === "threshold") {
    return {
      metric: v.metric,
      threshold: v.threshold,
      direction: v.direction ?? "gt",
    };
  }
  if (ruleType === "presence") {
    const cfg: Record<string, unknown> = {};
    if (v.required_clause_type) cfg.required_clause_type = v.required_clause_type;
    if (v.required_keys) cfg.required_keys = v.required_keys;
    if (v.required_pattern) cfg.required_pattern = v.required_pattern;
    if (v.within_clause_type) cfg.within_clause_type = v.within_clause_type;
    return cfg;
  }
  if (ruleType === "blacklist") {
    if (v.source_is_subject) {
      return { source: "subject_blacklist", match_field: "subject_name" };
    }
    return { blacklist: splitList(v.blacklist) };
  }
  // regex：模式写在条件里，config 留空
  return null;
}

// ==================== 示范条款编辑抽屉 ====================

interface ClauseDrawerProps {
  clause: StandardClause | null;
  onClose: () => void;
  onSaved: () => void;
}

function ClauseDrawer({ clause, onClose, onSaved }: ClauseDrawerProps) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const optionsQ = useQuery({ queryKey: ["rule-options"], queryFn: listRuleOptions });

  const saveMut = useMutation({
    mutationFn: async () => {
      const v = await form.validateFields();
      const body: StandardClauseIn = {
        clause_type: v.clause_type,
        contract_type: v.contract_type ?? null,
        title: v.title,
        content: v.content,
        source: v.source ?? null,
        enabled: v.enabled,
      };
      return clause ? updateStandardClause(clause.id, body) : createStandardClause(body);
    },
    onSuccess: (r) => {
      message.success(clause ? `示范条款「${r.title}」已更新` : `示范条款「${r.title}」已创建`);
      onSaved();
    },
    onError: (e) => message.error(apiError(e)),
  });

  return (
    <Drawer
      open
      width={560}
      title={clause ? `编辑示范条款：${clause.title}` : "新建示范条款"}
      onClose={onClose}
      extra={
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" loading={saveMut.isPending} onClick={() => saveMut.mutate()}>
            保存
          </Button>
        </Space>
      }
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          clause_type: clause?.clause_type,
          contract_type: clause?.contract_type ?? undefined,
          title: clause?.title,
          content: clause?.content,
          source: clause?.source,
          enabled: clause?.enabled ?? true,
        }}
      >
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="clause_type" label="条款类型" rules={[{ required: true }]}>
              <Select options={optionsQ.data?.clause_type} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="contract_type" label="适用业务类型" extra="留空表示通用">
              <Select
                allowClear
                options={Object.entries(BUSINESS_TYPE_LABEL).map(([value, label]) => ({
                  value,
                  label,
                }))}
              />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="title" label="标题" rules={[{ required: true }]}>
          <Input placeholder="责任上限" />
        </Form.Item>
        <Form.Item name="content" label="条款正文" rules={[{ required: true }]}>
          <Input.TextArea rows={6} />
        </Form.Item>
        <Form.Item
          name="source"
          label="来源"
          extra="同时是防幻觉的法条白名单：LLM 给出的法条若无法匹配任一来源，会被标「待人工复核」"
        >
          <Input placeholder="《民法典》第五百八十五条 / 行业惯例" />
        </Form.Item>
        <Form.Item name="enabled" label="启用" valuePropName="checked">
          <Switch />
        </Form.Item>
      </Form>
    </Drawer>
  );
}
