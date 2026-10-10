# ADR-0070 — Backlog durable y ventana de despacho acotada

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26, conforme a la prohibición de colas infinitas y la prioridad de no perder jobs. No se atribuye esta decisión al usuario.

## Context

K085 exige una cola acotada. Los jobs también deben sobrevivir reinicios, por lo que su estado `QUEUED` ya reside en SQLite. Mantener una copia en memoria de todo el backlog duplicaría estado y permitiría consumo de RAM proporcional al número de jobs.

## Decision

- SQLite es la fuente de verdad del backlog durable. Un job se persiste antes de entrar a la ventana de despacho; una ventana llena no elimina ni cambia el estado `QUEUED`.
- El scheduler pide a `DownloadJobRepository` únicamente un lote limitado, nunca materializa el backlog completo. La ventana de despacho acepta `limit` entre 1 y 8 jobs; 8 es el mayor valor del benchmark de concurrencia K239.
- La consulta inicial ordena por `created_at_us` y `job_id` para que el lote FIFO sea determinista. K086 introduce prioridad; K087 introduce fairness sin retirar el límite de materialización.
- Los jobs que no caben en un lote permanecen persistidos y disponibles para lecturas posteriores. No se rechazan ni borran por saturación de la ventana en memoria.
- La capacidad limitada de esta ADR aplica a objetos cargados en RAM. El backlog persistente es contenido del usuario en SQLite; cualquier fallo de escritura se propaga sin conservar una copia únicamente en memoria. El preflight de almacenamiento para materialización permanece bajo K111.

## Alternatives Considered

- Copiar todos los jobs `QUEUED` a una lista o `asyncio.Queue`: RAM crece con el backlog y la copia puede discrepar de SQLite tras un crash.
- Rechazar o cancelar el job cuando el lote está lleno: pierde una solicitud que ya puede permanecer durable en la base.
- Leer sin `LIMIT` y truncar después: la selección visible es pequeña, pero la consulta ya materializó el backlog en memoria.

## Positive Consequences

- La memoria retenida por cada lectura está limitada por `limit`, incluso con muchos jobs pendientes.
- El cierre inesperado no pierde jobs en espera ni requiere reconstruir una cola desde memoria.
- La consulta entrega orden determinista mientras se implementan prioridad y fairness.

## Negative Consequences

- Se consulta SQLite al llenar nuevas ventanas de despacho; el costo queda acotado por `limit` y por los índices existentes de estado.
- La selección FIFO inicial no satisface por sí sola las reglas finales de prioridad y fairness; K086/K087 las deben añadir.

## Failure Modes

- SQLite no permite leer o escribir: el job no se confirma como creado/cambiado y el error se propaga con la traza del caller.
- El lote solicitado excede `limit` 1–8: se rechaza antes de ejecutar la consulta.
- Se reinicia el proceso con jobs en espera: se vuelven a consultar desde SQLite; no se depende del contenido de una cola volátil.

## Reversal Strategy

Reemplazar la ventana por una cola residente mayor requiere medir RAM con backlog alto y conservar SQLite como única fuente de verdad. No se borran ni se marcan completados jobs para reducir la ventana.
