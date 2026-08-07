import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  // Omega listens on :8080 by default. Override via VITE_API_TARGET when
  // deploying the backend on another local port.
  const apiTarget = env.VITE_API_TARGET || 'http://127.0.0.1:8080';

  return {
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy: {
        '/api': apiTarget,
        '/health': apiTarget,
        '/v1': apiTarget,
      },
    },
    build: {
      target: 'es2022',
      sourcemap: true,
    },
  };
});
