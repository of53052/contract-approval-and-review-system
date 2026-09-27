import { Layout, Typography } from "antd";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import ErrorBoundary from "./components/ErrorBoundary";
import Dashboard from "./pages/Dashboard";
import Workbench from "./pages/Workbench";

const { Header, Content } = Layout;

export default function App() {
  const navigate = useNavigate();
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 24,
          background: "#fff",
          borderBottom: "1px solid #f0f0f0",
          paddingInline: 24,
        }}
      >
        <Typography.Title
          level={4}
          style={{ margin: 0, cursor: "pointer", whiteSpace: "nowrap" }}
          onClick={() => navigate("/")}
        >
          合同智能审查系统
        </Typography.Title>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          解析 → 双引擎审查 → 双向定位 → 回写审批系统
        </Typography.Text>
      </Header>
      <Content style={{ background: "#f5f6f8" }}>
        {/* 兜住渲染期异常，避免整页白屏且无任何提示 */}
        <ErrorBoundary>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/contracts/:id" element={<Workbench />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </ErrorBoundary>
      </Content>
    </Layout>
  );
}
