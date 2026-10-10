# ADR-0065 — Metadata inválida y page count inconsistente

## Status

Accepted for K078 implementation — 2026-09-26.

## Context

§83 exige metadata malformada y recuento de páginas incorrecto. El endpoint normal entrega metadata JSON válida con siete campos y declara dos páginas para dos referencias. La variante debe reproducir respuestas sintácticamente válidas pero semánticamente incompatibles con los tipos e invariantes del SDK.

## Decision

- `MALFORMED_METADATA` responde HTTP 200 y JSON válido, pero campos con tipos imposibles para el contrato: `title` entero, `author` lista, `genres` string, `status` boolean y `language` lista.
- El fixture no incluye una excepción del servidor ni falla el parseo JSON: el cliente debe llegar a validación del schema y rechazar campos incompatibles sin coerción heurística.
- `WRONG_PAGE_COUNT` conserva los dos page references normales, pero declara `page_count=3` en el metadata del capítulo. La discrepancia se puede observar leyendo ambas respuestas.
- Estos modos solo alteran su endpoint de fixture; Search, imágenes, `/health` y el modo normal permanecen independientes.
- No se define aquí el comportamiento de producto ante metadata parcial, campo opcional ausente ni `SRC_SCHEMA_CHANGED`; su contrato queda en el SDK y el connector.

## Alternatives Considered

- **Enviar JSON sintácticamente roto para ambos casos:** prueba el parser JSON, no la validación de tipos o invariantes.
- **Omitir title como único error:** varios campos del contrato son opcionales, así que podría ser una respuesta parcial válida.
- **Cambiar también la lista de páginas:** impediría distinguir un page count declarado erróneo de una lista real incompleta.
- **Coaccionar `42` a texto o genres string a lista:** normaliza silenciosamente contenido de fuente y oculta una incompatibilidad de schema.

## Consequences

- Tests posteriores pueden validar parsing y normalización en capas separadas y exigir que el count declarado coincida con las referencias recibidas.
- La respuesta malformada sigue dentro de límites pequeños de JSON y no puede agotar memoria.
- Los estímulos no convierten estos campos en errores de producto hasta que el Download/Source integration defina su policy.

## Failure Modes

- El escenario devuelve tipos válidos por accidente: el test de schema no detecta una fuente dañada.
- El page count no difiere del número de referencias: el test de integridad de lista falla.
- Se alteran rutas normales o salud: los tests base de K072 dejan de pasar.

## Reversal Strategy

Cambiar los tipos inválidos, el page count declarado o el alcance de las rutas requiere actualizar los tests del fixture y preservar separadas las respuestas normales.
