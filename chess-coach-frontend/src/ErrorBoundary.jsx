import { Component } from "react";

// Catches render-time exceptions so a single bad component can't unmount the
// whole app into a blank white screen. React only routes render/lifecycle
// errors here (not event-handler or async errors) — which is exactly the
// failure mode we hit: an off-shape coach explanation threw during render.
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Keep the stack in the console so a render crash is debuggable instead of
    // a silent white screen.
    console.error("Render error caught by ErrorBoundary:", error, info?.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24, maxWidth: 560, margin: "40px auto", fontFamily: "system-ui, sans-serif", color: "#e6e6e6" }}>
          <h2 style={{ marginTop: 0 }}>Something went wrong displaying this view</h2>
          <p style={{ color: "#9aa0a6" }}>
            The app hit an unexpected error while rendering. Your games and analysis are safe — reloading usually fixes it.
          </p>
          <pre style={{ whiteSpace: "pre-wrap", background: "#1a1a1a", padding: 12, borderRadius: 8, fontSize: 12, color: "#ff8a8a" }}>
            {String(this.state.error?.message || this.state.error)}
          </pre>
          <button onClick={() => window.location.reload()} style={{ padding: "8px 14px" }}>Reload</button>
        </div>
      );
    }
    return this.props.children;
  }
}
