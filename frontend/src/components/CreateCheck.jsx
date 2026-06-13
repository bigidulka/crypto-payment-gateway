import React, { useState } from 'react';
import axios from 'axios';
import { useNavigate } from 'react-router-dom';
import { Wallet, ArrowRight } from 'lucide-react';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

const CreateCheck = () => {
  const navigate = useNavigate();
  const [formData, setFormData] = useState({
    amount: '',
    currency: 'USDT',
    chain: 'BNB',
    description: ''
  });
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      const response = await axios.post(`${API_URL}/api/checks/`, formData);
      navigate(`/pay/${response.data.id}`);
    } catch (error) {
      console.error('Error creating check:', error);
      alert('Failed to create check');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="w-full max-w-md bg-white rounded-2xl shadow-xl p-8 border border-gray-100">
      <div className="flex items-center gap-3 mb-8">
        <div className="p-3 bg-black rounded-xl">
          <Wallet className="w-6 h-6 text-white" />
        </div>
        <h1 className="text-2xl font-bold">Создать чек</h1>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">Сумма</label>
          <input
            type="number"
            step="0.000001"
            required
            className="w-full px-4 py-3 rounded-xl border border-gray-200 focus:border-black focus:ring-0 transition-colors"
            placeholder="0.00"
            value={formData.amount}
            onChange={(e) => setFormData({ ...formData, amount: e.target.value })}
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Валюта</label>
            <select
              className="w-full px-4 py-3 rounded-xl border border-gray-200 focus:border-black focus:ring-0 bg-white"
              value={formData.currency}
              onChange={(e) => setFormData({ ...formData, currency: e.target.value })}
            >
              <option value="USDT">USDT</option>
              <option value="USDC">USDC</option>
              <option value="ETH">ETH</option>
              <option value="BNB">BNB</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Сеть</label>
            <select
              className="w-full px-4 py-3 rounded-xl border border-gray-200 focus:border-black focus:ring-0 bg-white"
              value={formData.chain}
              onChange={(e) => setFormData({ ...formData, chain: e.target.value })}
            >
              <option value="BNB">BNB Chain</option>
              <option value="BASE">Base</option>
              <option value="ARBITRUM">Arbitrum</option>
            </select>
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">Описание (опционально)</label>
          <input
            type="text"
            className="w-full px-4 py-3 rounded-xl border border-gray-200 focus:border-black focus:ring-0 transition-colors"
            placeholder="За услуги..."
            value={formData.description}
            onChange={(e) => setFormData({ ...formData, description: e.target.value })}
          />
        </div>

        <button
          type="submit"
          disabled={loading}
          className="w-full bg-black text-white py-4 rounded-xl font-medium hover:bg-gray-800 transition-colors flex items-center justify-center gap-2 disabled:opacity-50"
        >
          {loading ? 'Создание...' : (
            <>
              Создать ссылку <ArrowRight className="w-4 h-4" />
            </>
          )}
        </button>
      </form>
    </div>
  );
};

export default CreateCheck;
