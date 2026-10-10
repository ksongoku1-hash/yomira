# ADR-0026 — Invariantes de dominio y esquema SQLite

## Status

Accepted — decisión de implementación de Fase 4, antes de K035.

## Context

La especificación define las entidades mínimas y exige claves estables, relaciones, constraints, reglas de borrado y timestamps UTC. La capa de dominio debe permanecer independiente de SQLAlchemy. Identidad numérica de capítulos, unidades de fecha y borrado por cascada necesitaban una regla común antes de crear tablas.

## Decision

- Mantener tipos y reglas de dominio independientes de los modelos ORM. SQLAlchemy mapea filas en la capa de persistencia; los repositorios traducen entre filas y entidades.
- Los IDs nuevos son UUIDv4 canónicos, en texto minúsculo con guiones. Ninguna identidad de capítulo se basa en `float`; `chapter_id` es la identidad, `chapter_major` y `chapter_minor` son enteros anulables, y `chapter_sort_key` conserva una secuencia canónica de enteros. Las etiquetas remotas permanecen textuales y no se fusionan por título.
- Los instantes UTC se almacenan como entero Unix en microsegundos y se exponen como fechas conscientes de zona horaria. Se rechazan datetimes ingenuos en las fronteras de persistencia.
- Enums se guardan como texto con `CHECK`; booleanos como enteros 0/1 con `CHECK`; tamaños como enteros no negativos. SHA-256 se valida como 64 caracteres hexadecimales minúsculos. Los campos identificadores y de asociación llevan constraints e índices declarados en el modelo.
- Toda FK se declara explícitamente con `ON DELETE RESTRICT`; la DB nunca borra contenido relacionado en cascada. Las eliminaciones, cuando un caso de uso las habilite, deberán comprobar dependencias y ejecutar una operación explícita y transaccional.
- No se añade unicidad por títulos o etiquetas visibles. Las claves remotas solo son únicas junto con su identidad de fuente cuando el modelo representa una identidad remota única; cada constraint concreta se documenta en el modelo y prueba asociada.
- Las tablas mínimas de la especificación incluyen `exports`, `connector_health`, `metadata_overrides` y `chapter_versions`, aunque no aparezcan como tareas separadas en el plan. Se incorporan en las tareas de dominio relacionadas y K048 verifica el conjunto completo.

## Alternatives Considered

- Modelos ORM como entidades de dominio: acopla el negocio al adapter de SQLite y hace más difícil validar reglas sin abrir DB.
- IDs de fila automáticos: dependen del orden de inserción y pueden cambiar durante reconstrucción o importación.
- `float` para capítulo o porcentaje de progreso: puede redondear valores y no preserva etiquetas como `12.10`.
- `ON DELETE CASCADE`: una eliminación accidental podría borrar progreso, versiones o relaciones sin una decisión explícita.
- Único por título: confunde obras homónimas y mezcla identidades de fuentes distintas.

## Positive Consequences

- Las invariantes sobreviven cambios de motor y son verificables en SQL además de Python.
- Los borrados quedan bloqueados por defecto y pueden ofrecerse posteriormente con un flujo recuperable.
- La estructura cubre todas las tablas mínimas normativas antes de declarar PASS.

## Negative Consequences

- Las restricciones RESTRICT exigen que futuros casos de uso describan su borrado explícito.
- La conversión de timestamps y claves de orden requiere mappers probados y formatos estables.

## Failure Modes

- Una entidad importa SQLAlchemy, una tabla omite FK/constraint, o una migración elimina una relación por cascada.
- Se transforma un valor decimal de capítulo a binario de punto flotante.
- Las claves visibles se usan como identidad o criterio de fusión automática.

## Reversal Strategy

Todo cambio de identidad, formato de timestamp, política de FK o separación de capas requiere migración versionada con backup, comprobación de conteos, `integrity_check` y prueba de restauración.
