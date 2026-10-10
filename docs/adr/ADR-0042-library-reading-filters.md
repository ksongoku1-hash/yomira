# ADR-0042 — Filtros de lectura de la biblioteca

## Status

Accepted for K055 implementation — 2026-09-26. La implementación usa el estado local persistido; esta decisión registra la semántica necesaria porque K055 nombra los filtros pero no define su cálculo.

## Context

`Series.status` describe metadata editorial y de publicación, no el estado de lectura. `ReaderProgress` se guarda por serie y capítulo, incluye un porcentaje exacto de 0 a 100 y no exige conocer el total de páginas. Las series y capítulos sin progreso deben seguir visibles en Todos.

## Decision

- Todos no agrega una condición de lectura.
- Leyendo incluye una serie si al menos un capítulo local tiene progreso estrictamente entre 0 % y 100 %. Si hay varios capítulos, el progreso parcial en cualquiera de ellos prevalece aunque los demás no tengan progreso.
- Completos incluye una serie con al menos un capítulo catalogado localmente cuando cada capítulo local tiene una fila de progreso exactamente al 100 %. Esto no afirma que la publicación esté completa.
- Incompletos incluye una serie con al menos una fila de progreso que no califica como Completos ni Leyendo. Por tanto, las posiciones guardadas en 0 % pueden aparecer aquí; una serie sin ninguna fila de progreso aparece solo en Todos.
- La clasificación usa únicamente capítulos y `ReaderProgress` locales. No consulta ni modifica `Series.status`, metadata de fuente ni contenido remoto.
- El backend filtra y cuenta en SQLite antes de aplicar `limit` y `offset`; no carga toda la biblioteca a memoria. El parámetro HTTP es opcional y se llama `reading_status`, con valores `reading`, `completed` e `incomplete`. La ausencia del parámetro equivale a Todos.
- Cambiar de filtro reinicia el offset a cero. Una respuesta anterior solo puede mantenerse como contenido temporal durante paginación del mismo filtro; nunca se presenta bajo otro filtro.

## Alternatives Considered

- Guardar un estado manual por serie: requeriría controles para cambiarlo, una migración y una regla de precedencia frente al progreso por capítulo que K055 no especifica.
- Reutilizar `Series.status`: mezclaría estado editorial con lectura y produciría resultados dependientes del texto entregado por fuentes.
- Calcular filtros en el frontend: obligaría a descargar todos los capítulos y progresos, rompería el límite paginado y aumentaría consumo de memoria.
- Tratar todo progreso parcial como Incompletos además de Leyendo: haría que los filtros se solaparan sin una indicación contractual.

## Positive Consequences

- El progreso se deriva de la fuente local exacta y no duplica un estado manual que podría contradecirlo.
- La consulta y el total filtrado permanecen acotados por SQLite y la página de resultados.
- Los nombres de filtros no alteran los campos editoriales ni cambian datos almacenados.

## Negative Consequences

- Completos se interpreta respecto a los capítulos conocidos localmente; no representa el final de publicación ni capítulos aún no catalogados.
- Incompletos no incluye series sin filas de progreso; esas series solo aparecen en Todos.
- Los nombres de filtros no corresponden a un estado persistido y pueden cambiar si posteriormente se aprueba un estado manual.

## Failure Modes

- Usar `Series.status` en vez de `ReaderProgress` y clasificar por metadata de publicación.
- Marcar como Completos una serie sin capítulos locales por una condición vacía de “todos los capítulos completados”.
- Comparar porcentajes de punto flotante; el almacenamiento aprobado es entero en puntos base.
- Devolver como total el número anterior al filtro o reutilizar temporalmente resultados de otro filtro.

## Reversal Strategy

Cambiar la precedencia, solapamiento o persistencia de estos filtros requiere un nuevo ADR. Una futura columna de estado manual requiere migración con backup verificado, compatibilidad con progreso ya guardado y pruebas que garanticen que no se modifica `Series.status`.
