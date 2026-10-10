# ADR-0039 — Contrato de listado de biblioteca

## Status

Accepted — decisión de implementación de K050, 2026-09-26.

## Context

K050 publica `GET /api/v1/library`, previsto por la especificación, y K058 exige probar bibliotecas de hasta 10.000 series. Sin orden y paginación explícitos, SQLite puede devolver filas en un orden no estable y la API podría cargar una biblioteca completa sin límite. El usuario recibió la opción recomendada de orden A–Z por título canónico y no indicó un cambio durante la ventana de respuesta.

## Decision

- `GET /api/v1/library` lista todas las series locales por `canonical_title` ascendente con collation SQLite `BINARY`, y usa `series_id` ascendente como desempate determinista. Los valores almacenados no se normalizan.
- La lista usa `limit` por defecto 100, máximo 500, y `offset` por defecto 0, no negativo. El servicio aplica los mismos límites aunque se invoque sin pasar por HTTP.
- La respuesta sigue el envelope `{ "data": ..., "meta": ... }`; `meta` incluye `limit`, `offset` y el total de series.
- `GET /api/v1/series/{series_id}` devuelve una serie o 404 con `API_REQUEST_REJECTED` si no existe. Los errores conservan el envelope y la correlación definidos por ADR-0022.
- K050 no agrega filtros de estado; corresponden a K055. Las respuestas reflejan los campos `Series` disponibles; metadata con procedencia se ampliará en K056.

## Alternatives Considered

- Ordenar por última actualización: prioriza actividad reciente y mueve filas al editar, dificultando retomar una posición entre páginas.
- Devolver todas las filas: el uso de memoria y la respuesta crecen con la biblioteca.
- Ordenar solo por título: títulos repetidos no dan un orden determinista entre páginas.
- Usar collation dependiente de la locale del equipo: el resultado podría cambiar al mover la biblioteca a otra configuración regional.

## Positive Consequences

- La paginación produce un orden reproducible en diferentes máquinas y reinicios.
- El máximo de 500 entidades acota objetos y serialización por respuesta.
- El orden no modifica ni normaliza los metadatos originales.

## Negative Consequences

- `BINARY` ofrece orden estable, pero no una clasificación lingüística localizada de acentos y alfabetos no latinos.
- `offset` alto requiere recorrer filas previas; K058 medirá el comportamiento hasta 10.000 series.

## Failure Modes

- No usar el desempate por UUID y devolver páginas inestables cuando hay títulos iguales.
- Permitir `limit` excesivo o `offset` negativo desde una llamada directa al servicio.
- Agregar filtros sin fijar si alteran el total informado.

## Reversal Strategy

Una nueva ordenación, paginación por cursor o locale requiere ADR y pruebas de compatibilidad de la API, además de medir los casos de 1.000 y 10.000 series antes de cambiar el contrato.
