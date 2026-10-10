# ADR-0096 — Self-test de arranque y retención acotada de logs

## Estado

Accepted — 2026-09-27. Implementa K226 (§60) y K235 (§79) sin añadir umbrales de espacio libre que la SPEC no define.

## Contexto

K226 exige comprobar antes de declarar readiness que la configuración, SQLite/schema, filesystem, directorio temporal, biblioteca y consulta de espacio están disponibles. El arranque ya migra y valida el schema, pero no agrupaba estas verificaciones como un gate. K235 pide persistencia de logs configurable y finita; la aplicación solo emitía JSON a stderr.

El self-test debe distinguir un espacio consultable de un espacio suficiente: la segunda pregunta pertenece al presupuesto de disco de cada operación. Un fallo de almacenamiento de logs tampoco debe exponer rutas/secretos ni alterar el resultado de una escritura de datos.

## Decisión

1. Después de `MigrationRunner.upgrade_to_head()` y antes de reconciliar trabajos o declarar readiness, ejecutar K226. El DB probe usa `SELECT 1`; la compatibilidad compara `alembic_version` y el último `schema_history` con la revisión head que devolvió la migración.
2. Verificar el data root capturado y accesible; crear `temp/` y `library/` solo si faltan. Rechazar directorios que sean symlink/reparse point o no sean directorios. Enumerar la raíz y la biblioteca para comprobar lectura.
3. Para K226, crear un archivo único bajo `temp/`, escribir y releer un marcador fijo, cerrarlo y eliminarlo; confirmar su ausencia. Reintentar la limpieza en `finally`; si cualquier paso falla, no declarar readiness y conservar el error clasificado.
4. Consultar `disk_usage(data_root)` y validar que total/usado/libre sean enteros no negativos. **No exigir espacio libre mayor que cero** en este self-test: §60 no define un mínimo. Un resultado cero es una medición válida; los `DiskBudgetManager`/preflights de escritura deciden si un trabajo cabe.
5. Los fallos de checks usan `SYS_STARTUP_FAILED` con `stage` y `failed_check`; una configuración inválida usa `SYS_INVALID_CONFIG`. El log registra solo el identificador estable del check, `cause_type` y campos del catálogo. No registra texto de excepción, contenido del marcador, secretos ni rutas.
6. Persistir eventos JSON en `data_root/logs/yomira.log` con rotación por tamaño. Defaults: 10 MiB por archivo y 5 archivos totales (archivo activo más cuatro backups), de acuerdo con los 10 MB y 5 archivos sugeridos en §79. La configuración vía `YOMIRA_LOG_MAX_BYTES` acepta 32 KiB–25 MiB; `YOMIRA_LOG_FILE_COUNT` acepta 2–10 porque la librería de rotación requiere al menos un backup para mantener el límite. El total permanece acotado por 250 MiB; el default ocupa como máximo 50 MiB.
7. Limitar cada registro serializado a 16 KiB. Si el archivo/rotación no se puede abrir o escribir, reportar un evento genérico por stderr; al primer error de escritura se deshabilita solo el file handler. No propagar rutas ni mensajes de excepción y no cambiar el resultado de la operación de datos.

## Alternativas consideradas

- Fallar el arranque cuando `free == 0`: rechazado porque §60 pide verificar la métrica, no fija una reserva; la reserva y estimación pertenecen a §61.
- Rotar con `backupCount=5` además del archivo activo: rechazado porque produciría seis archivos, no el sugerido de cinco.
- Hacer fallar una descarga o mutación cuando el log no se puede escribir: rechazado porque logs son diagnóstico, no fuente de verdad ni parte de la transacción de datos.
- Registrar mensajes completos de excepción o paths absolutos para explicar fallos: rechazado por privacidad y porque la clase de causa y etapa son suficientes para triaje inicial.

## Consecuencias

- Un runtime no queda listo con DB/schema incompatible, directorios ilegibles o una prueba temporal incompleta.
- Una instalación nueva obtiene `library/` y `temp/` vacíos y utilizables; nunca se reemplaza un archivo preexistente en esas ubicaciones.
- Cero bytes libres no causa por sí solo rechazo de arranque. Cualquier operación de escritura debe pasar su presupuesto y puede rechazarla sin cambiar el estado a éxito.
- Los eventos de archivo son rotables y finitos; stderr conserva la vía de diagnóstico cuando el medio de logs falla. Los registros no incluyen mensajes de excepción ni rutas absolutas.
- El self-test comprueba el acceso del filesystem y no sustituye las pruebas físicas de Windows, fallos de disco, recovery o gates de release.

## Verificación requerida

- Aceptar un disk usage consultable con cero bytes libres y rechazar una consulta no disponible.
- Rechazar schema incompatible, configuración inválida y biblioteca ocupada por un archivo sin revelar rutas.
- Confirmar create/write/read/delete del probe y que no deja residuos en el caso normal.
- Confirmar rotación al número total configurado, límite de tamaño agregado y privacidad del contenido persistido.
- Forzar fallo de escritura de archivo y verificar fallback por stderr sin propagarlo al caller.
- Integrar K226 en el lifespan y ejecutar pruebas focales, Ruff y Pyright.
