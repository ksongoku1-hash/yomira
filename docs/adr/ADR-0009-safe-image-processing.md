# ADR-0009 — Procesamiento SAFE de imágenes

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Un recorte agresivo puede borrar dibujos, texto o márgenes deliberados. Las transformaciones irreversibles ponen en riesgo la integridad visual.

## Decision

Ofrecer `OFF`, `SAFE` y `E_READER`; usar `SAFE` por defecto. Si hay duda, no recortar. Mantener golden dataset y no recomprimir páginas que no requieren transformación.

## Alternatives Considered

- Recorte automático agresivo: puede eliminar contenido cercano al borde.
- Siempre procesar y volver a codificar: puede degradar imágenes sin beneficio.
- Desactivar todo procesamiento: elimina la opción de mejora segura prevista.

## Positive Consequences

- El original queda intacto cuando no se necesita cambio.
- Los casos límite se comparan con resultados esperados y tolerancias.

## Negative Consequences

- Un modo conservador puede dejar bordes visibles.
- La definición de seguridad necesita fixtures diversos y límites medibles.

## Failure Modes

- Falso positivo de recorte, página doble mal clasificada, metadata de imagen inválida o salida recomprimida inesperadamente.

## Reversal Strategy

Deshabilitar transformación para nuevos jobs y mantener cada versión anterior. Cambiar umbrales requiere ADR y actualización del golden dataset; no reescribir CBZ publicados sin validación y publicación atómica.
