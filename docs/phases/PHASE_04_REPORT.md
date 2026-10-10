# Informe — Fase 4: Base de datos y dominio

**Estado:** PASS  
**Fecha:** 2026-09-26  
**Tareas:** K034–K048  
**Alcance:** motor SQLite, migraciones Alembic, modelos de persistencia, invariantes de dominio y gate de base de datos.

## Resultado

K034–K048 están completadas. El motor configura y verifica WAL, claves foráneas, `busy_timeout`, `synchronous=FULL`, caché acotada y pool fijo. Las escrituras se serializan y las sesiones tienen espera acotada. Alembic es la autoridad de esquema; las migraciones crean backups verificados, comprueban integridad y foreign keys, validan el esquema y restauran el backup si una migración falla parcialmente.

Al cierre de Fase 4, la cabeza de esquema era `0011_settings`. K051 añadió después `0012_cover_asset_type` para exigir que las portadas referencien un Asset de tipo `COVER`. Los dominios persistentes cubiertos son `Series`, `Source`, `SeriesSource`, `Chapter`, `ChapterSource`, `Asset`, `DownloadJob`, `DownloadPage`, `ReaderProgress`, `Setting` y `SchemaHistory`. Sus pruebas verifican identidad, unicidad, rangos, restricciones de borrado y relaciones conforme a sus ADR.

K047 integra `PRAGMA optimize` y `VACUUM` condicional al cierre seguro. La DB drena sesiones, calcula espacio incluyendo WAL y sidecars, exige 25% de páginas libres y una reserva de `2 × tamaño + 64 MiB`; luego comprueba `integrity_check`. Los errores se registran como `DB_MAINTENANCE_FAILED`; el archivo se conserva y el cierre continúa controladamente. El mutex permanece tomado hasta cerrar el pool.

## Evidencia de K048 — DB Gate

- `test_upgrade_creates_verified_backup_and_append_only_history`: upgrade completo, backup íntegro, historial append-only e idempotencia.
- `test_backup_failure_stops_before_any_migration`: un backup fallido detiene el upgrade antes de modificar el esquema.
- `test_failed_migration_restores_pre_migration_database`: una migración que confirma DDL parcial se revierte desde backup; los datos previos quedan y el DDL parcial desaparece.
- `test_database_integrity_check_rejects_existing_orphan_asset_reference`: el preflight rechaza una base con referencia huérfana.
- Las pruebas por dominio ejercitan constraints e inserciones con FK inválida; el runner verifica `integrity_check`, `foreign_key_check`, versión Alembic, tablas requeridas e historial.
- K047 añade pruebas de fragmentación alta, baja fragmentación, sesiones activas, espacio insuficiente, bloqueo SQLite, preservación y cierre antes de liberar el mutex.

## Verificación global

El quality gate completo se ejecutó con las versiones fijadas y terminó con `QUALITY GATE: PASS`.

| Verificación | Resultado |
|---|---|
| Python / SQLite / Node / npm | Python 3.14.7 / SQLite 3.50.4 / Node 24.21.0 / npm 11.19.0 |
| Dependencias Python | `pip check`: sin requisitos rotos |
| Backend | 117 pruebas aprobadas |
| Ruff | PASS |
| Pyright | 0 errores, warnings ni informations |
| Frontend | 8 pruebas aprobadas; TypeScript y ESLint PASS |
| Build | Vite PASS |
| Integración | Smoke PASS en loopback, API y cierre |

## Límites y siguiente fase

La factoría todavía no resuelve ni crea una ruta de DB de usuario. `SQLiteDatabase` y `MigrationRunner` reciben rutas por inyección, como exige ADR-0025; no se inventó una ubicación persistente. La fase de biblioteca deberá componer estos servicios con la ruta de datos definida antes de realizar operaciones de usuario. No hay aún biblioteca funcional, conectores, descargas ni lector.

## Decisión de fase

K034–K048 cumplen sus criterios y el gate completo pasa. Fase 4 queda `PASS`; puede iniciar Fase 5 con composición explícita de persistencia y sin cambiar la política de rutas aprobada.
