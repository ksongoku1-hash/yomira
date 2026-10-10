# ADR-0002 — FastAPI + Python

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

El backend debe coordinar API, conectores, base de datos, validación de imágenes, CBZ, lectura, exportación, diagnósticos y recuperación en Windows.

## Decision

Usar Python y FastAPI con Pydantic, `httpx.AsyncClient`, Pillow, SQLAlchemy 2 y Alembic. Pytest, Ruff y Pyright forman parte de las herramientas de calidad previstas.

## Alternatives Considered

- ASP.NET: alternativa sólida en Windows, pero distinta del stack establecido.
- Node.js: unifica el lenguaje con la UI, pero no es el stack backend previsto y cambiaría el conjunto de herramientas de imágenes y datos.
- Servicio nativo en C++: ofrece control de bajo nivel, con mayor complejidad de mantenimiento.

## Positive Consequences

- Validación declarativa de entradas con Pydantic.
- Soporte para operaciones HTTP asíncronas y librerías de procesamiento de imágenes.
- Alembic permite versionar las migraciones de SQLite.

## Negative Consequences

- Debe empaquetarse el runtime; el usuario final no instalará Python.
- El trabajo de CPU y el I/O deben mantenerse acotados para no bloquear la API.

## Failure Modes

- Bloqueo del event loop por una operación prolongada.
- Incompatibilidad de dependencias al empaquetar.
- La API y sus schemas dejan de coincidir con el frontend.

## Reversal Strategy

Un cambio de runtime requiere ADR, inventario de dependencias, pruebas de migración y un adaptador que preserve los contratos `/api/v1` y el formato de los datos.
