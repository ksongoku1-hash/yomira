# ADR-0094 — Geometría estable al reducir zoom vertical

## Estado

Accepted — 2026-09-27. Aclara K180–K181 y restringe la geometría de página vertical.

## Contexto

ADR-0092 fija la ventana vertical en una página anterior, la página actual y hasta dos posteriores, y mantiene el DOM acotado. El lector comparte un zoom de 50–300 %. Reducir la altura de cada slot según el zoom permitiría que más páginas que la ventana cargada ocuparan físicamente el viewport, dejando páginas visibles en blanco o aumentando sin límite razonable la cantidad de recursos visibles no expulsables.

## Decisión

1. En modo vertical, el alto del slot usa la altura base de página y escala solo hacia arriba: `base_height × max(1, zoom)`. Nunca se reduce por debajo de la altura base cuando el usuario aleja el zoom.
2. El bitmap sí respeta el zoom seleccionado dentro del slot. Alejar puede dejar espacio neutro alrededor de la imagen; las posiciones y separaciones de páginas permanecen estables. Acercar expande el slot junto con la imagen para evitar que páginas vecinas cubran su contenido.
3. La virtualización, el scroll restaurado, el cálculo de página visible y la caché usan el mismo alto de slot. El límite de ventana se mantiene en cuatro páginas y una página fuera de esa ventana puede volver a solicitarse desde el CBZ verificado.
4. Fit-width y fit-height continúan aplicándose al bitmap y reinician el zoom manual a 100 %; no reordenan ni alteran las páginas.

## Consecuencias

- Reducir zoom vertical no aumenta el número de páginas visibles ni el presupuesto de memoria fijado.
- El espacio entre páginas no colapsa; la posición virtual conserva una relación estable con el índice.
- El usuario puede ver margen adicional alrededor de una imagen al alejar el zoom. K186 debe validar que esta presentación sea legible en pantalla pequeña y e-reader.

## Verificación requerida

- Prueba de que el alto del slot no disminuye por debajo de la altura base a 50 %.
- Regresión de posición vertical, orden y ventana acotada al acercar/alejar.
- Typecheck, lint, build y suite de lector.
