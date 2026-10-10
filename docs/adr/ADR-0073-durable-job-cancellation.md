# ADR-0073 — Intención durable y puntos seguros de cancelación

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26 para ejecutar K088 conforme a la sección 19 de la especificación y ADR-0068. No se atribuye esta decisión al usuario.

## Context

La especificación requiere cancelar inmediatamente `QUEUED` y `FETCHING_METADATA`, pero detener nuevas solicitudes y cerrar streams en `DOWNLOADING`, diferir validación/procesamiento/verificación hasta puntos seguros, terminar o limpiar escrituras críticas en `PACKAGING` y no cancelar una transacción de base de datos en `INDEXING`.

Una petición diferida que solo exista en memoria puede desaparecer tras un cierre abrupto y el Recovery Manager podría reanudar el trabajo que el usuario pidió cancelar. El estado `CANCELLED` tampoco debe persistirse antes de detener las operaciones protegidas.

## Decision

- Añadir `cancel_requested_at_us` nullable al registro durable del job. `NULL` significa que no hay una petición pendiente. La fecha se fija una sola vez y no se borra; jobs existentes migran con `NULL`.
- `request_cancellation` usa compare-and-swap. En `QUEUED` y `FETCHING_METADATA` escribe la intención y pasa inmediatamente a `CANCELLED`. En las otras etapas activas conserva el estado y registra la intención para que el worker la atienda en el próximo límite seguro.
- La intención repetida es idempotente. `COMPLETED`, `FAILED` e `INTERRUPTED` no aceptan nuevas peticiones; `CANCELLED` devuelve su estado actual. Reintentar `FAILED` o `CANCELLED` crea otro `job_id`, conforme a ADR-0068.
- `complete_cancellation` es la única transición genérica del repositorio a `CANCELLED`. Exige intención durable y evidencia explícita de seguridad por etapa:
  - `DOWNLOADING`: admisión de solicitudes detenida y stream cerrado.
  - `VALIDATING` y `VERIFYING`: punto seguro alcanzado.
  - `PROCESSING`: límite entre páginas alcanzado.
  - `PACKAGING`: escritura crítica inactiva y cleanup completo.
  - `INDEXING`: transacción DB cerrada.
- Mientras existe una intención, se rechaza avanzar por la ruta normal de éxito. Se permite registrar un fallo real o una interrupción; la intención persiste. K091/K094 deben resolverla durante recovery antes de reanudar, después de reconciliar y limpiar los artefactos de forma segura.
- La evidencia de seguridad se valida en dominio antes de cualquier actualización. Una condición incompleta deja status, timestamps e intención exactamente como estaban.
- El campo de intención no contiene texto libre ni secretos; el error/diagnóstico de cancelación se registra en los sistemas posteriores de K095 sin incluir referencias sensibles.

## Alternatives Considered

- Guardar la petición solo en un `Event`/`asyncio.Task`: se pierde en un cierre abrupto y no permite distinguir una reanudación autorizada de una cancelación pendiente.
- Marcar `CANCELLED` en cuanto llega el request para cualquier etapa: declararía terminado el job mientras aún puede escribir, mantener un stream o estar dentro de una transacción.
- Borrar el campo al cancelar: elimina evidencia de por qué el job terminó y complica auditoría/recovery.
- Permitir `transition_status(..., CANCELLED)` sin intención ni evidencia: permite saltarse cleanup y los límites críticos de ADR-0068.
- Cancelar `INTERRUPTED` directamente por la ruta genérica: contradice ADR-0068, que reserva la salida de ese estado al Recovery Manager.

## Positive Consequences

- La intención de usuario sobrevive un reinicio y no se confunde con el estado de ejecución.
- El estado terminal solo se escribe cuando las operaciones protegidas cumplen las condiciones de limpieza y seguridad.
- Las peticiones repetidas y las escrituras concurrentes tienen resultados deterministas mediante CAS.

## Negative Consequences

- Un job puede permanecer en su etapa actual con `cancel_requested_at_us` mientras alcanza un punto seguro.
- K091/K094 deben integrar explícitamente la intención pendiente con recovery antes de permitir la reanudación.
- Cada worker por etapa debe producir evidencia veraz; marcar un punto seguro sin completar cleanup es un defecto de integridad.

## Failure Modes

- Request sobre snapshot obsoleta: lanza `StaleJobStateError`; el caller relee y decide otra vez.
- Escritura SQLite fallida: no se considera aceptada la petición; status e intención se revierten juntos.
- Evidencia incompleta: no cambia ninguna fila y el worker conserva la petición pendiente.
- Crash tras aceptación: SQLite conserva intención y etapa; Recovery Manager reconcilia antes de continuar.

## Reversal Strategy

Quitar o modificar el campo requiere migración nueva y una política explícita para convertir todas las peticiones pendientes; nunca se descartan sin registrar su resolución. Cambiar los puntos seguros requiere ADR nueva y pruebas para cada etapa protegida.
