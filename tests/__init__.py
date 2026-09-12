"""Pruebas del proyecto.

Estructura sugerida, siguiendo la separación de `src/<paquete>/`: `test_contratos.py`
prueba los modelos Pydantic con datos de ejemplo (sin red); `test_herramientas.py` prueba
cada tool contra un doble de prueba del sistema externo que toque (nunca contra el sistema
real del cliente); `test_agentes.py` construye cada `Agent` y verifica su forma (nombre,
tools, output_type) sin llamar al modelo; y las pruebas que sí llaman al modelo real, si
las hay, quedan aparte y fuera de la corrida por defecto, igual que en las skills del
plugin marcan sus propios ejemplos con `# verificar: solo-compilar`.
"""
