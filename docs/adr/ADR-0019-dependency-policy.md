# ADR-0019 — Política de dependencias

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

El instalador contiene runtimes y librerías; versiones, licencias, vulnerabilidades y peso afectan confiabilidad y distribución.

## Decision

Toda dependencia nueva debe justificar necesidad, mantenimiento, licencia, peso, seguridad y alternativa estándar. Mantener lockfiles; fijar versiones y generar SBOM para release.

## Alternatives Considered

- Añadir paquetes sin revisión: acelera prototipos, pero hace impredecible el artefacto.
- Evitar dependencias externas por completo: puede duplicar código de alto riesgo.
- Actualizar siempre a la versión más reciente: puede introducir cambios no probados.

## Positive Consequences

- Inventario reproducible de componentes empaquetados.
- La evaluación ocurre antes de que una dependencia se vuelva crítica.

## Negative Consequences

- Añade tiempo a adopción y actualización.
- Lockfiles requieren renovación y pruebas de compatibilidad.

## Failure Modes

- Dependencia abandonada o vulnerable, conflicto de licencia, drift entre entorno y paquete o runtime ausente.

## Reversal Strategy

Retirar o sustituir una dependencia mediante lockfile nuevo, SBOM actualizado y prueba de clean machine. Mantener versión anterior para rollback del release cuando sea seguro.
