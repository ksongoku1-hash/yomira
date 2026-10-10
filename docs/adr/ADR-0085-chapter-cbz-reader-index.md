# ADR-0085 — Vínculo durable de CBZ publicado y acceso del lector

## Estado

Accepted — autoridad arquitectónica, 2026-09-27. No modifica la SPEC aprobada ni el plan canónico. El servicio de lectura no se habilita en runtime hasta que composición inyecte límites operativos aprobados.

## Contexto

La SPEC requiere que un CBZ se publique y verifique antes de indexarlo, que el lector nunca lea el filesystem directamente y que los CBZ publicados permanezcan inmutables mientras se leen. `assets` describe el archivo físico, `DownloadJob` registra el trabajo que lo produjo y `chapters` identifica la entidad de biblioteca, pero no existe una relación durable que pruebe qué job completó qué capítulo ni qué CBZ debe abrir el lector.

V1 necesita una relación única y transaccional que impida tener más de un CBZ activo por capítulo y que permita consultar páginas listas sin exponer rutas locales. La especificación no establece reemplazo de CBZ ni valores para límites de streaming.

## Decisión

### Relación durable

1. Añadir la tabla `chapter_cbz_assets` mediante una migración Alembic aditiva.
2. `chapter_id` será la clave primaria y FK `RESTRICT` a `chapters.chapter_id`; por construcción, cada capítulo tendrá como máximo un vínculo CBZ activo en V1.
3. `asset_id` tendrá restricción `UNIQUE` y FK `RESTRICT` a `assets.asset_id`; `download_job_id` tendrá restricción `UNIQUE` y FK `RESTRICT` a `download_jobs.job_id`. Los tres identificadores serán UUIDv4 canónicos. La fila guardará también `page_count` positivo y `indexed_at_us` entero para que los listados y el lector no tengan que inferirlos desde el nombre del archivo.
4. La base de datos no puede expresar con las tablas actuales que `download_job_id` apunta a un job cuyo `chapter_source_id` termina en el mismo `chapter_id`. El servicio de indexación comprobará esa lineage dentro de la transacción de escritura antes de insertar el vínculo.

### Indexación y finalización

1. La entrada de indexación es el resultado inmutable de un CBZ que el builder ya publicó y verificó. Antes de escribir en SQLite, el servicio vuelve a comprobar la identidad del DataRoot, que cada componente del path relativo no atraviese un enlace/reparse point, que el destino sea un archivo regular, y que su tamaño y SHA-256 coincidan con el resultado verificado.
2. El servicio exige que el job esté en `INDEXING`, sin cancelación pendiente ni cuarentena de recovery, que el asset sea `CBZ`, y que el `chapter_source` del job pertenezca al `chapter_id` indicado. También comprueba que las páginas verificadas del job formen una secuencia completa de ordinales y que coincidan en número, tamaño, MIME y SHA-256 con las entradas de imagen del CBZ.
3. En una sola transacción SQLite de escritura se inserta `AssetRow`, se inserta `chapter_cbz_assets` y se hace CAS del job `INDEXING → COMPLETED`. Si cualquier comprobación o escritura falla, la transacción revierte las tres mutaciones y conserva el archivo publicado para diagnóstico/reintento; el servicio no elimina temporales ni decide reemplazos.
4. Una repetición de una operación ya completada solo es idempotente cuando job, capítulo y resultado de asset coinciden exactamente con el vínculo existente. Un vínculo distinto en el mismo capítulo es conflicto y nunca se sustituye en V1.

### Acceso del lector

1. `ReaderAssetService` lista por serie únicamente capítulos que tengan una relación durable a un asset `CBZ` verificado, resuelve páginas usando el `DownloadPage` del job vinculado y el nombre estable `NNN.ext`, donde `ext` se deriva del MIME validado. No devuelve ni acepta rutas del filesystem en el contrato HTTP.
2. El endpoint V1 conserva `GET /api/v1/reader/{chapter_id}` y `GET /api/v1/reader/{chapter_id}/pages/{page}`; se añade `GET /api/v1/reader/series/{series_id}/chapters` para enumerar solo capítulos listos. La página se transmite por chunks. Los límites de `ReaderAssetService` son obligatorios e inyectados: `max_cbz_bytes=2,147,483,648`, `max_page_bytes=67,108,864` y `chunk_bytes=65,536`, según ADR-0086 y ADR-0088. No hay valores por defecto y la composición de runtime debe inyectarlos antes de habilitar el servicio.
3. Antes de responder, el lector verifica el archivo CBZ regular contra el SHA-256/tamaño indexados y verifica completamente la página solicitada contra su SHA-256/tamaño/MIME de `DownloadPage`. La respuesta usa ETag del SHA-256, `Cache-Control: private, no-transform` y el MIME validado. No se implementa Range ni caché adicional en este slice.
4. El stream sostiene abierto el CBZ mientras entrega la página. El contrato de V1 trata el archivo publicado como inmutable; esta ADR no autoriza reemplazarlo, borrarlo ni desempacar páginas a rutas controladas por el cliente.

## Consecuencias

- La indexación del asset, el vínculo único capítulo→CBZ y `COMPLETED` quedan atómicos en SQLite después de verificar el filesystem.
- Un job/asset/capítulo incompatible no puede completar ni aparecer como listo en biblioteca/lector.
- El listado de capítulos listos y el lector dependen de que runtime inyecte los límites V1 aprobados en ADR-0086/0088; la ausencia de configuración mantiene el servicio deshabilitado.
- Una falla ambiental después de publicar el CBZ puede dejar un archivo no indexado. Se conserva para diagnóstico y una operación posterior puede indexar solo después de repetir todas las verificaciones.
- Reemplazo, borrado, caché de páginas y soporte Range requieren decisión y gates separados.

## Pruebas requeridas

- Migración aditiva 0018: creación de restricciones/FKs, preservación de filas existentes y downgrade bloqueado mientras existan vínculos.
- Indexación: lineage incorrecta, job no `INDEXING`, cancelación/recovery pendiente, asset no-CBZ, path inseguro, symlink/reparse, archivo no regular, hash/tamaño divergente, página faltante/duplicada/discordante y vínculo existente deben fallar sin cambiar el estado ni exponer el asset.
- Atomicidad: errores al insertar asset, vínculo o actualizar el job dejan las tres tablas y el estado previos intactos.
- Idempotencia: la misma indexación completada se reconoce; nunca se reemplaza el vínculo de un capítulo.
- Reader: solo capítulos indexados aparecen; UUID inexistente/no listo y ordinal inválido fallan; path no se entrega al cliente; límites explícitos impiden páginas y archivos CBZ mayores al máximo; el streaming devuelve bytes, MIME, ETag y cache headers coherentes; una discrepancia posterior del archivo o página no devuelve contenido.
