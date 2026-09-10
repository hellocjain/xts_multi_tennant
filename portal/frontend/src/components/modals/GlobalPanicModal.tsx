import React, { useState, useEffect } from 'react';
import { AlertOctagon, Flame, Loader2, X } from 'lucide-react';
import { api } from '../../services/api';
import { toast } from 'sonner';

interface GlobalPanicModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

export const GlobalPanicModal: React.FC<GlobalPanicModalProps> = ({ isOpen, onClose, onSuccess }) => {
  const [confirmText, setConfirmText] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (isOpen) {
      setConfirmText('');
    }
  }, [isOpen]);

  if (!isOpen) return null;

  const isConfirmed = ['SQUARE OFF', 'PANIC ALL', 'PANIC'].includes(confirmText.trim().toUpperCase());

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!isConfirmed || isSubmitting) return;

    setIsSubmitting(true);
    toast.info('🚨 Executing emergency panic sweep across ALL active client accounts...', {
      duration: 4000,
    });

    try {
      const res = await api.panicAll();
      toast.success(`🚨 Global Panic Complete: All accounts swept and squared off!`, {
        duration: 5000,
      });
      onClose();
      if (onSuccess) onSuccess();
    } catch (err: any) {
      toast.error(`❌ Panic Error: ${err.message || 'Operation encountered an error'}`, {
        duration: 6000,
      });
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div
      onClick={(e) => {
        if (e.target === e.currentTarget && !isSubmitting) onClose();
      }}
      className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4"
    >
      <div className="bg-cardbg border border-rose-500/40 w-full max-w-md rounded-2xl p-6 shadow-2xl space-y-5 animate-in fade-in zoom-in-95 duration-150">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3 text-rose-400">
            <div className="w-10 h-10 rounded-xl bg-rose-500/20 border border-rose-500/30 flex items-center justify-center">
              <AlertOctagon className="w-5 h-5 text-rose-400 animate-pulse" />
            </div>
            <div>
              <h3 className="font-bold text-base text-slate-100">Confirm Global Emergency Square-Off</h3>
              <p className="text-xs text-rose-400 font-mono">CRITICAL ACTION • ALL ACCOUNTS</p>
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

        <p className="text-xs text-slate-300 leading-relaxed">
          ⚠️ This will immediately <strong className="text-white">cancel all pending orders</strong> and execute marketable limit square-off orders for <strong className="text-white">all open positions across ALL client accounts</strong>.
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-[11px] font-mono text-slate-400">
              Type <strong className="text-rose-400">SQUARE OFF</strong> or <strong className="text-rose-400">PANIC ALL</strong> below to confirm:
            </label>
            <input
              type="text"
              autoFocus
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder="Type SQUARE OFF"
              disabled={isSubmitting}
              className="w-full px-3.5 py-2.5 rounded-xl bg-obsidian border border-bordercolor text-slate-100 text-xs font-mono focus:border-rose-500 focus:outline-none transition"
            />
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
              type="submit"
              disabled={!isConfirmed || isSubmitting}
              className={`font-bold text-xs px-5 py-2 rounded-xl transition shadow-lg flex items-center gap-1.5 ${
                isConfirmed && !isSubmitting
                  ? 'bg-rose-600 hover:bg-rose-500 text-white shadow-rose-600/30 cursor-pointer'
                  : 'bg-rose-600/40 text-slate-400 cursor-not-allowed'
              }`}
            >
              {isSubmitting ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  <span>Sweeping All Clients...</span>
                </>
              ) : (
                <>
                  <Flame className="w-4 h-4" />
                  <span>Yes, Square Off All Clients</span>
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
