# ADR-0021 — Familia API_ para errores de contrato HTTP

## Status

Accepted — aprobado por el usuario el 2026-09-25 al elegir “Añadir API_ mediante ADR-0021”.

## Context

K004 aprobó un catálogo de familias que no enumeraba API_. K021 del plan maestro exige el código API_VALIDATION_FAILED para errores de validación de requests. Reutilizar SYS_ para una entrada de cliente confundiría errores de infraestructura con errores de contrato HTTP y degradaría diagnóstico/trazabilidad.

## Decision

Añadir API_ como familia estable para errores detectados en la frontera HTTP/API. API_VALIDATION_FAILED identifica requests que no cumplen el schema publicado; no se reintenta sin corregir el input. El error mantiene el contrato code, message, trace_id y retryable del catálogo aprobado. La respuesta no refleja valores sensibles ni cuerpos arbitrarios.

Los códigos SYS_INTERNAL_ERROR, SYS_STARTUP_FAILED y SYS_INVALID_CONFIG permanecen bajo SYS_ y no cambian la semántica de otros códigos.

## Alternatives Considered

- Mapear API_VALIDATION_FAILED a SYS_: mezcla errores de request y errores internos, y no cumple el nombre requerido por K021.
- Omitir el código de validación: deja la respuesta sin clasificación estable y no satisface K021.

## Positive Consequences

- El código y la capa que detecta el error concuerdan.
- Logs y soporte pueden distinguir input inválido de fallos internos.
- Se conserva la forma de respuesta ya aprobada en K004.

## Negative Consequences

- El catálogo ahora tiene una familia adicional que clientes y verificadores deben reconocer.

## Failure Modes

- API devuelve texto libre o valores de input no sanitizados.
- Se marca como retryable un request que no cambia.
- UI compara el texto humano en vez de code.

## Reversal Strategy

Retirar API_ requiere migrar todos los códigos antes de release y mantener una traducción compatible en las respuestas durante la transición. No se sustituirá silenciosamente por SYS_.
