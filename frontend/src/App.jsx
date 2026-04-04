import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import CreateCheck from './components/CreateCheck';
import PaymentView from './components/PaymentView';

function App() {
  return (
    <Router>
      <div className="min-h-screen flex flex-col items-center justify-center p-4">
        <Routes>
          <Route path="/" element={<CreateCheck />} />
          <Route path="/pay/:checkId" element={<PaymentView />} />
        </Routes>
      </div>
    </Router>
  );
}

export default App;
