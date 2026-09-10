import { useState, useCallback } from 'react';
import { WorkspaceTab } from '../types/workspace';

const DEFAULT_TABS: WorkspaceTab[] = [
  {
    id: 'overview',
    type: 'overview',
    title: 'Executive Overview',
    isClosable: false,
  },
];

export function useWorkspaceTabs() {
  const [tabs, setTabs] = useState<WorkspaceTab[]>(DEFAULT_TABS);
  const [activeTabId, setActiveTabId] = useState<string>('overview');

  const openTab = useCallback((tab: WorkspaceTab) => {
    setTabs((prev) => {
      const existing = prev.find((t) => t.id === tab.id);
      if (existing) {
        // Tab already exists, update properties if needed
        return prev.map((t) => (t.id === tab.id ? { ...t, ...tab } : t));
      }
      return [...prev, tab];
    });
    setActiveTabId(tab.id);
  }, []);

  const openClientTab = useCallback((clientId: string, clientName: string) => {
    openTab({
      id: `client-${clientId}`,
      type: 'client',
      title: clientName || clientId,
      subtitle: clientId,
      clientId,
      clientName,
      isClosable: true,
    });
  }, [openTab]);

  const closeTab = useCallback((tabId: string) => {
    setTabs((prev) => {
      const newTabs = prev.filter((t) => t.id !== tabId);
      return newTabs.length > 0 ? newTabs : DEFAULT_TABS;
    });

    setActiveTabId((currentActive) => {
      if (currentActive === tabId) {
        // Find index of closed tab
        const idx = tabs.findIndex((t) => t.id === tabId);
        const remaining = tabs.filter((t) => t.id !== tabId);
        if (remaining.length > 0) {
          const nextTab = remaining[Math.max(0, idx - 1)];
          return nextTab.id;
        }
        return 'overview';
      }
      return currentActive;
    });
  }, [tabs]);

  const updateTabBadge = useCallback((tabId: string, badge?: string | number, badgeColor?: 'profit' | 'loss' | 'neutral' | 'brand') => {
    setTabs((prev) =>
      prev.map((t) => (t.id === tabId ? { ...t, badge, badgeColor } : t))
    );
  }, []);

  return {
    tabs,
    activeTabId,
    setActiveTabId,
    openTab,
    openClientTab,
    closeTab,
    updateTabBadge,
  };
}
