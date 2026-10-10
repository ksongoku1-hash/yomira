# ADR-0003 — SQLite en modo WAL

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

La aplicación es local-first y debe mantener una biblioteca recuperable sin requerir un servidor de base de datos externo.

## Decision

Usar SQLite con `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout` configurado y escrituras centralizadas y breves. No diseñar el sistema suponiendo múltiples escritores simultáneos.

## Alternatives Considered

- PostgreSQL local: añade servicio y operación que no son necesarios para el modelo local de V1.
- JSON o archivos sueltos: no proporcionan por sí solos las relaciones, constraints y transacciones requeridas.
- SQLite sin WAL: posible, pero no cumple la configuración especificada.

## Positive Consequences

- Base local de un solo archivo con constraints y transacciones.
- El modo WAL permite lecturas concurrentes con la escritura centralizada.
- El usuario no necesita instalar ni administrar un servidor de base de datos.

## Negative Consequences

- Hay que gestionar correctamente los archivos auxiliares WAL y los bloqueos.
- El modelo de escritor central limita el paralelismo de escritura.

## Failure Modes

- Disco lleno o permisos insuficientes durante una transacción.
- Integridad dañada, lock prolongado o migración incompatible.
- Copia de seguridad tomada de forma inconsistente.

## Reversal Strategy

Exportar y verificar datos antes de cambiar de motor; mantener backup pre-migración y herramientas de reconstrucción desde CBZ. No retirar el motor anterior hasta validar integridad y conteos.
