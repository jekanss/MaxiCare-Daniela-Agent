import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// La configuración que trajo Figma Make incluía cinco plugins propios de su entorno de
// previsualización (site.json, replay del overlay de errores, el kit de stories). Aquí no
// existen: se quedaron fuera a propósito, no se perdieron.
//
// Lo que sí se conserva es el alias `@` -> ./src, porque el código importado de Make lo usa.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  build: {
    // `runtime.py` sirve esta carpeta. Si cambias la ruta, cambia también RUTA_WEB allá.
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // En desarrollo hay dos procesos: Vite en :5173 y uvicorn en :8080. El proxy hace que
    // el navegador los vea como uno solo, que es lo que importa para la cookie de sesión --
    // una cookie puesta por :8080 no viaja a :5173, así que sin esto el login "funcionaría"
    // y la siguiente petición saldría sin sesión.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/salud': { target: 'http://127.0.0.1:8080', changeOrigin: true },
    },
  },
})
