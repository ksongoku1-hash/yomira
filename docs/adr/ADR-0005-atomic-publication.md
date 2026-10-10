# ADR-0005 — Publicación atómica

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Un archivo final visible antes de terminar la escritura podría ser confundido con contenido completo y causar pérdida o corrupción aparente.

## Decision

Construir en `.tmp`, verificar ZIP, `ComicInfo.xml`, número de páginas, decodificación y hashes; cerrar el archivo y solo entonces renombrarlo atómicamente a `.cbz`. Indexar después de publicar.

## Alternatives Considered

- Escribir directamente con extensión `.cbz`: expone archivos parciales.
- Renombrar antes de verificar: hace visible contenido no validado.
- Mantener varias copias finales: aumenta ambigüedad y uso de disco.

## Positive Consequences

- La ruta definitiva indica un artefacto ya verificado.
- Una caída durante construcción deja una ruta temporal identificable y recuperable.

## Negative Consequences

- Se necesita espacio temporal para la nueva copia.
- Las garantías de rename dependen del filesystem y del volumen de destino.

## Failure Modes

- Disco lleno antes del rename, bloqueo antivirus, crash durante ZIP o archivo temporal huérfano.

## Reversal Strategy

No hay reversión del principio. Si el filesystem no ofrece el rename requerido, bloquear publicación y definir un ADR con protocolo alternativo que mantenga la regla de no exponer un archivo parcial.
