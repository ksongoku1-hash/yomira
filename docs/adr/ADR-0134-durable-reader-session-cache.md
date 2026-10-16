# ADR-0134 — Sesión de lectura online y caché verificable

## Estado y alcance

Accepted para H2b.2, 2026-10-11. Continúa ADR-0130/0131/0132/0133. Implementa el consumidor del core para páginas progresivas; el lector React y H2 completa siguen pendientes.

Actualización H2c: [ADR-0135](ADR-0135-online-reading-without-image-files.md) sustituye la caché de disco para nuevas lecturas online por imágenes bajo demanda en memoria. Este ADR se conserva como historial y contrato de recuperación del prototipo anterior.

La unidad de progreso sigue siendo el capítulo canónico, no el UUID del proveedor ni un archivo temporal. El proveedor entrega disponibilidad de archivos cerrados; el core decide cuándo los bytes son aptos para leer. Una sesión no representa una descarga ni un CBZ de biblioteca.

## Decisión

1. Añadir `reader_sessions` y `reader_session_pages` mediante la migración 0024, conservando tablas y registros existentes. Una sesión captura capítulo/fuente/conector/version/referencia, generación y total del plan; cada ordinal guarda intento y metadatos de imagen verificada. El índice parcial impide dos sesiones vigentes para el mismo capítulo. Los estados de publicación permiten distinguir una intención durable de una página disponible.
2. Componer `ReaderSessionService` con `SourcePageService`, `DownloadTransport`, `DiskBudgetManager`, `validate_image_file` y `AtomicFilePublisher`. No duplicar el acceso a la fuente ni modificar la construcción/publicación de CBZ. La sesión usa un UUID nuevo por intento; el ID canónico del capítulo permanece estable.
3. Rechazar cambios o regresiones del plan en el punto compartido de acceso: generación, total, referencias ya publicadas y finalización. Mantener la comprobación específica del contrato nativo. Un plan inicialmente desconocido puede establecerse sin sustituir datos verificados.
4. Descargar a los `.part` existentes, persistir el siguiente intento antes de un reintento, validar imagen/hash/tamaño, guardar intención de publicación, promover sin reemplazo y confirmar `ready`. Las referencias y credenciales del proveedor no se devuelven al navegador. Se conservan originales; no se convierten ni recomprimen imágenes.
5. Guardar la caché fuera de la biblioteca, en `cache/reader/{session_id}/{ordinal}.page`. Aplicar el ajuste existente de caché de 16–500 MiB, contado sobre archivos de caché y temporales propios. Reservar también el máximo de staging nativo (512 MiB) con el gestor compartido. La cuota de caché y la reserva nativa son límites distintos.
6. Limpiar sesiones inactivas por acceso reciente. No retirar contenido de un productor activo ni de una respuesta que aún tiene consumidores. Comprobar raíz, namespace, reparse points, hardlinks e identidad antes de borrar; archivos o directorios desconocidos quedan preservados y bloquean la operación. La eliminación pasa por `evicting` antes de tocar archivos, para poder repetirse tras una interrupción.
7. Reutilizar los endpoints de metadata, páginas y progreso del lector, dando prioridad al CBZ local. Mantener páginas desde 1 y `page_index` de progreso desde 0, como el lector existente. El lookup híbrido habilita el total online únicamente con plan establecido y al menos una página validada. El guardado de progreso online exige que el ordinal esté disponible. Añadir únicamente POST/GET/DELETE de la sesión para abrir, consultar y pausar; `restart: true` crea otro intento después de confirmar la liberación del anterior.
8. Al iniciar, recuperar una promoción terminada entre rename y commit verificando los bytes contra su intención. Marcar adquisiciones anteriores como interrumpidas, retirar solamente `.part` conocidos y reconciliar el UUID nativo antes de reintentar. Un conector no disponible permite leer caché válida, pero no autoriza reiniciar un intento cuya liberación no esté demostrada.
9. Diferenciar un UUID ausente mediante 404 `not_found` en la API Pascal. Una ausencia de un intento durable restaurado permite confirmar que no existe una tarea en el proceso actual. Un POST ambiguo del proceso actual mantiene el fallo cerrado. La consulta progresiva inicial puede esperar a que el POST cree la tarea, sin publicar ni borrar archivos.
10. Esperar el fin real de validación/publicación/finalización ejecutadas fuera del event loop antes de liberar reservas o limpiar. Las cancelaciones conservan la página cuya promoción terminó, para recuperarla posteriormente. Una limpieza nativa diferida conserva la reserva y vuelve a comprobarse antes de admitir un reinicio.

## Consumidores y protección de funciones existentes

`lifecycle` compone y recupera la sesión antes de arrancar el dispatcher, y detiene los productores antes de cerrar transporte/conector/DB. Si la caché tiene propiedad o integridad inseguras, deshabilita la lectura online y conserva biblioteca, CBZ y progreso local.

`routes/reader.py` ofrece las páginas mediante la ruta existente y no expone el token nativo. `ReaderProgressService` incorpora una comprobación de disponibilidad opcional; sus otros consumidores conservan el comportamiento anterior. Las rutas de descarga, exportaciones, construcción de CBZ y ComicInfo no cambian. Las pruebas que esperaban el último schema se actualizan a 0024; la historia anterior sigue append-only.

## Límites explícitos

- La caché retiene capítulos completos por sesión activa. Si un capítulo supera la cuota, la adquisición falla conservando las páginas verificadas; H5 debe introducir una ventana de páginas y recuperación de páginas retiradas. No se convierte ese parcial en un capítulo terminado.
- Dos respuestas de imágenes de caché pueden mantenerse simultáneamente, con un máximo de 64 MiB por respuesta. Se verifican y materializan bytes antes de responder: hasta 128 MiB de contenido, además de buffers y otros servicios. El navegador conserva su presupuesto independiente.
- La reducción de cuota se aplica en el próximo ingreso, reintento o arranque; no hay limpieza periódica ni recorte inmediato desde la pantalla de ajustes en este incremento.
- Se conservan identidades de sesiones retiradas para trazabilidad/reconciliación; su historial en SQLite todavía no tiene compactación.
- Se elige la primera fuente habilitada/disponible por identidad estable. No se implementa fallback entre fuentes, ni deduplicación/reutilización de caché como entrada de una descarga. Lectura y descarga comparten conector y transporte, pero tienen intentos separados.
- Historial y listado de biblioteca siguen sus reglas actuales; H4 aún debe integrar presentación online/local. El CBZ local no depende de la caché.

## Aceptación y reversión

Las pruebas de sesión demuestran primera página por la API antes del final, rechazo de HTML, progreso solamente en páginas disponibles, protección de respuestas en uso, archivos alterados/desconocidos/hardlinks preservados, reapertura SQLite, recuperación de promoción interrumpida, fuente ausente, pausa/reinicio y finalización diferida. El ensayo Windows usa exclusivamente una fuente Lua e imágenes sintéticas por loopback; prueba además el API del lector con el ejecutable compilado, la liberación del staging y el progreso retenido.

El punto `Yomira-H2b1-antes-de-H2b2.zip` conserva el backend anterior; `Yomira-H2b1-recuperacion-completa.zip` conserva todo el código del mismo commit base. La migración normal realiza el backup existente antes de cambiar schema. No se aplica sobre la instalación o biblioteca personal durante este desarrollo. El downgrade de 0024 rechaza eliminar tablas si hay evidencia de sesiones; una reversión con datos requiere conservar la DB o restaurar un backup íntegro, no borrar registros de lectura.

Los resultados de ejecución y los hashes de los artefactos de prueba se conservaron como evidencia local; no forman parte del código fuente publicado.
