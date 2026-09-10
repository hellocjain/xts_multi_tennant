import React from 'react';
import { 
  LayoutDashboard, 
  User, 
  BookOpen, 
  ShieldCheck, 
  Settings as SettingsIcon, 
  X, 
  Plus
} from 'lucide-react';
import { WorkspaceTab } from '../../types/workspace';

interface WorkspaceTabsProps {
  tabs: WorkspaceTab[];
  activeTabId: string;
  onSelectTab: (tabId: string) => void;
  onCloseTab: (tabId: string) => void;
  onOpenOrders: () => void;
  onOpenLogs: () => void;
  onOpenSettings: () => void;
  onAddNewClient?: () => void;
}

export const WorkspaceTabs: React.FC<WorkspaceTabsProps> = ({
  tabs,
  activeTabId,
  onSelectTab,
  onCloseTab,
  onOpenOrders,
  onOpenLogs,
  onOpenSettings,
  onAddNewClient,
}) => {
  const getTabIcon = (tab: WorkspaceTab) => {
    switch (tab.type) {
      case 'overview':
        return <LayoutDashboard className="w-3.5 h-3.5 text-brand-400" />;
      case 'client':
        return <User className="w-3.5 h-3.5 text-slate-300" />;
      case 'orders':
        return <BookOpen className="w-3.5 h-3.5 text-sky-400" />;
      case 'logs':
        return <ShieldCheck className="w-3.5 h-3.5 text-amber-400" />;
      case 'settings':
        return <SettingsIcon className="w-3.5 h-3.5 text-slate-400" />;
      default:
        return null;
    }
  };

  return (
    <div className="h-10 bg-slate-950 border-b border-bordercolor flex items-center justify-between px-2 shrink-0 select-none overflow-x-auto no-scrollbar">
      {/* Scrollable Tabs Ribbon */}
      <div className="flex items-center space-x-1.5 overflow-x-auto no-scrollbar py-1">
        {tabs.map((tab) => {
          const isActive = tab.id === activeTabId;
          return (
            <div
              key={tab.id}
              onClick={() => onSelectTab(tab.id)}
              className={`group relative flex items-center space-x-2 px-3 py-1.5 rounded-t-lg border-t border-x cursor-pointer transition-all duration-150 text-xs font-medium shrink-0 ${
                isActive
                  ? 'bg-cardbg border-bordercolor text-slate-100 shadow-sm border-b-transparent -mb-[1px] z-10'
                  : 'bg-obsidian/60 border-transparent hover:bg-slate-900/80 text-slate-400 hover:text-slate-200'
              }`}
            >
              <div className="flex items-center space-x-1.5">
                {getTabIcon(tab)}
                <span className="font-semibold tracking-wide truncate max-w-[130px]">
                  {tab.title}
                </span>
              </div>

              {/* Dynamic Badge (e.g. MTM or Count) */}
              {tab.badge !== undefined && (
                <span
                  className={`text-[10px] font-mono font-bold px-1.5 py-0.2 rounded ${
                    tab.badgeColor === 'profit'
                      ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                      : tab.badgeColor === 'loss'
                      ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
                      : 'bg-slate-800 text-slate-300'
                  }`}
                >
                  {tab.badge}
                </span>
              )}

              {/* Close Tab Button */}
              {tab.isClosable && (
                <button
                  type="button"
                  title="Close Tab"
                  onClick={(e) => {
                    e.stopPropagation();
                    onCloseTab(tab.id);
                  }}
                  className="opacity-40 group-hover:opacity-100 hover:bg-slate-800 hover:text-rose-400 p-0.5 rounded transition"
                >
                  <X className="w-3 h-3" />
                </button>
              )}

              {/* Active Tab Underline Glow */}
              {isActive && (
                <div className="absolute -top-[1px] left-0 right-0 h-[2px] bg-brand-500 rounded-t-sm" />
              )}
            </div>
          );
        })}

        {onAddNewClient && (
          <button
            type="button"
            onClick={onAddNewClient}
            title="Add New Trading Client"
            className="flex items-center space-x-1 px-2.5 py-1 text-[11px] font-medium text-slate-400 hover:text-slate-200 hover:bg-slate-900 rounded-lg border border-dashed border-slate-800 transition"
          >
            <Plus className="w-3 h-3" />
            <span>New Client</span>
          </button>
        )}
      </div>

      {/* Quick Nav Right Toolbar */}
      <div className="flex items-center space-x-1.5 pl-2 border-l border-bordercolor/60 shrink-0 text-xs">
        <button
          type="button"
          onClick={onOpenOrders}
          title="All System Orders"
          className={`flex items-center space-x-1 px-2.5 py-1 rounded-lg transition font-medium text-xs ${
            activeTabId === 'orders'
              ? 'bg-sky-500/20 text-sky-300 border border-sky-500/30'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900'
          }`}
        >
          <BookOpen className="w-3.5 h-3.5" />
          <span className="hidden md:inline">Orders</span>
        </button>

        <button
          type="button"
          onClick={onOpenLogs}
          title="Security & Audit Logs"
          className={`flex items-center space-x-1 px-2.5 py-1 rounded-lg transition font-medium text-xs ${
            activeTabId === 'logs'
              ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900'
          }`}
        >
          <ShieldCheck className="w-3.5 h-3.5" />
          <span className="hidden md:inline">Audit Logs</span>
        </button>

        <button
          type="button"
          onClick={onOpenSettings}
          title="System & Risk Settings"
          className={`flex items-center space-x-1 px-2.5 py-1 rounded-lg transition font-medium text-xs ${
            activeTabId === 'settings'
              ? 'bg-slate-800 text-slate-200 border border-slate-700'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900'
          }`}
        >
          <SettingsIcon className="w-3.5 h-3.5" />
          <span className="hidden md:inline">Settings</span>
        </button>
      </div>
    </div>
  );
};
