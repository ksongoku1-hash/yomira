# ADR-0069 — Persistencia atómica de transiciones de jobs

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26, conforme a ADR-0007 y a las prioridades aprobadas de integridad y trazabilidad. No se atribuye esta decisión al usuario.

## Context

K043 ya persiste la identidad y el estado actual de `DownloadJob`, y `SQLiteDatabase.write_session()` serializa las escrituras dentro del proceso. K084 necesita cambiar el estado sin sobrescribir una transición concurrente o trabajar sobre una lectura obsoleta.

## Decision

- `DownloadJobRepository` ofrece `add`, `get` y `transition_status` sobre la sesión SQLAlchemy que recibe; no crea sesiones ni confirma transacciones por su cuenta.
- `transition_status` recibe el snapshot `DownloadJob` que observó el caller, el estado destino y el timestamp de cambio.
- La operación vuelve a leer la fila dentro de la transacción. Si no existe, devuelve `None` sin escribir. Si `status` o `updated_at_us` difieren del snapshot, lanza `StaleJobStateError` y no reintenta automáticamente.
- Antes de escribir, valida el destino con `assert_transition_allowed`. Transiciones ilegales lanzan `IllegalJobTransition` sin mutación.
- El cambio usa una actualización compare-and-swap condicionada por `job_id`, `status` y `updated_at_us`; modifica únicamente `status` y `updated_at_us`. Si la condición deja de coincidir, la transacción falla como estado obsoleto.
- El timestamp se normaliza a UTC, debe ser consciente de zona horaria y no puede preceder al `updated_at` actual. La transacción y el bloqueo del escritor permanecen bajo el ownership de `SQLiteDatabase.write_session()`.
- El caller resuelve estados obsoletos releyendo el job y tomando una decisión explícita; no se aplica un target alternativo ni se pierde un estado previo por retry automático.

## Alternatives Considered

- Sobrescribir status sin comprobar el snapshot: permite que una operación retrasada deshaga o salte una transición más reciente.
- Guardar el objeto completo del caller: una snapshot vieja podría restaurar prioridad u otros campos que no forman parte de la transición.
- Abrir y confirmar una sesión dentro del repositorio: evita que el caller agrupe trabajo atómico y rompe el ownership del escritor SQLite central.
- Reintentar automáticamente un conflicto: el target pudo dejar de ser correcto tras cambiar el estado; primero debe releerse y evaluarse.

## Positive Consequences

- Status y `updated_at_us` cambian juntos o no cambian.
- Dos callers con la misma snapshot no pueden confirmar ambos una transición incompatible.
- Los campos ajenos al cambio se conservan y un rollback de la sesión revierte la transición.

## Negative Consequences

- Los callers deben conservar la snapshot observada y resolver un `StaleJobStateError` mediante lectura nueva.
- Un job inexistente se representa con `None`; la capa superior debe clasificarlo con el contrato de error apropiado.

## Failure Modes

- La fila desaparece antes de escribir: la condición compare-and-swap falla y no se confirma el cambio.
- El caller presenta una snapshot vieja: se rechaza el cambio y el estado vigente queda intacto.
- Falla la transacción: SQLite revierte status y timestamp juntos; el caller no reintenta un resultado incierto sin releer.

## Reversal Strategy

Retirar compare-and-swap requiere demostrar que ningún worker conserva snapshots concurrentes y añadir una migración o cambio de servicio solo si cambia el esquema. No se reescriben silenciosamente estados persistidos ni se sustituye el writer central.
