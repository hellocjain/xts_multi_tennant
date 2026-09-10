import React, { useState } from 'react';
import { X, Plus, RotateCw, UserPlus } from 'lucide-react';
import { api } from '../../services/api';
import { toast } from 'sonner';

interface AddClientModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

export const AddClientModal: React.FC<AddClientModalProps> = ({ isOpen, onClose, onSuccess }) => {
  const [tenantId, setTenantId] = useState('');
  const [clientName, setClientName] = useState('');
  const [brokerClientId, setBrokerClientId] = useState('');
  const [appKey, setAppKey] = useState('');
  const [secretKey, setSecretKey] = useState('');
  const [executionMode, setExecutionMode] = useState<'LIVE' | 'PAPER'>('LIVE');
  const [isSubmitting, setIsSubmitting] = useState(false);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!tenantId.trim() || !clientName.trim()) {
      toast.error('Tenant ID and Client Name are required');
      return;
    }

    setIsSubmitting(true);
    try {
      await api.addClient({
        tenant_id: tenantId.trim().toLowerCase(),
        name: clientName.trim(),
        broker_client_id: brokerClientId.trim(),
        app_key: appKey.trim(),
        secret_key: secretKey.trim(),
        execution_mode: executionMode,
      });
      toast.success(`Client ${clientName} created successfully!`);
      onSuccess();
      onClose();
    } catch (err: any) {
      toast.error(`Failed to create client: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="add-client-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !isSubmitting) onClose();
      }}
      className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4"
    >
      <div className="bg-cardbg border border-bordercolor w-full max-w-lg rounded-2xl p-6 shadow-2xl space-y-4 animate-in fade-in zoom-in-95 duration-150">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-2 text-brand-400">
            <UserPlus className="w-5 h-5" />
            <h3 id="add-client-title" className="font-bold text-base text-slate-100">Add New Trading Client</h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            className="text-slate-400 hover:text-slate-200 p-1 rounded-lg"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4 text-xs font-mono">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <label htmlFor="new-tenant-id" className="text-slate-400">Tenant ID (e.g. abk13)</label>
              <input
                id="new-tenant-id"
                type="text"
                required
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value.toLowerCase())}
                placeholder="abk13"
                className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
              />
            </div>

            <div className="space-y-1">
              <label htmlFor="new-client-name" className="text-slate-400">Client Name</label>
              <input
                id="new-client-name"
                type="text"
                required
                value={clientName}
                onChange={(e) => setClientName(e.target.value)}
                placeholder="John Doe"
                className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <label htmlFor="new-broker-client-id" className="text-slate-400">Broker Client ID</label>
              <input
                id="new-broker-client-id"
                type="text"
                value={brokerClientId}
                onChange={(e) => setBrokerClientId(e.target.value)}
                placeholder="AB1234"
                className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
              />
            </div>

            <div className="space-y-1">
              <label htmlFor="new-execution-mode" className="text-slate-400">Execution Mode</label>
              <select
                id="new-execution-mode"
                value={executionMode}
                onChange={(e) => setExecutionMode(e.target.value as any)}
                className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
              >
                <option value="LIVE">LIVE (Real Broker Routing)</option>
                <option value="PAPER">PAPER (Simulated Virtual Fills)</option>
              </select>
            </div>
          </div>

          <div className="space-y-1">
            <label htmlFor="new-app-key" className="text-slate-400">Interactive App Key</label>
            <input
              id="new-app-key"
              type="text"
              value={appKey}
              onChange={(e) => setAppKey(e.target.value)}
              placeholder="XTS Interactive API Key"
              className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
            />
          </div>

          <div className="space-y-1">
            <label htmlFor="new-secret-key" className="text-slate-400">Interactive Secret Key</label>
            <input
              id="new-secret-key"
              type="password"
              value={secretKey}
              onChange={(e) => setSecretKey(e.target.value)}
              placeholder="XTS Interactive Secret Key"
              className="w-full px-3 py-2 rounded-xl bg-obsidian border border-bordercolor text-slate-100 focus:outline-none focus:border-brand-500"
            />
          </div>

          <div className="flex items-center justify-end space-x-2 pt-3 border-t border-bordercolor">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded-xl text-slate-400 hover:text-slate-200 bg-slate-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting}
              className="px-5 py-2 rounded-xl bg-brand-600 hover:bg-brand-500 text-white font-semibold flex items-center space-x-1.5 shadow-sm"
            >
              {isSubmitting ? <RotateCw className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
              <span>Provision Client</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
