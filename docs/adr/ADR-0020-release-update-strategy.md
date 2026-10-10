# ADR-0020 — Releases, actualización y rollback

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Una actualización puede fallar en instalación, migración o self-test; un downgrade puede destruir compatibilidad de DB.

## Decision

V1 usa actualización manual. Antes de instalar: preflight y backup; después: instalación, migración, self-test y verificación de compatibilidad. Mantener rollback recuperable y bloquear downgrade destructivo. Evaluar firma antes de distribución pública.

## Alternatives Considered

- Actualización automática en segundo plano: no es requisito V1 y complica autorización y rollback.
- Reinstalar sin backup: expone biblioteca y DB a fallos de migración.
- Permitir cualquier downgrade: una versión anterior puede no entender el schema nuevo.

## Positive Consequences

- El usuario controla cuándo actualizar.
- Un fallo de migración tiene ruta de recuperación definida.

## Negative Consequences

- El usuario debe obtener e instalar manualmente el paquete.
- Rollback requiere conservar artefactos y backups compatibles.

## Failure Modes

- Backup no verificable, instalador interrumpido, migración parcial, downgrade incompatible o versiones de frontend/backend que no coinciden.

## Reversal Strategy

Restaurar backup pre-upgrade junto a binarios compatibles; no degradar DB a ciegas. Automatizar actualizaciones solo con ADR nuevo, firma/validación del artefacto y pruebas de upgrade/rollback.
