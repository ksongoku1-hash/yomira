# ADR-0074 — Pausa temporal cooperativa de jobs

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26 para ejecutar K089 conforme a ADR-0068 y a las prioridades aprobadas de integridad, recuperación y recursos acotados. No se atribuye esta decisión al usuario.

## Context

ADR-0068 establece que una pausa temporal no cambia el estado persistido del job. K089 necesita permitir una pausa y reanudación durante una ejecución sin declarar una etapa completa, perder artefactos ni interferir con la cancelación durable de K088.

La ventana de despacho y el máximo de concurrencia están acotados a ocho jobs. Un registro de pausa por cada job en cola crecería con el backlog; duplicar el estado duradero de SQLite en memoria también podría divergir después de un cierre abrupto.

## Decision

- Implementar un `JobPauseController` volátil y confinado a un único event loop de `asyncio`. Mantiene un registro por job activo, con capacidad máxima de ocho, y no registra jobs `QUEUED` ni guarda una copia del backlog.
- La pausa es una superposición temporal (`RUNNING`, `PAUSE_REQUESTED`, `PAUSED` o `CANCELLATION_REQUESTED`). No cambia `DownloadJob.status`, timestamps, manifests ni filas SQLite. Un reinicio descarta esa superposición; K091/K094 recuperan el estado durable conforme a ADR-0068.
- `request_pause` es idempotente. Si aún no se alcanzó un límite seguro, conserva `PAUSE_REQUESTED` y el worker continúa hasta el siguiente límite. El worker solo espera cuando `pause_if_requested` recibe evidencia válida de seguridad para la etapa actual.
- Los límites seguros son: `FETCHING_METADATA`, sin operación de red activa; `DOWNLOADING`, sin nuevas solicitudes y con stream cerrado; `VALIDATING`/`VERIFYING`, en punto seguro; `PROCESSING`, entre páginas; `PACKAGING`, fuera de escritura crítica y con cleanup de checkpoint completo; `INDEXING`, fuera de una transacción DB. Estados terminales e `INTERRUPTED` no admiten pausa.
- `resume` despierta al worker y vuelve a `RUNNING`; no avanza la etapa durable. El permiso de admisión global/source/host permanece reservado mientras el job está pausado para impedir que una reanudación exceda cuotas o se duplique. El worker espera de forma asíncrona y no ocupa CPU durante la pausa.
- Si el job llega a un estado terminal antes del siguiente checkpoint seguro, la finalización gana; al terminar el worker se descarta la petición de pausa no atendida y la capa superior relee el estado durable.
- La cancelación durable tiene precedencia sobre pausa. Tras confirmar `request_cancellation` en SQLite, la capa de aplicación notifica al controller; este despierta al worker con `CANCELLATION_REQUESTED`, que termina la pausa y continúa hacia el safe point de cancelación de K088. Un fallo de persistencia de cancelación no se comunica como cancelación aceptada.
- El registro se elimina al terminar el worker; la eliminación despierta como cancelado cualquier checkpoint que estuviera esperando. Los identificadores desconocidos, duplicados o una capacidad excedida producen errores explícitos y no modifican otros jobs.

## Alternatives Considered

- Persistir un nuevo estado `PAUSED`: contradice ADR-0068 y exigiría ampliar la máquina de estados, migración y estrategia de recuperación.
- Pausar jobs `QUEUED` con una colección en memoria: su tamaño dependería del backlog; una colección durable requeriría otra decisión sobre schema, fairness y recuperación.
- Bloquear un thread con `Event.wait`: puede congelar el event loop de red y retrasar otros jobs.
- Liberar el permiso de concurrencia al pausar: permitiría más trabajo concurrente al reanudar y exigiría integrar un protocolo de reacquisición y fairness. Se mantiene el permiso hasta que el job sale.
- Interrumpir inmediatamente cualquier etapa: podría cortar streams, escrituras o transacciones protegidas.

## Positive Consequences

- La pausa no inventa estados persistidos ni cambia la semántica de `INTERRUPTED`.
- La memoria del controller está limitada por el máximo de jobs activos y no crece con la cola durable.
- Cancelación y pausa no pueden dejar un worker suspendido ignorando una solicitud durable.
- La espera cooperativa mantiene libre el event loop mientras se pausa el trabajo.

## Negative Consequences

- Un job pausado conserva su permiso de concurrencia hasta que se reanuda o termina, por lo que reduce una plaza disponible.
- No se puede pausar individualmente un job que siga `QUEUED`; esa política requiere una cola durable y una ADR específica.
- La pausa desaparece tras reinicio y no promete continuar pausada después de recovery.

## Failure Modes

- Evidencia insuficiente en un checkpoint: se conserva `PAUSE_REQUESTED`, el worker continúa y vuelve a revisar en el próximo límite seguro.
- Capacidad llena o identificador no registrado: se devuelve error explícito; ningún otro registro se modifica.
- Resume o cancel llega mientras el worker espera: el evento asyncio lo despierta sin bloquear el event loop.
- Cancelación durable falla: la capa de aplicación no llama a `notify_cancellation_requested`; no se anuncia aceptación.
- Proceso termina mientras un job está pausado: la pausa volátil desaparece y recovery reconcilia el job y sus artefactos antes de reanudar.

## Reversal Strategy

Cambiar la pausa a estado durable, permitir pausar jobs en cola o liberar permisos durante la pausa requiere una ADR nueva, definición de recuperación/fairness y pruebas de límites, concurrencia, cancelación e integridad de artefactos. La reversión del controller no elimina ni reescribe datos de jobs.
