import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';
import { wsService } from '../services/websocket';
import { DashboardTelemetry } from '../types/telemetry';

export function useTelemetry(enabled: boolean = true) {
  const queryClient = useQueryClient();
  const [isWsConnected, setIsWsConnected] = useState<boolean>(false);

  // TanStack Query for initial load and background fallback
  const { data: telemetry, isLoading, error, refetch } = useQuery<DashboardTelemetry>({
    queryKey: ['dashboardTelemetry'],
    queryFn: api.getDashboardData,
    enabled,
    refetchInterval: enabled ? (isWsConnected ? 15000 : 3000) : false,
    refetchOnWindowFocus: enabled,
  });

  useEffect(() => {
    if (!enabled) {
      wsService.disconnect();
      return;
    }

    wsService.connect();

    const unsubStatus = wsService.on('connection_status', ({ connected }: { connected: boolean }) => {
      setIsWsConnected(connected);
    });

    const unsubTelemetry = wsService.on('telemetry_update', (data: DashboardTelemetry) => {
      if (data && data.clients) {
        queryClient.setQueryData(['dashboardTelemetry'], data);
      }
    });

    return () => {
      unsubStatus();
      unsubTelemetry();
      wsService.disconnect();
    };
  }, [enabled, queryClient]);

  return {
    telemetry,
    isLoading,
    error,
    isWsConnected,
    refetch,
  };
}
