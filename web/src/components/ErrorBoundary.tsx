/**
 * W-01 — 최상위 ErrorBoundary.
 *
 * 한 페이지 렌더 예외가 SPA 전체를 백지로 만들지 않도록 콘텐츠 영역만 fallback으로
 * 격리한다. 사이드바·상단바(킬스위치·LIVE 배지·로그아웃)는 살아남는다.
 *
 * 디자인 시스템: empty-state 패턴 + brand outline 새로고침 CTA (DESIGN.md).
 * 원인은 console.error로 보존 — 콘솔에서 stack을 확인할 수 있다.
 */
import React from "react";

interface State {
  error: Error | null;
}

interface Props {
  children: React.ReactNode;
}

export default class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    // 원인 보존 — 사용자가 콘솔에서 stack을 확인할 수 있게 한다.
    // 외부 알림으로 보내지 않음 (사용자 PC 보안).
    console.error("[ErrorBoundary] 콘텐츠 렌더 예외:", error, info);
  }

  private _reset = () => {
    this.setState({ error: null });
  };

  private _reload = () => {
    window.location.reload();
  };

  render() {
    if (this.state.error) {
      return (
        <div className="empty-state" role="alert">
          <h3>화면을 그릴 수 없습니다</h3>
          <p className="muted">
            이 페이지에서 예기치 못한 오류가 발생했습니다. 새로고침하거나 다른 페이지로
            이동해 보세요. 문제가 반복되면 콘솔(F12)의 stack을 확인하세요.
          </p>
          <pre className="muted small" style={{
            maxWidth: 640, overflow: "auto", marginTop: 12,
            padding: 8, background: "rgba(0,0,0,0.04)", borderRadius: 6,
          }}>
            {String(this.state.error.message || this.state.error)}
          </pre>
          <div style={{ marginTop: 16, display: "flex", gap: 8 }}>
            <button className="primary" onClick={this._reload}>새로고침</button>
            <button className="ghost" onClick={this._reset}>다시 시도</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
