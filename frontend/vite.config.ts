import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/',
  server: { proxy: { '/api': 'http://127.0.0.1:8765' } },
  build: { sourcemap: false, outDir: 'dist', emptyOutDir: true },
});
