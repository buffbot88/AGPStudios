import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// API origin the dev proxy forwards to. Overridable so parallel stacks (E2E
// runs) can point at their own API instance instead of the default one.
const apiTarget = process.env.ASHAT_API_TARGET ?? 'http://127.0.0.1:3110';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3101,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
      '/health': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
});
