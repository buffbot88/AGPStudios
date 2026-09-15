import { vitePlugin as remixVitePlugin } from '@remix-run/dev';
import UnoCSS from 'unocss/vite';
import { defineConfig, type ViteDevServer } from 'vite';
import { nodePolyfills } from 'vite-plugin-node-polyfills';
import { optimizeCssModules } from 'vite-plugin-optimize-css-modules';
import tsconfigPaths from 'vite-tsconfig-paths';

export default defineConfig((config) => {
  return {
    build: {
      target: 'esnext',
    },
    optimizeDeps: {
      // Kept pre-bundled so the client-only `path` shim can import it without
      // it appearing in application source.
      include: ['path-browserify'],
      esbuildOptions: {
        // Dependency pre-bundling (client) needs the same `path` ->
        // path-browserify mapping the plugin below provides for source
        // imports; without it, optimized deps importing `node:path` (e.g.
        // istextorbinary) are served as browser-externals and crash at runtime.
        alias: {
          path: 'path-browserify',
          'node:path': 'path-browserify',
        },
      },
    },
    server: {
      proxy: {
        // GitHub App endpoints live on the auth backend behind the production
        // edge; route them there during development as well.
        '/api/github': {
          target: 'https://agpstudios.org',
          changeOrigin: true,
        },
      },
      headers: {
        // WebContainer (workbench) requires cross-origin isolation.
        'Cross-Origin-Opener-Policy': 'same-origin',
        'Cross-Origin-Embedder-Policy': 'require-corp',
      },
    },
    plugins: [
      nodePolyfills({
        // `path` is polyfilled by the plugin below (client-only) instead of here:
        // the plugin's global `path` -> `path-browserify` alias also applies during
        // Vite dev SSR, where the CommonJS polyfill fails with "module is not
        // defined". Server code must keep the real node:path.
        include: ['buffer'],
      }),
      {
        name: 'client-only-path-polyfill',
        enforce: 'pre',
        resolveId(source, importer, options) {
          if (options?.ssr) {
            return null;
          }

          if (source === 'path' || source === 'node:path') {
            // Namespace imports (`* as nodePath`) need real named exports,
            // which CJS interop does not provide in dev — resolve to the ESM
            // shim instead of the raw CJS package.
            return this.resolve('~/lib/client/path-shim', importer, { skipSelf: true });
          }

          return null;
        },
      },
      remixVitePlugin({
        future: {
          v3_fetcherPersist: true,
          v3_relativeSplatPath: true,
          v3_throwAbortReason: true,
        },
      }),
      UnoCSS(),
      tsconfigPaths(),
      chrome129IssuePlugin(),
      config.mode === 'production' && optimizeCssModules({ apply: 'build' }),
    ],
  };
});

function chrome129IssuePlugin() {
  return {
    name: 'chrome129IssuePlugin',
    configureServer(server: ViteDevServer) {
      server.middlewares.use((req, res, next) => {
        const raw = req.headers['user-agent']?.match(/Chrom(e|ium)\/([0-9]+)\./);

        if (raw) {
          const version = parseInt(raw[2], 10);

          if (version === 129) {
            res.setHeader('content-type', 'text/html');
            res.end(
              '<body><h1>Please use Chrome Canary for testing.</h1><p>Chrome 129 has an issue with JavaScript modules & Vite local development, see <a href="https://github.com/stackblitz/bolt.new/issues/86#issuecomment-2395519258">for more information.</a></p><p><b>Note:</b> This only impacts <u>local development</u>. `pnpm run build` and `pnpm run start` will work fine in this browser.</p></body>',
            );

            return;
          }
        }

        next();
      });
    },
  };
}
