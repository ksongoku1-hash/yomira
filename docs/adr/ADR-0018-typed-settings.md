# ADR-0018 — Configuración tipada

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Timeouts, concurrencia, caché, LAN y modo del lector cambian el comportamiento y necesitan valores válidos y visibles.

## Decision

Mantener settings centrales tipados; cada uno declara tipo, default, mínimo/máximo, si requiere reinicio y si es editable por el usuario. Validar inputs y versiones al leerlos.

## Alternatives Considered

- Variables sueltas por módulo: crea defaults y validaciones divergentes.
- Texto libre en archivo de configuración: permite valores mal formados.
- Defaults no documentados: dificulta diagnóstico y Support Bundle.

## Positive Consequences

- Límites de recursos configurables y trazables.
- Startup self-test puede detectar configuración inválida antes de jobs.

## Negative Consequences

- El esquema de settings debe versionarse y migrarse.
- Cambiar rangos puede invalidar preferencias guardadas.

## Failure Modes

- Unidad incorrecta, setting fuera de rango, defaults inconsistentes o secreto guardado en texto plano.

## Reversal Strategy

Versionar y migrar settings; retener backup previo y presentar valores que no puedan convertirse. Las credenciales no se migran a texto plano.
