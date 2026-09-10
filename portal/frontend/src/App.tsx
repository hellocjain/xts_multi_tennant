import React, { useState, useEffect } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Header } from './components/layout/Header';
import { WorkspaceTabs } from './components/layout/WorkspaceTabs';
import { DashboardView } from './components/views/DashboardView';
import { ClientDetailView } from './components/views/ClientDetailView';
import { GlobalOrdersView } from './components/views/GlobalOrdersView';
import { AuditLogsView } from './components/views/AuditLogsView';
import { SettingsView } from './components/views/SettingsView';
import { LoginView } from './components/views/LoginView';
import { GlobalPanicModal } from './components/modals/GlobalPanicModal';
import { AddClientModal } from './components/modals/AddClientModal';
import { useWorkspaceTabs } from './hooks/useWorkspaceTabs';
import { useTelemetry } from './hooks/useTelemetry';
import { api } from './services/api';
import { Toaster, toast } from 'sonner';
import { ErrorBoundary } from './components/layout/ErrorBoundary';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function TerminalApp() {
  const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null);
  const [isPanicModalOpen, setIsPanicModalOpen] = useState(false);
  const [isAddClientModalOpen, setIsAddClientModalOpen] = useState(false);

  const {
    tabs,
    activeTabId,
    setActiveTabId,
    openClientTab,
    openTab,
    closeTab,
  } = useWorkspaceTabs();

  const { telemetry, isLoading, refetch, isWsConnected } = useTelemetry(isAuthenticated === true);

  useEffect(() => {
    let isMounted = true;
    api.getMe().then((res) => {
      if (isMounted) {
        setIsAuthenticated(res.authenticated);
      }
    }).catch(() => {
      if (isMounted) {
        setIsAuthenticated(false);
      }
    });

    const handleUnauthorized = () => {
      queryClient.clear();
      setIsAuthenticated(false);
    };

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setIsPanicModalOpen(false);
        setIsAddClientModalOpen(false);
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        const searchInput = document.querySelector<HTMLInputElement>('input[data-search="true"], input[placeholder*="Search"]');
        if (searchInput) {
          searchInput.focus();
          searchInput.select();
        }
      }
    };

    window.addEventListener('auth:unauthorized', handleUnauthorized);
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('auth:unauthorized', handleUnauthorized);
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, []);

  const handleLogout = async () => {
    try {
      await api.logout();
    } catch {
      // ignore
    } finally {
      queryClient.clear();
      setIsAuthenticated(false);
      toast.success('Logged out successfully');
    }
  };

  if (isAuthenticated === null) {
    return (
      <div className="h-screen w-screen bg-[#05070A] flex items-center justify-center font-mono text-xs text-brand-400">
        INITIALIZING TERMINAL...
      </div>
    );
  }

  if (!isAuthenticated) {
    return <LoginView onLoginSuccess={() => setIsAuthenticated(true)} />;
  }

  // Active Tab View resolution
  const activeTab = tabs.find((t) => t.id === activeTabId) || tabs[0];

  return (
    <div className="h-screen w-screen flex flex-col bg-obsidian text-slate-100 overflow-hidden select-none">
      {/* Top Header */}
      <Header
        telemetry={telemetry}
        isWsConnected={isWsConnected}
        onOpenGlobalPanic={() => setIsPanicModalOpen(true)}
        onLogout={handleLogout}
      />

      {/* Workspace Tabs Navigation Ribbon */}
      <WorkspaceTabs
        tabs={tabs}
        activeTabId={activeTabId}
        onSelectTab={setActiveTabId}
        onCloseTab={closeTab}
        onOpenOrders={() =>
          openTab({
            id: 'orders',
            type: 'orders',
            title: 'Global Orders',
            isClosable: true,
          })
        }
        onOpenLogs={() =>
          openTab({
            id: 'logs',
            type: 'logs',
            title: 'Audit Logs',
            isClosable: true,
          })
        }
        onOpenSettings={() =>
          openTab({
            id: 'settings',
            type: 'settings',
            title: 'System Settings',
            isClosable: true,
          })
        }
        onAddNewClient={() => setIsAddClientModalOpen(true)}
      />

      {/* Main Active View Canvas */}
      <main className="flex-1 flex flex-col overflow-hidden relative">
        {activeTab.type === 'overview' && (
          <DashboardView
            telemetry={telemetry}
            isLoading={isLoading}
            onRefresh={refetch}
            onOpenClientTab={openClientTab}
            onToggleTrading={async (cid, pause) => {
              await api.toggleClientTrading(cid, pause);
              refetch();
            }}
            onOpenAddClient={() => setIsAddClientModalOpen(true)}
          />
        )}

        {activeTab.type === 'client' && activeTab.clientId && (
          <ClientDetailView
            key={activeTab.clientId}
            clientId={activeTab.clientId}
            clientName={activeTab.clientName}
            onCloseTab={() => closeTab(activeTab.id)}
          />
        )}

        {activeTab.type === 'orders' && <GlobalOrdersView />}

        {activeTab.type === 'logs' && <AuditLogsView />}

        {activeTab.type === 'settings' && <SettingsView />}
      </main>

      {/* Emergency Global Panic Modal */}
      <GlobalPanicModal
        isOpen={isPanicModalOpen}
        onClose={() => setIsPanicModalOpen(false)}
        onSuccess={() => refetch()}
      />

      {/* Add Client Modal */}
      <AddClientModal
        isOpen={isAddClientModalOpen}
        onClose={() => setIsAddClientModalOpen(false)}
        onSuccess={() => refetch()}
      />

      {/* Toast Notification Container */}
      <Toaster position="bottom-right" richColors theme="dark" />
    </div>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <TerminalApp />
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
