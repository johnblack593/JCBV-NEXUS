import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Toaster } from 'react-hot-toast';
import App from './App';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Toaster
      position="top-right"
      toastOptions={{
        duration: 4000,
        style: {
          background: '#1a2332',
          color: '#F9FAFB',
          border: '1px solid #1F2937',
          borderRadius: '8px',
          fontSize: '13px',
          fontFamily: 'Inter, system-ui, sans-serif',
        },
        success: {
          iconTheme: { primary: '#10B981', secondary: '#0B0E14' },
        },
        error: {
          iconTheme: { primary: '#EF4444', secondary: '#0B0E14' },
        },
      }}
    />
    <App />
  </StrictMode>
);
