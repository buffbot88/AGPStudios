import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3101,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:3110',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:3110',
        changeOrigin: true,
      },
    },
  },
});
