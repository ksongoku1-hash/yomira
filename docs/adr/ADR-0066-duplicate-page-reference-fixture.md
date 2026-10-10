# ADR-0066 — Referencias de página duplicadas en el Mock Server

## Status

Accepted for K079 implementation — 2026-09-26.

## Context

El contrato K064 establece que secuencias de referencias remotas conservan orden y duplicados; el downloader asignará ordinals a cada entrada. Algunas fuentes pueden listar dos veces el mismo recurso. El Mock Manga Server debe reproducir el caso para que los consumidores no lo deduzcan como conjunto.

## Decision

- `DUPLICATE_PAGES` responde la secuencia `page-1`, `page-2`, `page-1`, con cada aparición en su posición original.
- La serie y los contenidos de página siguen siendo los mismos fixture IDs. Solo cambia la secuencia devuelta por el endpoint de páginas.
- El capítulo declara `page_count=3`, que cuenta las entradas de la secuencia, incluidas repeticiones. Hay exactamente dos referencias únicas y tres entradas.
- Cada ocurrencia repetida sigue siendo una entrada HTTP independiente en la respuesta JSON; el fixture no llama `set`, `unique`, sort ni deduplicación.
- El modo normal mantiene dos referencias únicas y `page_count=2`; la modalidad inválida de page count continúa separada.

## Alternatives Considered

- **Eliminar duplicados al construir la respuesta:** contradice el contrato de preservación y oculta el caso que la descarga debe manejar.
- **Declarar count=2 para tres entradas:** mezclaría duplicación con el escenario `WRONG_PAGE_COUNT` y haría el test ambiguo.
- **Cambiar bytes de page-1 al repetirse:** una referencia idéntica representa el mismo recurso; distintos bytes para el mismo recurso introducirían una carrera remota que esta tarea no especifica.
- **Ordenar las referencias:** cambia el orden fuente y rompe trazabilidad de ordinals.

## Consequences

- Tests de integridad pueden comprobar count bruto, orden y repeticiones antes de asignar ordinals.
- El modo simula identidad repetida, no contenido mutable entre dos lecturas.
- La deduplicación de almacenamiento por hash, si se decide, no debe borrar las entradas lógicas/ordinales del capítulo.

## Failure Modes

- Longitud de lista cae a dos: se perdió una aparición.
- Orden pasa a `page-1`, `page-1`, `page-2`: fixture ordena/deduplica de forma incorrecta.
- `page_count` no es igual a tres: se mezcló con el escenario de count inválido.

## Reversal Strategy

Cambiar la secuencia, count o semántica de ocurrencias requiere modificar esta decisión junto con K064 y los contract tests del Download Engine.
