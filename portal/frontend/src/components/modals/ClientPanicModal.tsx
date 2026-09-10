import React, { useState } from 'react';
import { Flame, Loader2, X } from 'lucide-react';
import { api } from '../../services/api';
import { toast } from 'sonner';

interface ClientPanicModalProps {
  isOpen: boolean;
  clientId: string;
  clientName?: string;
  onClose: () => void;
  onSuccess?: () => void;
}

export const ClientPanicModal: React.FC<ClientPanicModalProps> = ({
  isOpen,
  clientId,
  clientName,
  onClose,
  onSuccess,
}) => {
  const [isSubmitting, setIsSubmitting] = useState(false);

  if (!isOpen) return null;

  const handlePanic = async () => {
    setIsSubmitting(true);
    toast.info(`🚨 Executing panic square-off for ${clientName || clientId}...`, {
      duration: 3000,
    });
    try {
      await api.panicClient(clientId);
      toast.success(`🚨 Panic square-off completed for ${clientName || clientId}!`);
      onClose();
      if (onSuccess) onSuccess();
    } catch (err: any) {
      toast.error(`Panic square-off failed: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-cardbg border border-rose-500/40 w-full max-w-md rounded-2xl p-6 shadow-2xl space-y-5 animate-in fade-in zoom-in-95 duration-150">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3 text-rose-400">
            <div className="w-10 h-10 rounded-xl bg-rose-500/20 border border-rose-500/30 flex items-center justify-center">
              <Flame className="w-5 h-5 text-rose-400 animate-pulse" />
            </div>
            <div>
              <h3 className="font-bold text-base text-slate-100">Client Panic Square-Off</h3>
              <p className="text-xs text-rose-400 font-mono">EMERGENCY KILL-SWITCH • {clientId}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            disabled={isSubmitting}
            className="text-slate-400 hover:text-slate-200 p-1.5 rounded-lg hover:bg-slate-800 transition"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="space-y-3 text-xs text-slate-300 leading-relaxed">
          <p>
            Are you sure you want to trigger an emergency panic square-off for{' '}
            <strong className="text-white">{clientName || clientId}</strong>?
          </p>
          <div className="p-3 bg-obsidian rounded-xl border border-bordercolor space-y-1.5 text-[11px] font-mono">
            <div className="text-rose-300 flex items-center space-x-1.5">
              <span>•</span>
              <span>Cancel all open / pending orders immediately</span>
            </div>
            <div className="text-rose-300 flex items-center space-x-1.5">
              <span>•</span>
              <span>Dispatch marketable limit orders to close all positions</span>
            </div>
            <div className="text-rose-300 flex items-center space-x-1.5">
              <span>•</span>
              <span>Reset active strategy state to FLAT</span>
            </div>
          </div>
        </div>

        <div className="flex items-center justify-end space-x-2.5 pt-2">
          <button
            type="button"
            onClick={onClose}
            disabled={isSubmitting}
            className="px-4 py-2 text-xs font-semibold text-slate-400 hover:text-slate-200 bg-slate-800 rounded-xl transition"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handlePanic}
            disabled={isSubmitting}
            className="px-4 py-2 text-xs font-bold rounded-xl bg-rose-600 hover:bg-rose-500 text-white flex items-center space-x-1.5 shadow-lg shadow-rose-600/30 transition"
          >
            {isSubmitting ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                <span>Executing...</span>
              </>
            ) : (
              <>
                <Flame className="w-3.5 h-3.5" />
                <span>Execute Panic Square-Off</span>
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
};
