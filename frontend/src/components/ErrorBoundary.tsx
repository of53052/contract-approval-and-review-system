/**
 * 渲染错误边界。
 *
 * **为什么需要它**：React 组件渲染期抛异常时，整棵组件树会被卸载，
 * 页面变成纯白、控制台之外没有任何线索——排查成本极高。
 * 这里兜住异常，把错误信息与堆栈直接显示在页面上。
 *
 * 注意：错误边界**只能捕获渲染/生命周期/构造函数中的异常**，
 * 事件回调与异步代码（如接口请求）里的异常不走这里，
 * 那类错误由 React Query 的 error 状态或 try/catch 处理。
 */
import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button, Result, Typography } from "antd";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
  stack: string;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, stack: "" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 控制台留一份完整信息，便于开发期定位
    console.error("渲染异常:", error, info.componentStack);
    this.setState({ stack: info.componentStack || "" });
  }

  render(): ReactNode {
    const { error, stack } = this.state;
    if (!error) return this.props.children;

    return (
      <Result
        status="error"
        title="页面渲染出错"
        subTitle="这是前端渲染异常，不是后端服务问题。可刷新重试；若持续出现请把下面的信息反馈给开发。"
        extra={[
          <Button type="primary" key="reload" onClick={() => window.location.reload()}>
            刷新页面
          </Button>,
          <Button key="home" onClick={() => (window.location.href = "/")}>
            返回大盘
          </Button>,
        ]}
      >
        <Typography.Paragraph>
          <Typography.Text code>{error.name}: {error.message}</Typography.Text>
        </Typography.Paragraph>
        {stack && (
          <Typography.Paragraph>
            <pre style={{ maxHeight: 260, overflow: "auto", fontSize: 12 }}>{stack}</pre>
          </Typography.Paragraph>
        )}
      </Result>
    );
  }
}
