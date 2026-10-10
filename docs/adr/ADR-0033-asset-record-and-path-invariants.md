# ADR-0033 — Registro Asset, rutas y referencia de portada

## Status

Accepted — decisión de implementación de K042.

## Context

La especificación define seis tipos de Asset y exige ruta relativa, SHA-256, tamaño y timestamps. ADR-0008 prohíbe depender de rutas absolutas y exige resolver y validar bajo una raíz autorizada. ADR-0026 ya fija UUIDv4, hashes SHA-256 hexadecimales minúsculos, tamaños enteros no negativos, timestamps UTC en microsegundos y FKs `RESTRICT`. ADR-0028 requiere además la FK de `Series.cover_asset_id` a `assets.asset_id`. La ubicación externa de `EXPORT_COPY` pertenece al destino del flujo de exportación y no debe codificarse como una ruta absoluta en esta tabla.

## Decision

- `asset_id` es UUIDv4 canónico. `asset_type` acepta solo `COVER`, `THUMBNAIL`, `CBZ`, `PAGE_CACHE`, `TEMP` o `EXPORT_COPY`.
- `relative_path` se guarda en forma relativa, canónica con separadores `/`, bajo la raíz autorizada que corresponda al flujo. Se rechazan ruta vacía, raíz absoluta, separadores `\\`, `.` y `..` como segmentos, segmentos vacíos, `:`, caracteres inválidos de Windows y nombres de dispositivo reservados. La validación del registro es léxica; el servicio filesystem futuro debe resolver la ruta y verificar que no escape la raíz antes de cualquier acceso, como exige ADR-0008.
- Para `EXPORT_COPY`, la raíz externa pertenece al destino de exportación y se resuelve mediante el flujo ExportJob futuro; K042 no guarda una ruta absoluta ni implementa dicho flujo.
- `sha256` es exactamente 64 caracteres hexadecimales minúsculos. No es único: distintos Assets pueden referirse al mismo contenido sin fusionar registros o rutas.
- `size_bytes` es entero SQLite no negativo; cero se permite. No se presume un tamaño máximo no definido por la especificación.
- `created_at` es obligatorio y `verified_at` anulable hasta que exista verificación. Ambos son fechas conscientes de zona horaria y se almacenan como Unix UTC en microsegundos. No se impone orden entre ambos por posibles importaciones y ajustes del reloj del sistema.
- No hay unicidad por `relative_path`; la identidad lógica es `asset_id` y el hash sirve para buscar/revisar duplicados sin colapsar metadata.
- `Series.cover_asset_id` mantiene nulabilidad y recibe FK `ON DELETE RESTRICT` a `assets.asset_id`. La migración crea `assets` y añade la FK mediante reconstrucción de `series`. Si filas existentes contienen IDs de portada sin un Asset correspondiente, la migración debe fallar y el runner debe conservar/restaurar su backup; no se limpian ni inventan Assets automáticamente.
- SQLite no permite cambiar `PRAGMA foreign_keys` dentro de una transacción y `ON DELETE RESTRICT` bloquea el `DROP TABLE` necesario para reconstruir `series`. El runner solo lo desactiva con la conexión exclusiva y sin transacción durante Alembic; verifica `PRAGMA foreign_key_check` para toda la DB y lo vuelve a activar antes de continuar. Si la verificación falla, restaura el backup. La migración K042 vuelve a comprobar FKs justo después del rebuild; ninguna infracción queda aceptada.
- K042 solo crea persistencia y validación de dominio. No abre, crea, mueve, verifica ni elimina archivos.

## Alternatives Considered

- Guardar rutas absolutas o incluir letras de unidad: rompe portabilidad y expone ubicaciones del usuario.
- Tratar `EXPORT_COPY` como siempre relativo a `library_root`: es incorrecto para copias a USB.
- Hacer único el hash o la ruta: fusiona metadata o bloquea casos de uso sin que la especificación lo requiera.
- Borrar `cover_asset_id` desconocidos durante la migración: pierde asociaciones silenciosamente.
- Borrar Assets referenciados en cascada: puede dejar Series sin portada válida y oculta el borrado.
- Verificar el archivo al construir la entidad: introduciría filesystem e I/O en el modelo de dominio.

## Positive Consequences

- Un Asset no necesita una ruta absoluta para conservar su identidad al mover la biblioteca.
- Hash, tamaño y timestamps tienen invariantes comprobables tanto en Python como en SQLite.
- El vínculo opcional de portada queda protegido por la base de datos.
- Los datos de Series sobreviven a la reconstrucción del esquema o la actualización falla con backup recuperable.

## Negative Consequences

- `relative_path` por sí solo no decide cuál es la raíz de una exportación; el flujo ExportJob debe conservar ese contexto.
- Un archivo puede desaparecer o cambiar después de su registro; K042 no lo abre ni lo verifica.
- La reconstrucción de `series` requiere prueba de preservación de filas hijas y de restauración ante FK inválida.
- El runner mantiene enforcement suspendido durante la migración; un fallo al ejecutar la comprobación completa impide el arranque normal.

## Failure Modes

- Un servicio concatena rutas o accede a disco sin resolverlas bajo una raíz permitida.
- Un archivo cambia después de guardar su hash o tamaño.
- Una referencia de portada huérfana hace fallar la migración y se intenta saltar la verificación.
- Se elimina por hash un segundo Asset que solo comparte bytes con el primero.

## Reversal Strategy

Cambiar tipos, unicidad, formato/ruta, hash, semántica de verificación o FK de portada requiere nueva ADR, migración con backup y pruebas de contenido de Series, referencias hijas e integridad referencial.
