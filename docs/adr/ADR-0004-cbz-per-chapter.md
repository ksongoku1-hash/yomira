# ADR-0004 — Un CBZ por capítulo

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

La biblioteca organiza manga/manhwa por obra y capítulo; la exportación al e-reader debe manejar unidades completas y verificables.

## Decision

Crear un archivo CBZ por capítulo, con `ComicInfo.xml` y páginas en orden estable. No presentar el archivo en la biblioteca hasta verificarlo.

## Alternatives Considered

- Un archivo por serie: dificulta descargas parciales, actualización y exportación de capítulos.
- Imágenes sueltas como formato principal: no ofrece la unidad portátil requerida.
- PDF por capítulo: no es el formato objetivo de la especificación.

## Positive Consequences

- Un capítulo puede verificarse, reemplazarse o exportarse de forma independiente.
- El formato es portátil y reconstruible desde los archivos publicados.

## Negative Consequences

- Una serie produce muchos archivos y entradas de índice.
- El empaquetado duplica temporalmente espacio mientras se construye el CBZ.

## Failure Modes

- ZIP incompleto, orden de página incorrecto, metadatos inválidos o falta de espacio durante el empaquetado.

## Reversal Strategy

Conservar CBZ existentes y proporcionar una conversión verificable por capítulo. No eliminar originales hasta validar el formato nuevo y mantener un mapeo de capítulos y hashes.
