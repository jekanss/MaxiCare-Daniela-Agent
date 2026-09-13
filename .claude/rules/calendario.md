---
paths:
  - "src/maxicare_daniela/calendario.py"
  - "src/maxicare_daniela/herramientas.py"
  - "scripts/probar_calendario.py"
  - "tests/test_calendario_google.py"
---

# Google Calendar

El `CLAUDE.md` raíz lleva la regla en una línea. Aquí está el porqué.

- **Una cita de Daniela NO es un bloqueo del doctor.** Es la trampa central de
  `CalendarioGoogle` y con `CalendarioDoble` era invisible: el doble guarda eventos y
  bloqueos en listas separadas, Google los devuelve juntos. Sin filtrarlos, la primera cita
  de una hora taparía el bloque y la clínica atendería **uno** por hora en vez de dos. Cada
  evento que crea Daniela lleva `extendedProperties.private.origen = "daniela"` y
  `bloqueos()` lo descarta. Un evento sin marca es de los doctores y sí tapa.
- **Google Calendar manda también al ESCRIBIR, no solo al ofrecer.**
  `consultar_disponibilidad` respeta los bloqueos desde la fase 3, pero hasta el 13/09/2026
  `crear_cita` y `reprogramar_cita` no los miraban: comprobaban la hora pasada y el cupo de
  Neon, y se iban derechas a `crear_evento`. Dos caminos llegaban ahí con una hora que el
  doctor había apartado —el paciente que pide «las 3» y el modelo que agenda sin consultar
  antes, y el doctor que bloquea DESPUÉS de que Daniela ofreció esa hora, que en WhatsApp
  son minutos—. El guardia es `_bloqueo_que_tapa`, y va **antes de tocar la base**: un cupo
  consumido por una cita que nunca se pudo crear hay que ir a devolverlo. El mensaje no
  nombra el título del evento: es la agenda privada del doctor.
- **No hay caché de bloqueos, y es deliberado.** Cada consulta le pregunta a Google, que es
  lo que hace que borrar el evento libere la hora sin sincronizar nada. Los pasos 6b y 6c de
  `probar_calendario.py` existen para cazar a quien meta uno «para bajar la latencia».
- **La credencial es `MAXICARE_GOOGLE_SA_B64`, no una ruta a un archivo.** `config.py`
  decía `MAXICARE_GOOGLE_CREDENTIALS_PATH`, que no existía en ningún `.env`; nadie lo notó
  porque ningún módulo leía ese campo. El nombre correcto es el que documenta `.env.ejemplo`.
- **El 404 casi nunca es el código: es el permiso.** La cuenta de servicio se autentica
  perfectamente aunque no tenga acceso a nada. Hay que compartir el calendario con su
  `client_email` dándole «Hacer cambios en los eventos». `CalendarioGoogle` lo comprueba
  **al construirse**, con una lectura real, y el mensaje nombra el correo.
- **El calendario de la clínica es una cuenta personal de Gmail, y es una decisión tomada
  a conciencia** (MaxiCare, 12/09/2026: «sí va a ser ese correo, no pasa nada»). Lo que
  cuesta: las citas de los pacientes conviven con la agenda personal de esa persona —ya
  hubo un evento suyo borrado a mano durante una prueba— y el día que esa cuenta no esté,
  el calendario se va con ella. La salida, si algún día deja de ser aceptable, es barata:
  crear un calendario secundario, compartirlo con la misma cuenta de servicio y cambiar
  `MAXICARE_GOOGLE_CALENDAR_ID`. Ni una línea de código cambia.
- **El chat web sigue con `CalendarioDoble` a propósito.** Probar en la pestaña Pruebas
  crearía eventos falsos en el calendario donde los doctores miran su día —el mismo error
  de categoría que `public` vs `pruebas_web`—. En WhatsApp sí entra el de verdad:
  `calendario_desde_config`, construido una vez al arrancar `runtime.py`.
- `ZONA_BOGOTA` vive en `calendario.py` y `herramientas.py` la reexporta. Una sola
  definición: dos copias de un desfase horario son dos cosas que un día divergen.

Cuando Calendar «no funciona», lo primero es `uv run python scripts/probar_calendario.py
--diagnosticar`: solo lee, comprueba el acceso y lista los bloqueos de los próximos 14 días.
