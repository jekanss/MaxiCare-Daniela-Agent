# La política de tratamiento de datos, congelada por versión

Esto no es documentación: es **prueba**. La Ley 1581 de 2012 no pide que exista una política,
pide poder acreditar *cuál* política vio cada persona el día que aceptó. Un enlace no acredita
nada por sí solo, porque lo que hay detrás puede cambiar sin que el enlace cambie.

Cada fila de `consentimientos` guarda un `politica_version`. Ese identificador tiene que
apuntar a un archivo **inmutable**, y el archivo inmutable vive aquí, en git, donde la fecha
de entrada y cualquier modificación posterior quedan en el historial.

## Versión vigente

| | |
|---|---|
| Identificador (`politica_datos_version`) | `politica-v2.0-2026-09` |
| Versión que declara el documento | 2.0 — Septiembre de 2026 |
| Archivo congelado | [`politica-v2.0-2026-09.pdf`](politica-v2.0-2026-09.pdf) |
| SHA-256 | `7351fb5a8bba5d7fa8b9e5d29f3ddb9b1bf8324356b5d6371f81bf6c6f9c0065` |
| Tamaño | 37 079 bytes · 4 páginas |
| URL que ve el paciente | `https://drive.google.com/file/d/1IB_XYUfc6Dqd51zBeURemfAVQMVnTy28/view` |
| Bajada y verificada | 16/09/2026, sin sesión iniciada |

Comprobar que lo servido sigue siendo lo acreditado:

```bash
curl -sL "https://drive.usercontent.google.com/download?id=1IB_XYUfc6Dqd51zBeURemfAVQMVnTy28&export=download" | sha256sum
```

Si esa huella no coincide con la de la tabla, **alguien cambió el documento sin cambiar la
versión**, y los consentimientos ya registrados dejaron de apuntar a lo que su titular vio.
No es un fallo del código y ninguna prueba lo caza.

## Por qué hay una copia aquí si ya hay una URL

El destino es Google Drive, y Drive deja subir una versión nueva **sobre el mismo archivo**
sin que el enlace cambie. Eso es cómodo para quien publica y es exactamente lo que rompe el
rastro: quien aceptó en septiembre apuntaría a un texto de diciembre que nunca leyó.

Esta carpeta es la contramedida barata: el archivo tal como estaba el día que se encendió el
aviso, con su huella. Mientras no exista una URL bajo el dominio de MaxiCare con una copia
congelada por versión —lo que sigue siendo lo correcto—, esto es lo que hace acreditable el
`politica_version` que se guarda en cada fila.

Quedan dos riesgos que esta carpeta **no** cubre, y que son del negocio, no del código:

- si alguien mueve o despublica el archivo en Drive, el enlace muere **en silencio** y el
  sistema lo sigue mandando;
- un enlace de `drive.google.com` dentro de un mensaje que pide confianza sobre datos
  personales trabaja en contra de sí mismo.

## Al publicar una versión nueva

1. Se añade el PDF nuevo aquí, **sin tocar ni borrar el viejo**: hay consentimientos vivos
   que apuntan a él.
2. Se actualiza este archivo con la versión nueva, dejando la anterior en una tabla de abajo.
3. Se cambia `politica_datos_version` en `config.py`.
4. Se decide aparte —y hoy no está construido— si a quien ya vio la versión anterior hay que
   volver a enseñarle el aviso. Cambiar la versión **no** lo reenvía.
