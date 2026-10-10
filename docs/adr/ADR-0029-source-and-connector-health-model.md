# ADR-0029 — Identidad de fuentes y datos de salud de conectores

## Status

Accepted — decisión de implementación de K038.

## Context

La especificación define los campos de `Source`, los estados de salud y los estados del circuito, pero no define el formato de `connector_id`, si un connector puede tener varias configuraciones locales ni dónde persisten los últimos éxitos y fallos. El esquema mínimo requiere tanto `sources` como `connector_health`.

## Decision

- Cada `Source` representa una instancia local configurada. `source_id` es UUIDv4; `connector_id` y `connector_version` son texto opaco, requerido y no vacío. No se normalizan ni se hacen únicos, para permitir varias configuraciones del mismo connector.
- `display_name` es texto requerido y no vacío; no se recorta ni limita en persistencia. `enabled` se guarda como booleano 0/1. La combinación entre `enabled` y `health_status` no tiene una regla adicional en DB; el servicio de fuentes decide las transiciones explícitas.
- `health_status` es el estado actual único de la fuente y solo acepta `HEALTHY`, `DEGRADED`, `BROKEN`, `DISABLED` o `INCOMPATIBLE`, según la especificación.
- `connector_health` tiene una fila como máximo por fuente, identificada por `source_id` con FK `ON DELETE RESTRICT`. Guarda `last_success_at_us`, `last_failure_at_us`, `circuit_state` y `updated_at_us`; el estado actual se lee de `sources.health_status` para evitar dos copias divergentes.
- `circuit_state` acepta únicamente `CLOSED`, `OPEN` o `HALF_OPEN`. La lógica de umbrales, cooldown y transición corresponde a K104, no a K038.
- Los timestamps se guardan en Unix UTC en microsegundos enteros. Instantes de última actividad son opcionales hasta que ocurra la observación; las fechas de creación y actualización de `Source` son obligatorias.
- Se indexan `connector_id`, `enabled` y `health_status` para consultar y filtrar las fuentes. No se persisten tokens ni cookies en estas tablas.

## Alternatives Considered

- Usar `connector_id` como PK: impediría dos configuraciones locales del mismo connector.
- Usar valores de health status libres: aceptaría estados que el contrato de la especificación no define.
- Duplicar `current_status` en `connector_health`: permitiría que Source y Health discrepen después de un fallo parcial.
- Eliminar en cascada el health al borrar Source: ocultaría historial operativo por un borrado implícito.
- Validar semver o normalizar identificadores: la especificación no fija sus gramáticas.

## Positive Consequences

- La identidad de una configuración queda separada de la identidad de su connector.
- Los estados válidos se verifican tanto en Python como en SQLite.
- La DB puede conservar timestamps y circuito sin duplicar el estado actual.

## Negative Consequences

- Los servicios deben actualizar Source y su observación de salud en una transacción cuando ambos cambien.
- El parser de versiones no se implementa hasta que el contrato de connector defina su formato.

## Failure Modes

- Una transición guarda la salud actual en la tabla equivocada o actualiza campos en transacciones distintas.
- Se reutiliza `connector_id` como identidad de instancia y se mezclan configuraciones.
- Se persiste una cookie o token en metadata de Source.

## Reversal Strategy

Cambiar identidad, vocabularios, nulabilidad, relación Source/Health o reglas de borrado requiere una nueva ADR, migración con backup y pruebas de consistencia y restauración.
