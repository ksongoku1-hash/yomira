# ADR-0032 — Identidad y disponibilidad de ChapterSource

## Status

Accepted — decisión de implementación de K041.

## Context

La especificación define `ChapterSource` como una versión concreta de un capítulo en una fuente, con referencias remotas, etiqueta, disponibilidad y última observación. No define nulabilidad ni el alcance de unicidad de `remote_id` o `remote_url`. Los IDs de capítulo de algunos conectores pueden ser locales a una serie, por lo que no es seguro asumir unicidad en toda una fuente.

## Decision

- `chapter_source_id` es UUIDv4 canónico. `chapter_id` y `source_id` son FKs con `ON DELETE RESTRICT`.
- `remote_id` y `remote_url` son texto opaco anulable; cada valor presente debe ser no vacío y al menos uno debe existir. No se recortan, normalizan ni parsean.
- `remote_label` es anulable. Si está presente, se conserva exactamente, incluido texto vacío. No es identidad ni criterio de asociación automática.
- `available` es un booleano obligatorio almacenado como 0/1. K041 no fuerza una relación entre disponibilidad y `last_seen_at`; ambos reflejan datos explícitos de la observación y pueden representar una entrada local aún no revisada.
- `last_seen_at` es anulable hasta la primera observación, debe tener zona horaria si existe y se persiste como Unix UTC en microsegundos.
- No se añade unicidad sobre IDs, URLs, etiquetas, `chapter_id` o `source_id`. El alcance de los IDs remotos depende del conector y el modelo admite varias versiones concretas por capítulo y fuente. K041 conserva cada fila explícita y no sobrescribe ni fusiona duplicados. Un contrato de conector podrá definir una identidad y restricción más específica mediante una ADR y migración posterior.
- Se indexan `chapter_id` y `source_id`. No se indexa `available`: es de baja cardinalidad y sus cambios escribirían un índice adicional con poco beneficio selectivo.
- No se añaden timestamps de creación/modificación ausentes de la lista normativa.

## Alternatives Considered

- Hacer único `(source_id, remote_id)`: puede rechazar IDs de capítulo que solo son únicos dentro de una serie.
- Hacer único `(chapter_id, source_id)`: impide conservar versiones alternativas del mismo capítulo en una fuente.
- Usar `remote_label` como clave: las etiquetas cambian, se repiten y pueden tener distintas formas.
- `ON DELETE CASCADE`: puede borrar datos de fuente sin una operación explícita.
- Inferir `available` desde `last_seen_at`: confunde una observación ausente con una observación negativa.

## Positive Consequences

- Los datos de conectores quedan preservados aunque sus IDs tengan ámbitos distintos.
- Las etiquetas y observaciones se conservan sin transformaciones.
- La eliminación de un padre nunca borra versiones de capítulo implícitamente.

## Negative Consequences

- Pueden existir referencias remotas repetidas o ambiguas hasta que el conector declare su gramática y alcance.
- La capa de sync futura deberá reportar conflictos y no resolverlos con una fusión destructiva.

## Failure Modes

- Un conector vuelve a numerar capítulos y un servicio sobrescribe referencias históricas.
- Se interpreta el último avistamiento como disponibilidad actual sin una observación explícita.
- Un borrado ignora el bloqueo de FK en lugar de ejecutar un flujo explícito.

## Reversal Strategy

Cambiar identidad, unicidad, nulabilidad, semántica de disponibilidad o timestamps requiere una ADR nueva, migración versionada y pruebas de conflicto, integridad referencial y restauración.
