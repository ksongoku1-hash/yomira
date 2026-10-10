# ADR-0017 — Instancia única

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Dos procesos backend escribiendo la misma DB contradicen el modelo de escritor central y pueden provocar locks o estado inconsistente.

## Decision

Solo una instancia backend gestiona la DB. Una segunda ejecución detecta la instancia existente, abre su UI o termina mostrando un mensaje.

## Alternatives Considered

- Permitir varias instancias: requiere coordinación y escrituras multi-proceso que no están en el diseño.
- Rechazar la segunda instancia sin informar: causa confusión.
- Crear una DB por proceso: fragmenta biblioteca y progreso.

## Positive Consequences

- Mantiene un único propietario de escritura.
- Evita que abrir la app varias veces cree bibliotecas divergentes.

## Negative Consequences

- El descubrimiento de instancia debe sobrevivir a cierre inesperado y PID obsoleto.
- Una instancia trabada puede requerir recuperación antes de reabrir.

## Failure Modes

- Mutex huérfano, detección falsa, instancia activa no localizable o lock liberado antes de cerrar DB.

## Reversal Strategy

Si se necesita concurrencia multi-proceso, crear ADR y demostrar serialización, recuperación y migración de datos antes de quitar el límite.
