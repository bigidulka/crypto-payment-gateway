import React, { useEffect, useState, useRef } from 'react';
import axios from 'axios';
import { useParams } from 'react-router-dom';
import { QRCodeSVG } from 'qrcode.react';
import { Copy, CheckCircle2, Loader2, AlertCircle } from 'lucide-react';
import CryptoIcon from './CryptoIcon';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const WS_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000';

const PaymentView = () => {
  const { checkId } = useParams();
  const [check, setCheck] = useState(null);
  const [loading, setLoading] = useState(true);
  const [payment, setPayment] = useState(null);
  const [status, setStatus] = useState('pending'); // pending, detected, gas_sent, completed
  const ws = useRef(null);

  useEffect(() => {
    fetchCheck();
  }, [checkId]);

  useEffect(() => {
    if (checkId) {
      connectWebSocket();
    }
    return () => {
      if (ws.current) ws.current.close();
    };
  }, [checkId]);

  const connectWebSocket = () => {
    ws.current = new WebSocket(`${WS_URL}/api/checks/ws/${checkId}`);
    
    ws.current.onmessage = (event) => {
      const data = JSON.parse(event.data);
      console.log("WS Update:", data);
      if (data.status) {
        setStatus(data.status);
        // Обновляем данные платежа если нужно
        if (data.amount_received) {
            setPayment(prev => ({...prev, amount_received: data.amount_received}));
        }
      }
    };

    ws.current.onclose = () => {
      // Reconnect logic could go here
      setTimeout(connectWebSocket, 3000);
    };
  };

  const fetchCheck = async () => {
    try {
      const response = await axios.get(`${API_URL}/api/checks/${checkId}`);
      setCheck(response.data);
      if (response.data.payment) {
        setPayment(response.data.payment);
        setStatus(response.data.payment.status);
      }
    } catch (error) {
      console.error('Error fetching check:', error);
    } finally {
      setLoading(false);
    }
  };

  const handlePayClick = async () => {
    try {
      setLoading(true);
      const response = await axios.post(`${API_URL}/api/checks/${checkId}/pay`);
      setCheck(response.data);
      setPayment(response.data.payment);
      setStatus(response.data.payment.status);
    } catch (error) {
      console.error('Error initiating payment:', error);
    } finally {
      setLoading(false);
    }
  };

  const copyToClipboard = (text) => {
    navigator.clipboard.writeText(text);
    // Could add toast notification here
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center">
        <Loader2 className="w-8 h-8 animate-spin text-gray-400" />
      </div>
    );
  }

  if (!check) {
    return (
      <div className="text-center text-red-500">
        <AlertCircle className="w-12 h-12 mx-auto mb-2" />
        <p>Чек не найден</p>
      </div>
    );
  }

  const isCompleted = status === 'completed';
  const isProcessing = ['detected', 'gas_sent'].includes(status);

  return (
    <div className="w-full max-w-md bg-white rounded-2xl shadow-xl overflow-hidden border border-gray-100">
      {/* Header */}
      <div className="bg-black p-6 text-white text-center">
        <p className="text-gray-400 text-sm uppercase tracking-wider mb-1">К оплате</p>
        <h1 className="text-4xl font-bold mb-2 flex items-center justify-center gap-3">
          <CryptoIcon name={check.currency} className="w-8 h-8 text-white" />
          {check.amount} <span className="text-2xl font-normal text-gray-400">{check.currency}</span>
        </h1>
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-gray-800 text-xs font-medium">
          <CryptoIcon name={check.chain} className="w-4 h-4" />
          {check.chain} Network
        </div>
      </div>

      <div className="p-8">
        {isCompleted ? (
          <div className="text-center py-8">
            <div className="w-20 h-20 bg-green-100 rounded-full flex items-center justify-center mx-auto mb-6">
              <CheckCircle2 className="w-10 h-10 text-green-600" />
            </div>
            <h2 className="text-2xl font-bold mb-2">Оплата успешна!</h2>
            <p className="text-gray-500">Средства зачислены.</p>
          </div>
        ) : !payment ? (
          <div className="text-center">
            <p className="text-gray-600 mb-8">
              {check.description || "Оплата заказа"}
            </p>
            <button
              onClick={handlePayClick}
              className="w-full bg-black text-white py-4 rounded-xl font-medium hover:bg-gray-800 transition-colors"
            >
              Перейти к оплате
            </button>
          </div>
        ) : (
          <div className="space-y-6">
            {/* QR Code */}
            <div className="flex justify-center">
              <div className="p-4 border-2 border-gray-100 rounded-2xl">
                <QRCodeSVG value={payment.wallet_address} size={180} />
              </div>
            </div>

            {/* Address */}
            <div>
              <label className="block text-xs font-medium text-gray-400 uppercase tracking-wider mb-2 text-center">
                Адрес кошелька ({check.chain})
              </label>
              <div 
                onClick={() => copyToClipboard(payment.wallet_address)}
                className="group flex items-center justify-between p-4 bg-gray-50 rounded-xl cursor-pointer hover:bg-gray-100 transition-colors"
              >
                <code className="text-sm font-mono break-all text-gray-800">
                  {payment.wallet_address}
                </code>
                <Copy className="w-4 h-4 text-gray-400 group-hover:text-black transition-colors flex-shrink-0 ml-3" />
              </div>
              <p className="text-xs text-center text-gray-400 mt-2">
                Отправляйте только {check.currency} в сети {check.chain}
              </p>
            </div>

            {/* Status */}
            <div className="border-t border-gray-100 pt-6">
              <div className="flex items-center justify-between mb-2">
                <span className="text-sm text-gray-500">Статус</span>
                <span className={`text-sm font-medium ${isProcessing ? 'text-blue-600' : 'text-gray-900'}`}>
                  {status === 'pending' && 'Ожидание оплаты...'}
                  {status === 'detected' && 'Платеж обнаружен'}
                  {status === 'gas_sent' && 'Обработка вывода...'}
                  {status === 'completed' && 'Готово'}
                </span>
              </div>
              {/* Progress Bar */}
              <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                <div 
                  className={`h-full transition-all duration-500 ${
                    status === 'completed' ? 'bg-green-500 w-full' :
                    status === 'gas_sent' ? 'bg-blue-500 w-3/4' :
                    status === 'detected' ? 'bg-blue-500 w-1/2' :
                    'bg-gray-300 w-5 animate-pulse'
                  }`}
                ></div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default PaymentView;
