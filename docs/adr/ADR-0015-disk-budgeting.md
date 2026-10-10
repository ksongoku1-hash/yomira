# ADR-0015 — Presupuesto de disco

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Las descargas y los CBZ temporales pueden llenar el volumen y comprometer biblioteca, DB, WAL y recuperación.

## Decision

Antes de iniciar un lote, estimar descarga, overhead temporal y de CBZ, y comprobar espacio libre y reservas mínimas para DB/WAL, logs, temporales, CBZ y thumbnails. No iniciar si el presupuesto es inseguro.

## Alternatives Considered

- Comprobar espacio solo después del fallo: aumenta riesgo de archivos truncados.
- Usar un límite fijo por job sin reservar otros datos: ignora tamaño real y costes temporales.
- Permitir que el sistema operativo determine el resultado: no ofrece aviso ni trazabilidad de la decisión.

## Positive Consequences

- El usuario recibe una decisión previa antes de una operación costosa.
- Se consideran simultáneamente las áreas que requieren espacio.

## Negative Consequences

- Tamaños remotos pueden ser desconocidos o variar.
- Estimaciones conservadoras pueden posponer trabajos que habrían cabido.

## Failure Modes

- Estimación inferior al tamaño real, volumen cambia/dispositivo se retira, o la reserva no contempla WAL temporal.

## Reversal Strategy

Reducir o recalibrar estimación con mediciones y fault injection; no eliminar la comprobación previa. Cambios en reservas deben preservar margen de DB y cleanup.
