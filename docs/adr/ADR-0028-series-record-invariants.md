# ADR-0028 — Invariantes del registro Series

## Status

Accepted — decisión de implementación de K037.

## Context

La especificación enumera los campos de `Series`, pero no define su nulabilidad ni un vocabulario estable para `status` y `language`. El sistema debe conservar metadata sin recortarla y evitar fusionar obras por título. La referencia a la portada depende de la tabla `assets`, que se incorporará en K042.

## Decision

- `series_id`, `canonical_title`, `display_title`, `created_at_us` y `updated_at_us` son obligatorios. El título vacío se rechaza; no se recortan, normalizan ni limitan títulos no vacíos.
- `description`, `status`, `language` y `cover_asset_id` son opcionales. `status` y `language` se guardan como texto libre, sin enum ni normalización que pueda rechazar valores futuros de fuentes.
- `series_id` y `cover_asset_id` son UUIDv4 canónicos. La identidad se valida en el dominio y se guarda en SQLite como texto minúsculo con guiones.
- `created_at_us` y `updated_at_us` guardan Unix UTC en microsegundos como enteros SQLite; `updated_at_us` no puede preceder a `created_at_us`. La DB comprueba el tipo entero además del orden.
- `cover_asset_id` es opcional y queda nulo hasta que exista un Asset asociado. K037 crea la columna nullable; K042 añade la FK a `assets.asset_id` con `ON DELETE RESTRICT`, junto con la tabla padre. Así la metadata ORM no mantiene referencias a tablas aún no registradas.
- `canonical_title` no es único. Se crean índices no únicos sobre `canonical_title` y `updated_at_us` para consultas por título y orden reciente.

## Alternatives Considered

- Enumerar estados e idiomas ahora: excluiría valores válidos que la especificación no enumera.
- Recortar o limitar títulos: cambiaría metadata de origen sin una regla aprobada.
- Hacer únicos títulos o títulos canónicos: mezclaría obras distintas con nombres iguales.
- Permitir que borrar un Asset borre una serie: podría eliminar metadata sin una operación explícita.

## Positive Consequences

- Se conservan fielmente valores de título, descripción, estado e idioma.
- La identidad tiene validación estable en dominio y DB; la relación con la portada se vuelve exigible cuando K042 introduce Assets.
- Las búsquedas por título y las consultas recientes disponen de índices no únicos.

## Negative Consequences

- El texto libre requiere que las futuras fronteras validen longitud y formato de transporte sin modificar el valor persistido.
- Los índices ocupan espacio y deben mantenerse durante escrituras.

## Failure Modes

- La capa de dominio acepta un UUID que SQLite rechaza o viceversa.
- Un cambio de metadata sobrescribe el texto original mediante trimming o normalización.
- Una migración posterior elimina el `RESTRICT` o hace único el título.

## Reversal Strategy

Cambiar nulabilidad, formato de IDs, normalización, índices o política de portada requiere una nueva ADR, migración Alembic con backup y pruebas de conservación de valores existentes.
