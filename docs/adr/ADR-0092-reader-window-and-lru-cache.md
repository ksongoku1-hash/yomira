# ADR-0092 — Ventana virtual y caché LRU del lector

## Estado

Accepted — 2026-09-27. Concreta K180–K181; la caché es efímera y reconstruible, no forma parte de la biblioteca.

## Contexto

La SPEC prohíbe cargar el capítulo completo, pide una ventana configurable y propone una caché LRU inicial de 500 MB. El lector actual limita el DOM vertical a cuatro páginas, pero no configura la ventana ni conserva páginas fuera de ella. El backend ya sirve cada página verificada por separado desde un CBZ inmutable, con ETag derivado del contenido.

## Decisión

1. `reader.preload_ahead` es entero entre 0 y 2, default 2. En vertical se conservan como máximo la página anterior, la actual y hasta dos siguientes. Single/double solo montan su página o par actual. El DOM nunca incluye el capítulo completo.
2. `reader.cache_limit_mb` es entero entre 16 y 500, default 500 MB decimales (500,000,000 bytes). La unidad queda fijada como MB decimal por el nombre `*_mb` y la cifra de la SPEC. Los ajustes se aplican en caliente al cambiar capítulo o al moverse la ventana.
3. La caché es LRU en memoria del proceso de UI y está limitada por capítulo. Sus entradas contienen únicamente un `Blob` recibido del ReaderAssetService y metadatos de decodificación; no se escriben archivos temporales ni se persiste contenido de página. La caché se descarta al cerrar el lector o cambiar de capítulo.
4. El coste de una entrada se estima conservadoramente como `Blob.size + naturalWidth × naturalHeight × 4` bytes tras decodificar. La estimación cuenta el payload comprimido y una superficie RGBA. La página visible no se expulsa mientras se muestra; primero se expulsa por LRU cualquier página fuera de la ventana. Si las páginas visibles exceden el presupuesto, se permite únicamente esa excepción visible, acotada por los límites de página de ADR-0086 y el número máximo visible del modo actual.
5. Una página fuera del presupuesto se libera: se aborta si aún carga, se revoca su object URL y se retira del LRU. Un fallo de preload no bloquea la página actual; al llegar a una página expulsada se vuelve a solicitar desde el CBZ verificado.
6. La clave lógica durante una sesión es `(chapter_id, page_number)`. ADR-0085 declara el CBZ publicado inmutable y el servicio vuelve a verificar archivo/página antes de servir. La caché no sobrevive a la sesión del lector, evitando reutilizar un recurso tras cerrar/volver a abrir el capítulo.
7. Los valores de caché/ventana se almacenan como settings tipados versionados en la tabla existente. Valores ausentes usan sus defaults; valores presentes con JSON, versión, tipo o rango inválidos producen error trazable y no se reemplazan al leer. PUT sustituye explícitamente el ajuste inválido por una configuración validada en una transacción.

## Alternativas consideradas

- Cachear el capítulo completo o cada página en disco: duplica contenido y hace crecer almacenamiento persistente; contradice la política efímera.
- Limitar solo el número de páginas: no acota bytes ante páginas de tamaños/dimensiones distintas.
- Contar solo bytes comprimidos: subestima superficies de imagen decodificadas.
- Expulsar una página que el usuario está viendo: puede interrumpir la lectura.
- Cache global sin identidad de capítulo o tras cerrar el lector: facilita contenido obsoleto y retención innecesaria.

## Consecuencias y límites

- 500 MB es el presupuesto de la caché LRU, no una medición exacta de RSS del navegador. La estimación no incluye overhead interno del motor/GPU; K185 debe medir estabilidad con recorridos de 100/500/2000 páginas.
- La excepción de página(s) visible(s) no vuelve ilimitada la memoria: la imagen por página está acotada por ADR-0086 y el modo double expone como máximo dos páginas simultáneas.
- Reducir `preload_ahead` baja solicitudes y RAM en dispositivos lentos; `0` conserva solo página actual y la anterior en vertical.
- El esquema SQLite existente conserva claves desconocidas; no se requiere migración para nuevas claves de setting.

## Reversión

Se puede desactivar la retención fuera de ventana usando `preload_ahead=0` y un presupuesto mínimo. Retirar claves de settings requiere conservarlas como desconocidas o migrarlas explícitamente; nunca se borran silenciosamente.
