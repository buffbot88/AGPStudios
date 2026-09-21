import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { reportAppError } from './globalErrors';

type Props = {
  /** Rendered when a child throws. */
  fallbackTitle: string;
  children: ReactNode;
};

type State = { error: Error | null };

/**
 * Render-time crash isolation for the member and admin surfaces.
 *
 * The conversation_messages bug showed that one unexpected API field can
 * unmount the entire React tree, taking the header, sign-in, and homepage
 * down with it. This boundary confines the blast radius: a bad render inside
 * a surface degrades to a styled "something went wrong" panel with a Retry
 * button (which remounts the subtree and re-runs its data fetches), while the
 * rest of the app keeps working.
 *
 * Note this only catches render/lifecycle errors — network failures inside
 * event handlers or effects are the surfaces' own try/catch responsibility.
 */
export class SurfaceErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[AGP] Surface crashed:', error, info.componentStack);
    // Route the crash through the shared error reporter so it also raises a
    // toast: a degraded surface is easy to miss if you aren't watching the
    // panel, and the toast is what points at the real message. The component
    // stack becomes the toast's detail line, naming what actually threw.
    reportAppError(error, this.props.fallbackTitle, info.componentStack ?? '');
  }

  private retry = () => {
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <section className="member-section workspace-locked" role="alert">
        <span className="eyebrow">AGP Studios</span>
        <h2>{this.props.fallbackTitle}</h2>
        <p>
          Something went wrong while loading this section. Your other pages are
          unaffected — try again, and if it keeps happening the data source may
          be temporarily unhealthy.
        </p>
        <button type="button" className="primary-button" onClick={this.retry}>
          Try again
        </button>
      </section>
    );
  }
}
