# ADR-0045 — Raíz local de datos e inicialización de la biblioteca en runtime

## Status

Accepted for implementation of the normal K050 runtime composition — 2026-09-26.

## Context

K049–K058 implementan la base SQLite, migraciones, servicios y API de biblioteca. Las rutas HTTP dependen de `LibraryService` y `MetadataService`, pero el ciclo de vida normal solo marca el runtime como listo y no inyecta esos servicios; por eso la biblioteca devuelve 503 aunque la app informe readiness. La especificación define la estructura `YomiraData/` y exige rutas relativas a una biblioteca portátil, pero no fija la ubicación absoluta de `YomiraData/` en Windows.

## Decision

- La raíz de datos por defecto será `%LOCALAPPDATA%\Yomira\YomiraData`. Si `LOCALAPPDATA` no está disponible, se usa la ruta Windows equivalente bajo el perfil del usuario: `%USERPROFILE%\AppData\Local\Yomira\YomiraData`.
- `YOMIRA_DATA_ROOT` permite elegir explícitamente otra raíz, incluso en un disco portátil o en pruebas. Debe ser absoluta. Una ruta inválida impide el arranque con `SYS_INVALID_CONFIG`; nunca se interpreta con relación al directorio actual ni se cambia silenciosamente al default.
- `data_root` es un setting tipado de bootstrap. La base se guarda en `data_root/database/yomira.sqlite3` y los backups de migración en `data_root/backups/`.
- El arranque crea solo los directorios necesarios para abrir la base y ejecutar migraciones. No crea ni modifica la biblioteca del usuario ni presupone que `library_root` esté dentro de `data_root`.
- Bajo el mutex de instancia, el arranque abre SQLite, ejecuta `MigrationRunner.upgrade_to_head()` con su backup verificado, comprueba el esquema y solo entonces crea `LibraryService` y `MetadataService`. `runtime.ready` pasa a verdadero únicamente tras completar toda la composición.
- Si la apertura o migración falla, el runtime permanece no listo; cualquier conexión ya asignada se cierra antes de liberar el mutex. Un reinicio puede volver a intentar el arranque, sujeto a las garantías de backup, integridad y restauración del migrador.
- `library_root` sigue siendo una configuración independiente. Esta decisión no elige su ubicación ni autoriza crearla, mover archivos o resolver rutas de Assets durante el arranque.

## Alternatives Considered

- Guardar `YomiraData/` en el directorio de trabajo: depende de cómo se inició la app y puede mezclar datos con el código o el instalador.
- Usar `YomiraData/library/` automáticamente como biblioteca: anula la portabilidad y podría crear o cambiar una raíz de biblioteca sin consentimiento explícito.
- Mantener el runtime sin servicios y aceptar 503: deja la aplicación normal desconectada de la biblioteca local que ya implementan K049–K058.
- Migrar datos automáticamente cuando cambia `YOMIRA_DATA_ROOT`: puede crear dos bases divergentes o sobrescribir contenido sin una verificación explícita.

## Consequences

- El runtime normal servirá una biblioteca vacía con HTTP 200 cuando la base recién creada migre correctamente.
- Los tests y los despliegues portátiles pueden aislar todos los datos mediante una raíz absoluta temporal o elegida expresamente.
- Cambiar la raíz no mueve ni borra la base anterior. Una futura herramienta de traslado deberá verificar backup, integridad, conteos y recuperación antes de activar la nueva ubicación.
- El inicio sin conexión no depende de la red; la apertura de la biblioteca solo necesita disco local y espacio para SQLite y el backup de migración.

## Failure Modes

- `YOMIRA_DATA_ROOT` es relativa, no accesible o no permite crear el subdirectorio `database`: el arranque falla de forma trazable y nunca cae al directorio de trabajo.
- El disco se llena durante el backup o la migración: readiness no se activa y el migrador conserva/restaura los datos según su contrato; no se inicializan servicios sobre un esquema parcial.
- Se libera el mutex antes de cerrar una base que quedó abierta tras un error de startup: otra instancia podría abrir el mismo archivo durante la limpieza.
- El runtime marca ready antes de que las migraciones y los servicios estén disponibles: `/api/v1/library` podría fallar pese a reportar la app lista.
- Se confunde `data_root` con `library_root` y se crean, reubican o borran archivos de la biblioteca sin autorización.

## Reversal Strategy

Cambiar la ubicación predeterminada o la semántica de `YOMIRA_DATA_ROOT` requiere un nuevo ADR. No mover ni reemplazar automáticamente una base existente. Toda migración de ubicación debe conservar la fuente, crear y verificar un backup, comprobar integridad y revisión de esquema, y permitir volver a la ubicación anterior hasta validar la apertura de la nueva base.
