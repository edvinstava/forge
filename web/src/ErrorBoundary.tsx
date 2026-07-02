import React from "react";

interface Props {
  children: React.ReactNode;
}

interface State {
  error: Error | null;
}

/** Top-level guard so a single render throw shows a message instead of blanking
 *  the whole app. Before this, an object rendered as a React child (e.g. a plan
 *  step) would unmount everything to a white page. */
export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("[forge] render error:", error, info.componentStack);
  }

  handleReload = () => {
    this.setState({ error: null });
    location.reload();
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="app-error">
        <div className="app-error-icon">⚠</div>
        <div className="app-error-title">Something broke while rendering</div>
        <pre className="app-error-detail">{error.message}</pre>
        <button className="btn btn-accent" onClick={this.handleReload}>
          ↺ Reload
        </button>
      </div>
    );
  }
}
