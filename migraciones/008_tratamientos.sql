-- =========================================================================================
-- El vocabulario de tratamientos -- editable por la clínica
--
-- Hasta ahora la lista vivía SOLO en el `Literal` de `contratos.py`, y agregar uno nuevo
-- exigía un cambio de código y un despliegue. La condición de revisión de la fase 1 --«si
-- el Literal de tratamientos resulta insuficiente»-- se disparó: no por un dato que
-- faltara, sino porque el cliente tiene que poder crecer sin nosotros.
--
-- Lo que esta tabla NO abre: `LecturaArchivo.tratamiento`, que sigue con el Literal escrito
-- a mano. Ese es el muro entre el contenido clínico y el paciente, y no se abre desde una
-- pantalla. Un tratamiento creado aquí se puede cotizar y agendar; una radiografía sobre él
-- se clasifica `no_identificado` hasta que se incorpore al Literal.
--
-- La `clave` la restringe Postgres y no solo Python: es el string que termina dentro de
-- `citas.tratamiento` y en los argumentos que ve el modelo. Una frase clínica no pasa el
-- CHECK.
-- =========================================================================================

CREATE TABLE IF NOT EXISTS tratamientos (
    clave      TEXT PRIMARY KEY CHECK (clave ~ '^[a-z][a-z0-9_]{2,23}$'),
    etiqueta   TEXT        NOT NULL,
    activo     BOOLEAN     NOT NULL DEFAULT TRUE,
    creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    creado_por TEXT
);

-- Los catorce del Literal. `ON CONFLICT DO NOTHING` para no pisar una etiqueta que la
-- clínica ya haya editado.
INSERT INTO tratamientos (clave, etiqueta, creado_por) VALUES
    ('cordales',        'Cordales',              'semilla'),
    ('implantes',       'Implantes',             'semilla'),
    ('blanqueamiento',  'Blanqueamiento',        'semilla'),
    ('ortodoncia',      'Ortodoncia',            'semilla'),
    ('diseno_sonrisa',  'Diseño de sonrisa',     'semilla'),
    ('microdiseno',     'Microdiseño',           'semilla'),
    ('coronas',         'Coronas',               'semilla'),
    ('periodoncia',     'Periodoncia',           'semilla'),
    ('gingivectomia',   'Gingivectomía',         'semilla'),
    ('bichectomia',     'Bichectomía',           'semilla'),
    ('limpieza',        'Limpieza',              'semilla'),
    ('endodoncia',      'Endodoncia',            'semilla'),
    ('protesis',        'Prótesis',              'semilla'),
    ('no_identificado', 'Sin identificar',       'semilla')
ON CONFLICT (clave) DO NOTHING;
