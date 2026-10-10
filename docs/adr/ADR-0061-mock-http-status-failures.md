# ADR-0061 — Respuestas HTTP 404, 429 y 500 en Mock Manga Server

## Status

Accepted for K074 implementation — 2026-09-26.

## Context

Un connector debe clasificar por separado un recurso inexistente, rate limiting y un fallo del servidor. El Mock Manga Server debe poder reproducirlos sin Internet, con cuerpos y headers que no filtren parámetros recibidos y con liveness disponible para diferenciar una respuesta de error de un proceso muerto.

## Decision

- Los escenarios de instancia `NOT_FOUND`, `RATE_LIMITED` y `SERVER_ERROR` devuelven respectivamente HTTP 404, 429 y 500 para rutas de la fuente.
- Los cuerpos son JSON fijo y genérico: `{"error":"not found"}`, `{"error":"rate limited"}` y `{"error":"internal error"}`. No interpolan path, query, IDs ni metadata del request.
- La respuesta 429 incluye `Retry-After: 1` para permitir que tests de cliente verifiquen lectura de la cabecera; no define el backoff de producción.
- `/health` continúa devolviendo 200 en los tres escenarios. El estado solo se establece al construir el servidor y permanece inmutable.
- Responses conservan `Content-Length`, `Cache-Control: no-store` y cierre explícito de conexión.

## Alternatives Considered

- **Embeber detalle de excepción/URL en el cuerpo:** puede filtrar datos de request y crea una expectativa falsa de manejo de errores.
- **Usar una ruta separada para cada status:** obliga al cliente a alterar URLs para activar un fallo y añade ese artificio al contrato remoto.
- **Apagar el proceso para simular 404/429/500:** confunde errores HTTP con timeout o desconexión; esos casos tienen tareas propias.
- **Marcar health como caído durante errores de ruta:** impide probar la clasificación de respuesta mientras el proceso remoto sigue disponible.

## Consequences

- Los tests pueden afirmar tanto el código HTTP como el cuerpo fijo y la recuperación del proceso.
- `Retry-After` es un estímulo de fixture; el cliente debe validar valores y aplicar sus propios límites de espera.
- Estas respuestas no especifican mapping definitivo de fallos HTTP a códigos `SRC_*`; ese mapping corresponde a la implementación y contrato de cada connector.

## Failure Modes

- Body contiene query/ID: test de sanitización falla.
- Status cambia o Retry-After falta en 429: test de estado falla.
- Health deja de responder después del error: test de recuperación falla.

## Reversal Strategy

Cambiar status, cuerpo, cabeceras o semántica de health requiere actualizar el contrato del fixture y sus tests. No se debe alterar el comportamiento normal para introducir fallos.
