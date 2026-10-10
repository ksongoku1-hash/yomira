# ADR-0036 — Posición y porcentaje exactos de ReaderProgress

## Status

Accepted — decisión de implementación de K045, conforme a la sección 7.8 de la especificación y a ADR-0026.

## Context

La especificación enumera `series_id`, `chapter_id`, `page_index`, `percentage` y `updated_at`, pero no define el índice inicial, la precisión porcentual ni cómo impedir que un capítulo quede asociado a una serie distinta de la que realmente lo contiene. ADR-0026 exige tipos exactos, timestamps UTC en microsegundos y FKs `RESTRICT`.

## Decision

- Una fila identifica el progreso único de un capítulo local dentro de una serie mediante PK compuesta `(series_id, chapter_id)`.
- La pareja `(series_id, chapter_id)` referencia la pareja correspondiente de `chapters` con FK `ON DELETE RESTRICT`. `chapters` expone una clave única compuesta para que SQLite pueda comprobar la correspondencia; `chapter_id` sigue siendo su identidad primaria sin cambio.
- `page_index` es entero no negativo y cero-based. No se redondea ni se almacena como `float`; K045 no exige que el índice sea menor que un número de páginas que puede no estar disponible en el registro de dominio.
- El dominio recibe `percentage` como `Decimal` exacto entre `0` y `100`, inclusive. Solo acepta valores representables exactamente en incrementos de `0.01`; los valores de mayor precisión que perderían datos se rechazan en vez de redondearse.
- SQLite almacena `percentage` como `percentage_basis_points`, entero de `0` a `10_000`, donde cada unidad representa `0.01` puntos porcentuales. El mapper convierte de/para `Decimal` sin aproximación binaria.
- `updated_at` es una fecha consciente de zona horaria almacenada como Unix UTC en microsegundos. Es el instante requerido de la última actualización del progreso.
- K045 persiste la posición; no abre activos ni archivos ni calcula cuántas páginas totales tiene un capítulo.

## Alternatives Considered

- `float`: introduce aproximación binaria en un dato de posición visible y puede acumular errores al actualizarlo.
- `Decimal` serializado a `TEXT`: mantiene exactitud en Python pero obliga a duplicar parsing y comparación decimal fuera de los tipos numéricos nativos de SQLite.
- Redondear valores con más de dos decimales: cambia silenciosamente la posición solicitada.
- FKs independientes para serie y capítulo: aceptarían una fila donde ambas entidades existen pero el capítulo pertenece a otra serie.
- Usar solo `chapter_id` como clave: no refleja la identidad compuesta que declara el contrato de ReaderProgress.

## Positive Consequences

- Porcentaje, índice y timestamps se conservan con exactitud verificable.
- La FK compuesta detecta asociaciones de serie/capítulo contradictorias incluso en SQL directo.
- Una actualización de lectura sustituye solo la posición de ese capítulo y serie.

## Negative Consequences

- La base añade un índice único sobre `(series_id, chapter_id)` en `chapters` para soportar la FK compuesta.
- Porcentajes con precisión inferior a una centésima se rechazan; el caller debe conservar una posición exacta admitida.

## Failure Modes

- Un caller convierte un porcentaje a `float` y pierde precisión antes de construir el dominio.
- Un mapper usa una conversión redondeada en vez de una conversión exacta de basis points.
- Una migración crea el índice padre después de la FK o debilita la correspondencia compuesta.
- Un capítulo inexistente o perteneciente a otra serie se acepta como progreso válido.

## Reversal Strategy

Cambiar base del índice, precisión, escala del porcentaje o identidad requiere nueva ADR y migración. La migración debe validar el rango y la exactitud de cada porcentaje antes de convertirlo; no redondea ni descarta progreso. Cualquier reconstrucción verifica backup, conteos, FK e `integrity_check`.
