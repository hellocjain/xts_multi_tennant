export type TabType = 'overview' | 'client' | 'orders' | 'logs' | 'settings';

export interface WorkspaceTab {
  id: string; // 'overview', 'client-abk01', 'orders', etc.
  type: TabType;
  title: string;
  subtitle?: string;
  clientId?: string;
  clientName?: string;
  isClosable: boolean;
  badge?: string | number;
  badgeColor?: 'profit' | 'loss' | 'neutral' | 'brand';
}
