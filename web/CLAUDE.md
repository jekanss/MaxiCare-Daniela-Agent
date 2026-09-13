# web/ — la interfaz del panel

React + Vite + Tailwind, aparte del paquete de Python. Sale de un archivo de Figma Make;
`web/src/marca/` y los tokens `--color-sp-*` de `index.css` vienen de allí y **no se
renombran**, o la siguiente pantalla que llegue de Figma deja de encajar.

```
cd web && npm install && npm run build     # deja web/dist, que es lo que sirve runtime.py
cd web && npm run dev                      # :5173 con proxy a :8080 — hacen falta LOS DOS
uv run uvicorn maxicare_daniela.runtime:app --port 8080
```

- **LA TRAMPA QUE MÁS VA A COSTAR: el panel escribe en `public`, el chat de pruebas lee de
  `pruebas_web`.** Son dos bases distintas. Editar un precio en la pantalla y preguntarle a
  Daniela en la pestaña Pruebas **no** sirve para comprobar el cambio: ella cita el valor de
  la semilla y parece un fallo. Ya hizo que el entregable de esa fase se escribiera mal la
  primera vez. Para comprobar el camino completo: `probar_panel.py --chat`, que prueba cada
  mitad por su lado.
- **El chat de pruebas escribe en el esquema `pruebas_web`, nunca en `public`.** No es
  `pruebas`: ese lo BORRAN `probar_tools.py` y `probar_agentes.py` al terminar.
- **En `web/src/pantallas/Tratamientos.tsx`, el `useCallback` de `recargar` tiene
  dependencias vacías a propósito, y `alCaducarSesion` se consume por una `ref`.** Meter esa
  prop en las dependencias —lo que pediría cualquier regla de hooks— deja la pantalla
  releyendo Neon en bucle, porque `App.tsx` la pasa como una flecha nueva en cada render. No
  hay `eslint-plugin-react-hooks` ni arnés de pruebas de frontend que lo atrape: se vería
  como una pantalla lenta y una factura rara.
- **El `Literal` de tratamientos está partido en dos.** `LecturaArchivo` conserva los 14
  escritos a mano —es el muro, y tiene su prueba `test_tratamiento_no_admite_una_frase_clinica`—;
  el vocabulario de negocio vive en la tabla `tratamientos` y lo carga `runtime.py` al
  arrancar. Crear un tratamiento desde la pantalla **no** lo mete en el muro: una
  radiografía suya se clasifica `no_identificado`. Verificado por `probar_panel.py`.
- **Dos personas editando la misma ficha: la segunda pisa a la primera.** No hay bloqueo
  optimista, es deliberado. Lo que lo hace aceptable no es que sea improbable, sino que
  `cambios_configuracion` guarda el valor anterior: una edición pisada es recuperable, no
  perdida. Eso vale para el contenido **y para `aprobado`**, que se registra en su propia
  fila. Si algún día se añade un campo editable a la ficha, tiene que anotarse también, o
  esta frase vuelve a ser mentira para ese campo y el límite deja de ser aceptable.

## Del lado de Python, pero solo importa desde aquí

- Sin `MAXICARE_SECRETO_SESION` el panel se apaga con un 503 y **el webhook sigue vivo**.
  Es deliberado: WhatsApp está en producción y no puede caerse por una variable del panel.
- La ruta comodín que sirve `index.html` va **al final** de `runtime.py`. Antes se tragaría
  `/api`, `/salud` y el webhook.
