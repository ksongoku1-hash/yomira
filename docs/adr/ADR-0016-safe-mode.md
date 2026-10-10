# ADR-0016 — Safe Mode

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Con DB corrupta, migración fallida, permisos críticos o rutas inaccesibles, operaciones normales podrían empeorar el daño.

## Decision

Arrancar en modo limitado cuando falle una comprobación crítica. Permitir diagnóstico, restaurar backup, cambiar library root, reconstruir biblioteca y generar Support Bundle; bloquear nuevas descargas.

## Alternatives Considered

- Fallar el arranque por completo: impide herramientas de reparación.
- Continuar como si todo estuviera listo: riesgo de escribir sobre estado inválido.
- Reparar DB automáticamente sin inspección: podría ocultar o agravar corrupción.

## Positive Consequences

- Ofrece una ruta de recuperación sin habilitar operaciones dañinas.
- Expone la causa y estado de readiness al usuario.

## Negative Consequences

- Requiere separar readiness normal de acceso a herramientas de recuperación.
- Algunos fallos de permisos pueden impedir incluso exportar diagnósticos.

## Failure Modes

- Se activa tarde, habilita por error una descarga o no permite leer el backup de recuperación.

## Reversal Strategy

No eliminar las capacidades de Safe Mode. Cambios de restricciones requieren matriz de capacidades y tests que demuestren que DB/rutas inválidas no habilitan operaciones normales.
