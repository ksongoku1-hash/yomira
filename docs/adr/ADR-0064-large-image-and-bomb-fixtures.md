# ADR-0064 — PNG de alta dimensión e image bomb

## Status

Accepted for K077 implementation — 2026-09-26.

## Context

La especificación §29 exige límites independientes de bytes, dimensiones, píxeles totales y límites del decodificador. §83 pide un caso de imagen grande y uno de image bomb. Los tests deben ejercitar cabeceras reales de imagen sin asignar cientos de megabytes ni decodificar píxeles fuera de presupuesto.

## Decision

- `HUGE_IMAGE` genera un PNG válido y uniforme de `2048 × 2048` (≈12 MiB decodificados), comprimido en streaming por filas. El body HTTP queda debajo de 128 KiB y no construye el raster completo en memoria dentro del server.
- El test decodifica este raster con Pillow y comprueba dimensiones y formato. Es un fixture de alta dimensión respecto al PNG 2×2 normal; no afirma un límite de producto.
- `IMAGE_BOMB` devuelve un PNG sintético menor a 1 KiB con IHDR de `20000 × 20000` (400 millones de píxeles). El test exige que Pillow lance `Image.DecompressionBombError` al abrir la cabecera, antes de solicitar `load()`.
- El fixture bomb contiene los chunks mínimos para que el decoder inspeccione una cabecera PNG válida, pero no contiene raster de 400 MP. La prueba no materializa ni descomprime ese raster.
- Ambos escenarios se activan por instancia y solo reemplazan la respuesta del endpoint de page-1. Los modos normal y página inválida siguen independientes.
- `MAX_IMAGE_BYTES`, `MAX_IMAGE_PIXELS`, `MAX_IMAGE_WIDTH` y `MAX_IMAGE_HEIGHT` deben decidirse en Fase 10. Estos tamaños son estímulos de prueba, no sustituyen esos ADR ni implican umbrales finales.

## Alternatives Considered

- **Crear `Image.new(20000, 20000)` para la bomb:** puede reservar gigabytes y provocar precisamente el agotamiento de memoria que el test busca evitar.
- **Inflar el body por cientos de MiB:** vuelve el gate costoso sin demostrar la comprobación de dimensiones/píxeles antes de decodificar.
- **Usar solo un PNG header-only como imagen grande normal:** no verifica la carga íntegra de una imagen válida.
- **Fijar aquí caps de producción:** los umbrales pertenecen a la tarea de validación y requieren ADR propio con hardware y límites aprobados.

## Consequences

- La suite valida tanto el caso de dimensiones altas decodificable como la defensa del decoder ante una cabecera bomb, con payloads transmitidos pequeños.
- La futura validación de producto tendrá que comprobar tamaño de bytes y dimensiones por separado; pasar estos fixtures no demuestra que los caps finales sean suficientes.
- La generación en streaming tiene coste acotado a una fila, compresor y body comprimido, sin raster persistido.

## Failure Modes

- PNG grande no decodifica o excede el límite del body: test normal-large falla.
- Pillow no rechaza la cabecera bomb antes de decode: test bomb falla.
- El fixture produce una dimensión distinta o una asignación raster en el server: test/inspección del generador debe fallar antes de aceptar K077.

## Reversal Strategy

Cambiar dimensiones de estímulo, formato o método de generación requiere actualizar los tests de K077 y mantener una memoria máxima explícita. Los caps de producto deben permanecer en ADR independiente de Fase 10.
