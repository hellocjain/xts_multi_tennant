import React, { Component, ErrorInfo, ReactNode } from 'react';
import { AlertTriangle, RotateCw, Terminal } from 'lucide-react';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null,
    errorInfo: null,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error, errorInfo: null };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    this.setState({ errorInfo });
    console.error('Terminal Uncaught Exception:', error, errorInfo);
  }

  private handleReload = () => {
    window.location.reload();
  };

  private handleReset = () => {
    this.setState({ hasError: false, error: null, errorInfo: null });
  };

  public render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen w-full bg-[#05070A] text-slate-100 flex flex-col items-center justify-center p-6 font-mono select-none">
          <div className="w-full max-w-xl bg-cardbg border border-rose-500/40 rounded-3xl p-8 shadow-2xl space-y-6">
            <div className="flex items-center space-x-3 text-rose-400">
              <div className="w-12 h-12 rounded-2xl bg-rose-500/20 border border-rose-500/30 flex items-center justify-center shadow-inner">
                <AlertTriangle className="w-6 h-6 text-rose-400 animate-pulse" />
              </div>
              <div>
                <h1 className="text-base font-bold text-slate-100 uppercase tracking-wider">
                  Terminal Workspace Interrupted
                </h1>
                <p className="text-xs text-rose-400">AN UNEXPECTED RUNTIME EXCEPTION OCCURRED</p>
              </div>
            </div>

            <p className="text-xs text-slate-300 leading-relaxed">
              The terminal caught an unhandled rendering error. All backend order execution engines, client containers, and risk limits remain active and unaffected on the server.
            </p>

            {this.state.error && (
              <div className="space-y-1.5">
                <div className="text-[10px] text-slate-400 uppercase tracking-wider flex items-center space-x-1">
                  <Terminal className="w-3 h-3 text-slate-500" />
                  <span>Exception Details</span>
                </div>
                <div className="p-3.5 bg-obsidian rounded-xl border border-bordercolor text-[11px] text-rose-300 overflow-x-auto whitespace-pre-wrap max-h-40">
                  {this.state.error.toString()}
                </div>
              </div>
            )}

            <div className="flex items-center justify-end space-x-3 pt-2 border-t border-bordercolor/60">
              <button
                type="button"
                onClick={this.handleReset}
                className="px-4 py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 font-semibold text-xs transition"
              >
                Attempt In-Memory Recovery
              </button>
              <button
                type="button"
                onClick={this.handleReload}
                className="px-5 py-2.5 rounded-xl bg-brand-600 hover:bg-brand-500 text-white font-bold text-xs flex items-center space-x-2 transition shadow-lg shadow-brand-600/30"
              >
                <RotateCw className="w-4 h-4" />
                <span>Reload Terminal</span>
              </button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
