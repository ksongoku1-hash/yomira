# ADR-0087 — Reintentos acotados para locks transitorios de Windows

## Estado

Accepted — valores aprobados explícitamente por el usuario el 2026-09-27. Implementa K108 y complementa ADR-0078, ADR-0079 y ADR-0081.

## Contexto

Indexadores y antivirus pueden mantener locks transitorios durante operaciones de archivos. Repetir indiscriminadamente una mutación puede sobrescribir contenido o duplicar una publicación si el primer resultado fue ambiguo. No reintentar nunca puede fallar ante un lock breve recuperable.

## Decisión

- Aplicar como máximo cuatro reintentos tras el intento inicial, con esperas de 100 ms, 200 ms, 400 ms y 800 ms.
- Reintentar solamente códigos Win32 `ERROR_SHARING_VIOLATION` (32), `ERROR_LOCK_VIOLATION` (33) y `ERROR_SHARING_BUFFER_EXCEEDED` (36).
- No reintentar `ERROR_DISK_FULL`, `ERROR_HANDLE_DISK_FULL`, `SQLITE_FULL`, acceso denegado, rutas inválidas, destino existente ni otros errores permanentes.
- Las operaciones de lectura/stat/hash se repiten desde el inicio únicamente después de cerrar cualquier handle previo.
- Una mutación filesystem solo se reintenta si se demuestra que no mutó: origen conserva los mismos bytes verificados y destino permanece ausente. Si el resultado es ambiguo, se preservan candidatos y se reclasifican bajo el lock del job; no se invoca otra vez a ciegas `MoveFileExW`, `ReplaceFileW`, escritura, flush o cleanup.
- Un error Win32 durante el único intento de cierre del writer tiene resultado no verificado: `close()` puede haber liberado o cerrado parcialmente el handle antes de devolver el error. No se vuelve a invocar `close()` a ciegas. El error se propaga con diagnóstico trazable; el caller falla cerrado, conserva el staging y bloquea la publicación. Si un cierre ejecutado durante el manejo de otra operación falla, esa operación tampoco se reintenta. El diagnóstico de cierre incluye contexto sanitizado y omite ruta y contenido. Esta es una interpretación del contrato de resultado ambiguo y no cambia el runtime.
- Cada operación emite diagnóstico sanitizado con `trace_id`, `job_id` cuando esté disponible, etapa, código nativo y número de intento; no registra rutas personales ni contenido.

## Alternativas consideradas

- Retry ilimitado o retry para todos los errores: rechazado porque puede ocultar fallos permanentes, consumir recursos sin cota o repetir una mutación ya aplicada.
- Reintentar un resultado ambiguo: rechazado porque puede sobrescribir o destruir evidencia.
- Fallar en el primer sharing/lock transitorio: rechazado porque hace frágil la escritura bajo indexadores y antivirus de Windows.

## Consecuencias

- El tiempo de espera acumulado máximo por operación es 1.5 s, aparte del intento inicial.
- El código de filesystem debe clasificar códigos nativos y demostrar que no hubo mutación antes de un retry de escritura/publicación.
- Tests deben inyectar las operaciones Win32, sleep y verificación de archivos para cubrir liberación y persistencia del lock.
- Un fallo después de agotar retries conserva bytes previos y cualquier candidato ambiguo; el job sigue visible con error trazable.

## Verificación requerida

- Locks liberados durante cada espera producen éxito sin reemplazar destino previo.
- Lock persistente agota exactamente cuatro reintentos y conserva origen/destino.
- Locks transitorios en `flush` y `fsync` reintentan solo tras cerrar el handle previo y demostrar identidad/tamaño sin cambio; un lock persistente en cada operación agota exactamente cuatro reintentos, conserva los bytes e identidad, y emite diagnóstico sanitizado.
- Un error ambiguo de `close()` no causa una segunda llamada: se conserva el candidato, se bloquea la publicación y se emite diagnóstico sanitizado.
- Disk-full, permisos, conflicto y errores no incluidos fallan sin espera/retry.
- Una mutación aplicada con respuesta de error se reclasifica sin repetir la operación.
- Se verifica trazabilidad sanitizada y el tiempo acumulado máximo de 1.5 s.
