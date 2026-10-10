# ADR-0025 — Runtime SQLite, transacciones y migraciones

## Status

Accepted — decisión de implementación de Fase 4, antes de K034.

## Context

La especificación exige SQLite WAL, claves foráneas, espera acotada por locks, escrituras centralizadas y breves, y migraciones con backup e inspección de integridad. Los valores exactos, el límite de conexiones y la relación entre Alembic y `schema_history` aún no estaban fijados.

## Decision

- Usar SQLAlchemy 2.0 en modo síncrono y Alembic; fijar versiones exactas en `requirements.in` y `requirements.lock`. K036 fija Alembic 1.20.0. Las sesiones son de una sola operación y nunca se comparten entre hilos o requests.
- Requerir SQLite 3.46.0 o posterior para disponer de `PRAGMA optimize` con límites de análisis temporales; el runtime fijado para desarrollo contiene SQLite 3.50.4 y el gate comprobará esa versión.
- La ruta de la DB se inyecta al crear el servicio de persistencia. El motor no adivina una ubicación de datos ni crea raíces de biblioteca.
- Limitar el pool a cuatro conexiones, sin overflow, con espera máxima de cinco segundos. Cada conexión tiene page cache de 2 MiB, `foreign_keys=ON`, `busy_timeout=5000` y `synchronous=FULL`. En la creación se establece `journal_mode=WAL` y se verifica que SQLite lo haya aceptado. Un incumplimiento detiene la inicialización con error tipado. Cada conexión nueva configura y vuelve a verificar sus PRAGMAs.
- Todas las escrituras se ejecutan por el único coordinador de escritura y en transacciones breves. La sesión de lectura activa `query_only`; si un lock persiste tras el timeout, se propaga un error DB trazable. No se reintenta una transacción de resultado incierto.
- Durante el cierre se rechazan sesiones nuevas y se esperan hasta cinco segundos las lecturas/escrituras activas. Si una sesión sigue abierta, `close()` falla sin disponer la DB, para que el llamador conserve el control y el mutex de instancia.
- Solo Alembic modifica el esquema. Antes de cada migración, el coordinador detiene escrituras, verifica la DB existente y crea un backup consistente con SQLite Backup API en el directorio de backups inyectado. El runner conserva los tres backups automáticos pre-migración más recientes; los backups manuales no coinciden con su patrón de retención. Tras migrar, comprueba `integrity_check`, la revisión Alembic, las tablas esperadas y el registro de `schema_history`. Si algo falla, el arranque normal no continúa: se conserva el backup y se restaura únicamente cuando la copia se verifica y no hay escritores activos; de lo contrario se conserva la evidencia y se requiere Safe Mode.
- `alembic_version` es la autoridad de la revisión actual. La revisión base de K036 crea `schema_history` y sus protecciones append-only; K046 añadirá el modelo de aplicación. `schema_history` registra cada transición y su revisión resultante, no es una segunda autoridad; divergencia entre ambos bloquea el arranque normal.
- Las operaciones SQL síncronas no se ejecutan directamente en el event loop. Los handlers serán síncronos o el servicio llamará a un worker thread con límite explícito.

## Alternatives Considered

- SQLAlchemy async con aiosqlite: añade otro runtime de acceso a SQLite y un modelo de concurrencia que no mejora la serialización de escritura requerida.
- Pool sin límite fijo: permite que la concurrencia de requests abra un número impredecible de conexiones y cachés SQLite.
- Confiar en defaults de SQLite sin comprobarlos: una conexión puede arrancar con claves foráneas deshabilitadas o con durabilidad inferior a la acordada.
- Crear esquema mediante `metadata.create_all()`: evita el historial reversible y el preflight obligatorio de Alembic.
- Reintentar automáticamente una transacción completa después de un timeout: puede repetir efectos si no se sabe si el commit llegó a persistirse.

## Positive Consequences

- El máximo de conexiones, la memoria de caché y la espera por lock son acotados.
- `synchronous=FULL` prioriza durabilidad ante caída del sistema o pérdida de energía, de acuerdo con la regla de integridad.
- Backup, migración y verificación son pasos explícitos y trazables.

## Negative Consequences

- `synchronous=FULL` y el pool pequeño pueden reducir rendimiento; cualquier cambio posterior requiere mediciones y un ADR.
- Una DB que no supera integridad o que tiene historial de esquema divergente no se abre en modo normal.

## Failure Modes

- Se omite un PRAGMA en conexiones nuevas, se comparte una sesión o se escribe fuera del coordinador.
- Un backup incompleto se trata como válido o se restaura sin comprobarlo.
- Alembic avanza y `schema_history` queda desincronizado.

## Reversal Strategy

Antes de cambiar PRAGMAs, pool, forma de acceso o autoridad de esquema, crear y verificar backup, probar migración reversible con fixtures de DB anteriores y actualizar este ADR. No reducir durabilidad como optimización sin benchmark y aprobación de integridad.

## References

- [SQLite synchronous pragma](https://www.sqlite.org/pragma.html#pragma_synchronous)
- [SQLite Online Backup API](https://www.sqlite.org/backup.html)
- [Alembic Migration Environment](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [Alembic SQLite batch migrations](https://alembic.sqlalchemy.org/en/latest/batch.html)
