# ADR-0014 — Versionado de capítulos

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Un capítulo puede existir en varias fuentes o actualizarse; reemplazarlo sin trazabilidad puede destruir una versión válida.

## Decision

Mantener versiones como `ACTIVE`, `ALTERNATE`, `SUPERSEDED` o `INVALID`. Validar la nueva versión y publicarla atómicamente; registrar la versión anterior como reemplazada o alternativa.

## Alternatives Considered

- Un solo archivo sobrescrito: pierde la versión previa.
- Duplicados con sufijos arbitrarios: no representan identidad/versiones.
- Fusionar capítulos por título: no basta para establecer identidad.

## Positive Consequences

- Se conserva trazabilidad de fuente y cambios.
- El usuario puede distinguir una alternativa de una versión activa.

## Negative Consequences

- Aumentan metadatos e interfaz de resolución.
- Retener alternativas consume espacio y necesita política explícita.

## Failure Modes

- Dos jobs publican al mismo target, fuente reasignada incorrectamente o nueva versión inválida marcada activa.

## Reversal Strategy

No borrar versiones anteriores automáticamente. Si cambia el modelo, exportar vínculos y estados a un esquema nuevo con backup y verificación de cada CBZ.
