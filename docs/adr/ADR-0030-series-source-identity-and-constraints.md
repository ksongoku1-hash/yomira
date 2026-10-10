# ADR-0030 — Identidad y constraints de SeriesSource

## Status

Accepted — decisión de implementación de K039.

## Context

La especificación define los seis campos de `SeriesSource` y prohíbe fusionar automáticamente por título, pero no define nulabilidad, normalización, unicidad de referencias remotas ni borrado de filas relacionadas. Esas reglas determinan si una obra de una fuente puede quedar asociada a varias series locales o si un registro puede existir sin una referencia remota estable.

## Decision

- `series_source_id` es UUIDv4 canónico. `series_id` y `source_id` son FKs con `ON DELETE RESTRICT`; la base nunca elimina asociaciones implícitamente.
- `remote_id` y `remote_url` son texto opaco y anulable porque distintos conectores pueden proporcionar una referencia, la otra o ambas. Cada valor presente debe ser no vacío. Al menos uno debe estar presente para que la asociación conserve una referencia identificable además del título.
- Los valores remotos se persisten exactamente como se reciben, sin recorte, normalización, parsing ni cambio de mayúsculas.
- `remote_title` es anulable y se conserva exactamente, incluido un valor vacío recibido. No es clave ni participa en unicidad o fusión.
- `last_checked_at` es anulable hasta que ocurra una comprobación. Cuando existe, debe ser una fecha consciente de zona horaria y se guarda como Unix UTC en microsegundos.
- Se crean índices únicos parciales por `(source_id, remote_id)` y `(source_id, remote_url)` cuando la referencia correspondiente está presente. Así una misma obra remota de una fuente no puede terminar asociada silenciosamente a varias filas/series locales. No se añade unicidad a `(series_id, source_id)`, para permitir más de una obra remota de la misma fuente asociada a una serie local.
- K039 solo persiste asociaciones explícitas. No añade matching, fusión ni asociación automática por título.
- No se añaden timestamps de creación/actualización: no aparecen entre los campos de `SeriesSource` en la especificación.

## Alternatives Considered

- Hacer obligatorios `remote_id` y `remote_url`: descarta fuentes que solo ofrecen una de esas referencias.
- Permitir ambos ausentes: dejaría una asociación sin identidad remota estable y haría imposible detectar duplicados sin recurrir al título.
- Unicidad por título: puede confundir obras distintas y contradice la prohibición de fusión automática por título.
- Unicidad por `(series_id, source_id)`: impide que una obra local represente más de una obra remota de una fuente.
- `ON DELETE CASCADE`: convierte un borrado local en pérdida implícita de la asociación.

## Positive Consequences

- Las asociaciones conservan las referencias originales y sus timestamps sin transformaciones con pérdida.
- Las restricciones detectan duplicados remotos dentro de una fuente antes de que una capa superior pueda sobrescribir o fusionar datos.
- El título queda fuera de las decisiones de identidad.

## Negative Consequences

- Un conector que no ofrezca ID ni URL no puede persistir una asociación hasta que exista una referencia estable o una decisión arquitectónica nueva.
- Los aliases remotos con distinto ID y distinta URL no se reconocen como la misma obra en K039.
- Los conflictos de unicidad deben mostrarse como error explícito; K039 no los resuelve automáticamente.

## Failure Modes

- Se normaliza `remote_id` o `remote_url` y cambia la identidad que asigna el conector.
- Se usa el título como clave o criterio automático de fusión.
- Una migración futura cambia la unicidad o nulabilidad sin reconciliar asociaciones existentes.

## Reversal Strategy

Cambiar nulabilidad, unicidad, tratamiento de texto o reglas de borrado requiere una nueva ADR y una migración versionada con verificación de duplicados, integridad referencial y restauración.
