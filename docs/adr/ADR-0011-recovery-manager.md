# ADR-0011 — Recovery Manager

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Un crash puede dejar jobs no terminales, `.part`, `.tmp` o discrepancias entre DB y filesystem. Borrarlos ciegamente puede eliminar contenido recuperable.

## Decision

Al arrancar, inspeccionar DB, migraciones, manifests, jobs y temporales; reconciliar filesystem con DB y clasificar elementos como reanudables, reintentables, huérfanos, corruptos o candidatos a limpieza.

## Alternatives Considered

- Borrar todos los temporales al arrancar: puede destruir una descarga reanudable.
- Confiar solo en DB: no detecta archivos publicados/movidos externamente.
- Confiar solo en el filesystem: no reconstruye intento, fuente ni trazabilidad del job.

## Positive Consequences

- Recuperación explicable y con trazabilidad tras interrupciones.
- Limpieza conservadora basada en relación con un job.

## Negative Consequences

- Arranque puede tardar más con muchos temporales.
- Reconciliación debe manejar estados ambiguos sin borrar automáticamente.

## Failure Modes

- DB ilegible, manifest corrupto, archivo bloqueado o clasificación errónea de un temporal.

## Reversal Strategy

Deshabilitar limpieza automática y conservar archivos para diagnóstico. Restaurar backup o reconstruir índice desde CBZ; retomar la política solo con tests de fault injection.
