# ADR-0006 — Aislamiento de conectores de fuentes

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Las fuentes remotas son cambiantes y no confiables. Un parser de fuente no debe poder corromper la biblioteca ni asumir coordinación de jobs.

## Decision

Un connector implementa health check, búsqueda, metadatos, listado y páginas según sus capacidades. Devuelve datos normalizados al core; no escribe en `library/`, no ejecuta SQL arbitrario, no genera CBZ ni decide estados del Job Manager.

## Alternatives Considered

- Plugins con acceso completo al proceso: simplifican integraciones, pero amplían el impacto de un bug del parser.
- Lógica de fuente dentro del downloader: mezcla parsing remoto con persistencia y empaquetado.
- Un formato específico por fuente en todo el core: acopla cada módulo a cambios remotos.

## Positive Consequences

- Se puede sustituir o deshabilitar una fuente sin delegarle escritura local.
- Los fixtures permiten probar conectores sin Internet.

## Negative Consequences

- Hace falta un contrato y adaptación de datos por connector.
- Cambios de schema remoto requieren actualizar y validar cada integración.

## Failure Modes

- HTML cambiado, metadata maliciosa, timeout, fuente incompatible o parser que entrega URLs inválidas.

## Reversal Strategy

Ampliar el contrato mediante versión explícita y pruebas de compatibilidad; no conceder acceso directo a DB o biblioteca como arreglo local.
