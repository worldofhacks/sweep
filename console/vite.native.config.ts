import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Same Atlas components, separate native entry: no inactive fleet sockets or media bootstrap.
export default defineConfig({
  plugins: [react()],
  base: './',
  build: { outDir: 'dist-android', rollupOptions: { input: 'atlas-native.html' } },
})
