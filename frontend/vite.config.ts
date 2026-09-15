import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// Backend port for dev server proxy (default: 8000)
const backendPort = process.env.BACKEND_PORT || '8000'
const backendUrl = `http://localhost:${backendPort}`


export default defineConfig({
  // Relative asset URLs (./assets/...), which is what lets the same build serve
  // both directly and under Home Assistant's ingress prefix
  // (/api/hassio_ingress/<session>/), a path that is unknown at build time.
  //
  // This was `base: '/'` because relative URLs used to resolve against the
  // current route: a refresh on a deep route (/camera/<id>, /projects/<id>,
  // kiosk /spoolbuddy/ams) asked for /<route-prefix>/assets/..., the SPA
  // fallback answered with text/html, and the browser refused to execute it
  // (#1221). What changed since: the server now injects `<base href>` as the
  // first element in <head> (backend/app/core/ingress.py), so every relative
  // URL in the document resolves against the app root — "/" on direct access,
  // the ingress prefix behind the proxy — at any route depth. #1221's failure
  // mode is gone, and the subpath reverse-proxy case (#1195) is covered by the
  // same mechanism.
  base: './',
  plugins: [react()],
  build: {
    outDir: '../static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 3000,
  },
  server: {
    host: '0.0.0.0',
    proxy: {
      '/api/v1/ws': {
        target: backendUrl,
        ws: true,
        changeOrigin: true,
      },
      '/api': {
        target: backendUrl,
        changeOrigin: true,
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
