# ADR-0093 — Perfil e-reader y entrada del lector

## Estado

Accepted — 2026-09-27. Implementa K182–K184 conforme a `E_READER_UX.md` y `DESIGN_SYSTEM.md`.

## Contexto

La UX aprobada exige un perfil e-reader con superficies de alto contraste, movimiento mínimo, objetivos táctiles grandes y menor precarga. También exige navegación con teclado y zonas táctiles en modos paginados; el mapeo concreto aceptado aparece en `E_READER_UX.md` §4. El lector conserva las preferencias tipadas en SQLite y no puede modificar el CBZ ni sus imágenes.

## Decisión

1. El perfil es una preferencia booleana tipada `reader.e_reader`, default `false`, guardada en la tabla de ajustes existente junto con las demás preferencias del lector. Activarlo no sobrescribe `reader.preload_ahead`; la precarga efectiva pasa a cero mientras el perfil está activo y la preferencia original vuelve a aplicarse al desactivarlo.
2. El perfil cambia únicamente la presentación de la interfaz del lector: superficies planas de alto contraste, bordes visibles, objetivos de control de al menos 44 × 44 px CSS, sin sombras, animaciones ni transiciones perceptibles. No aplica filtros a páginas, CBZ o archivos de origen.
3. Atajos de teclado, activos cuando el foco pertenece al lector y no a un campo editable:
   - En modos de página única y doble, `ArrowRight`/`ArrowLeft` avanzan o retroceden según la dirección visible; en RTL se invierten. `PageDown` avanza y `PageUp` retrocede independientemente de RTL.
   - En vertical, `ArrowDown`/`PageDown` y `ArrowUp`/`PageUp` desplazan la ventana una página lógica; las flechas izquierda/derecha no cambian la página.
   - `F` alterna pantalla completa; `Escape` queda al control nativo del navegador.
   - `+`/`=` acerca, `-` aleja y `0` restablece el zoom. Se respetan los límites de ADR-0091.
   - Las acciones manejadas evitan el comportamiento de navegador correspondiente, pero nunca mueven el foco al navegar.
4. En single/double, un tap corto en el 30 % exterior izquierdo o derecho de la superficie de página navega una página/spread; el 40 % central no actúa. El lado que avanza sigue la dirección visible y se invierte en RTL. Solo se acepta un tap de hasta 350 ms con desplazamiento menor de 12 CSS px; gestos largos o de arrastre no navegan. En vertical se conserva el desplazamiento nativo por touch. Zoom y navegación siempre tienen controles visibles como alternativa.
5. Los atajos no se ejecutan desde `input`, `textarea`, `select` ni elementos `contenteditable`, ni cuando Control, Alt o Meta acompañan la tecla. Ningún gesto intercepta un control interactivo.

## Consecuencias

- El ajuste adicional usa una clave desconocida preservable por el esquema actual; no requiere migración SQLite.
- La precarga cero del perfil reduce solicitudes y memoria sin descartar preferencias configuradas por el usuario.
- La zona central inerte y el umbral de movimiento reducen navegación táctil accidental; vertical sigue usando scroll normal.
- El perfil conserva el contenido y los controles principales; K185 debe medir memoria y K186 validar interacción en hardware desktop, móvil y e-reader.

## Verificación requerida

- Pruebas de persistencia y validación de `reader.e_reader` en backend.
- Pruebas de atajos, RTL, exclusión de campos editables, umbrales touch, modo vertical y perfil visual en frontend.
- Full suite, typecheck, lint y build frontend; backend tests, Ruff y Pyright.
- K185/K186 permanecen abiertos hasta medir recorridos largos y completar las plataformas requeridas.
