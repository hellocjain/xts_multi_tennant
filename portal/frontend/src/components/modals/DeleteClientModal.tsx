import React, { useState } from 'react';
import { AlertTriangle, Trash2, Loader2, X } from 'lucide-react';
import { api } from '../../services/api';
import { toast } from 'sonner';

interface DeleteClientModalProps {
  isOpen: boolean;
  clientId: string;
  clientName?: string;
  onClose: () => void;
  onSuccess?: () => void;
}

export const DeleteClientModal: React.FC<DeleteClientModalProps> = ({
  isOpen,
  clientId,
  clientName,
  onClose,
  onSuccess,
}) => {
  const [confirmText, setConfirmText] = useState('');
  const [isDeleting, setIsDeleting] = useState(false);

  if (!isOpen) return null;

  const isConfirmed = confirmText.trim().toLowerCase() === clientId.trim().toLowerCase();

  const handleClose = () => {
    setConfirmText('');
    setIsDeleting(false);
    onClose();
  };

  const handleDelete = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!isConfirmed || isDeleting) return;

    setIsDeleting(true);
    try {
      await api.deleteClient(clientId);
      toast.success(`Client ${clientName || clientId} has been deleted permanently`);
      handleClose();
      if (onSuccess) onSuccess();
    } catch (err: any) {
      toast.error(`Failed to delete client: ${err.message || 'Unknown error'}`);
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-client-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !isDeleting) handleClose();
      }}
      className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4"
    >
      <div className="bg-cardbg border border-rose-500/40 w-full max-w-md rounded-2xl p-6 shadow-2xl space-y-5 animate-in fade-in zoom-in-95 duration-150">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3 text-rose-400">
            <div className="w-10 h-10 rounded-xl bg-rose-500/20 border border-rose-500/30 flex items-center justify-center">
              <AlertTriangle className="w-5 h-5 text-rose-400" />
            </div>
            <div>
              <h3 id="delete-client-title" className="font-bold text-base text-slate-100">Delete Client Container</h3>
              <p className="text-xs text-rose-400 font-mono">DANGER ZONE • IRREVERSIBLE</p>
            </div>
          </div>
          <button
            type="button"
            onClick={handleClose}
            disabled={isDeleting}
            aria-label="Close dialog"
            className="text-slate-400 hover:text-slate-200 p-1.5 rounded-lg hover:bg-slate-800 transition"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <p className="text-xs text-slate-300 leading-relaxed">
          This will immediately stop and remove Docker container <strong className="text-white font-mono">{clientId}</strong>, purge all associated SuperTrend strategies, and delete encrypted broker credentials.
        </p>

        <form onSubmit={handleDelete} className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-[11px] font-mono text-slate-400">
              Type <strong className="text-rose-400 font-mono">{clientId}</strong> below to confirm deletion:
            </label>
            <input
              type="text"
              autoFocus
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={clientId}
              disabled={isDeleting}
              className="w-full px-3.5 py-2.5 rounded-xl bg-obsidian border border-bordercolor text-slate-100 text-xs font-mono focus:border-rose-500 focus:outline-none transition"
            />
          </div>

          <div className="flex items-center justify-end space-x-2.5 pt-2">
            <button
              type="button"
              onClick={handleClose}
              disabled={isDeleting}
              className="px-4 py-2 text-xs font-semibold text-slate-400 hover:text-slate-200 bg-slate-800 rounded-xl transition"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!isConfirmed || isDeleting}
              className={`px-4 py-2 text-xs font-bold rounded-xl flex items-center space-x-1.5 transition ${
                isConfirmed && !isDeleting
                  ? 'bg-rose-600 hover:bg-rose-500 text-white shadow-lg shadow-rose-600/30'
                  : 'bg-slate-800 text-slate-500 border border-slate-700 cursor-not-allowed'
              }`}
            >
              {isDeleting ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span>Deleting...</span>
                </>
              ) : (
                <>
                  <Trash2 className="w-3.5 h-3.5" />
                  <span>Delete Permanently</span>
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
