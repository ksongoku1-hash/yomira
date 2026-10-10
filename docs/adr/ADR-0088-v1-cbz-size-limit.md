# ADR-0088 — Tamaño máximo del CBZ V1

## Estado

Accepted — límite aprobado explícitamente por el usuario el 2026-09-27. Complementa ADR-0083, ADR-0085 y ADR-0086.

## Contexto

El límite de 64 MiB por página y el límite existente de 10 000 páginas por manifest acotan páginas individuales y cardinalidad, pero no acotan suficientemente el espacio requerido por empaquetado ni el trabajo de verificación/lectura del CBZ. El builder escribe primero un archivo `.cbz.tmp`; un límite posterior a la escritura llegaría demasiado tarde para controlar el consumo.

## Decisión

- Un archivo CBZ por capítulo no excederá 2 GiB (2 147 483 648 bytes).
- Los artefactos temporales de páginas de un job tampoco excederán 2 GiB en conjunto. El total incluye páginas validadas, el `.part` de cada intento actual y todo `.part` previo preservado. La cifra fue aprobada explícitamente por el usuario el 2026-09-27.
- El core determina el espacio temporal restante antes de empezar una transferencia y el transporte comprueba ese presupuesto antes de escribir cada chunk; no permite que una escritura exceda la cota. Un agotamiento de cuota detiene el stream, preserva los archivos ya escritos y falla el job sin generar/publicar CBZ.
- Cada job conserva su presupuesto hasta completar o fallar; los archivos ambiguos no se excluyen del conteo. El inventario de temporales debe ser seguro, acotado y basado en rutas bajo el DataRoot identificado.
- El máximo de artefactos propios de un job durante empaquetado/publicación es 4 GiB: hasta 2 GiB de páginas temporales más hasta 2 GiB del `.cbz.tmp` o CBZ publicado. La DB, WAL, logs y espacio reservado del volumen requieren presupuestos propios; esta cota no los reemplaza.
- El builder calculará y rechazará antes de abrir/escribir el `.cbz.tmp` si el tamaño esperado, incluidos metadatos y overhead ZIP, excede el límite. También aplicará una guarda de bytes durante la escritura para protegerse ante cambios de origen o discrepancias de estimación.
- La verificación desde disco comprobará el tamaño real y el lector volverá a aplicar el mismo límite al CBZ indexado antes de abrirlo.
- Si el límite se excede, el builder no publica ni indexa el archivo. Conserva cualquier staging ya escrito para diagnóstico; no elimina ni reemplaza un candidato existente.
- Se mantienen el límite aprobado de 64 MiB por página y el máximo vigente de 10 000 páginas por manifest. El máximo agregado efectivo de un CBZ es 2 GiB.

## Alternativas consideradas

- Permitir CBZ sin límite agregado: rechazado porque no acota consumo de disco ni el coste de verificación del lector.
- Revisar el tamaño solo tras terminar el archivo: rechazado porque el disco ya pudo agotarse durante la escritura.
- Reducir el límite de página para inferir un máximo agregado: rechazado porque multiplicar cardinalidad por el límite individual produce una cota poco útil.

## Consecuencias

- El builder, indexador y lector reciben un límite explícito de 2 GiB desde composición y no definen valores divergentes. El downloader recibe además el presupuesto restante de temporales del job.
- Capítulos cuyo contenido más overhead excede el límite fallan antes de publicación, conservando sus páginas validadas.
- Cambiar el límite exige ADR versionado y pruebas de preflight, escritura y lectura al borde.

## Verificación requerida

- Tamaño estimado exactamente en el límite se acepta; un byte por encima se rechaza antes de crear staging.
- Temporales agregados exactamente en el límite se aceptan; un chunk que lo excedería se rechaza antes de escribirlo. Se incluyen parciales previos al reabrir o reintentar.
- Una fuente que crece o cambia después de preflight dispara la guarda durante la escritura; no hay publicación ni indexación.
- Se comprueba el tamaño observado de `.cbz.tmp`, del CBZ publicado y del archivo servido por el lector.
- Disk-full no se reintenta; los bytes previos y candidatos se conservan.
