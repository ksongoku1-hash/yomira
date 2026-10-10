# ADR-0037 — Settings versionados y modelo del historial de esquema

## Status

Accepted — decisión de implementación de K046, conforme a las secciones 54–55, ADR-0018 y ADR-0025.

## Context

La especificación exige configuración tipada y versionada, pero no enumera el catálogo completo de claves ni prescribe columnas para `settings`. `schema_history` ya existe desde `0001_schema_history` y conserva cada aplicación de migración mediante triggers append-only. K066 asigna tokens y cookies al Secret Vault, separados de la configuración en texto plano.

## Decision

- `settings` mantiene una fila por `setting_key` con `value_json`, `schema_version` positivo y `updated_at_us`. La PK impide que una misma clave tenga varios valores simultáneos.
- `setting_key` es texto no vacío; los nombres reconocidos, tipos, defaults, rangos, permisos de edición y reinicio pertenecen al registry tipado de configuración. La tabla conserva valores JSON y su versión sin reinterpretarlos.
- El dominio valida que `value_json` sea JSON válido y finito. La migración y SQLite exigen JSON textual válido, versión entera positiva y timestamp UTC entero. Un cambio de formato de valor requiere nueva versión de settings y conversión explícita, preservando datos que no pueda convertir.
- `settings` contiene configuración no secreta. Los tokens y cookies nunca se almacenan ahí; cuando K066 lo implemente, la configuración persistirá solo la referencia opaca que el contrato del Vault permita.
- `SchemaHistoryRow` refleja las columnas, tipos, PK, AUTOINCREMENT y CHECK de `0001_schema_history`; no cambia ni recrea esa tabla. Los triggers append-only continúan siendo la autoridad que bloquea UPDATE y DELETE.
- El código normal de la aplicación no escribe eventos de migración. Alembic sigue siendo el único escritor de `schema_history`, mediante su callback transaccional.

## Alternatives Considered

- Una columna SQLite por ajuste: requiere una migración de DB por cada setting y dificulta preservar claves que una versión anterior desconozca.
- JSON sin `schema_version`: no permite seleccionar una conversión segura al cambiar el contrato de configuración.
- Guardar secretos como valores normales: contradice ADR-0018 y expone credenciales a logs, exports y Support Bundle.
- Reconstruir `schema_history` para añadir constraints: arriesga perder evidencia y puede desactivar triggers que protegen el historial.
- Permitir que servicios de producto escriban `schema_history`: confunde cambios de dominio con migraciones y rompe la auditoría de Alembic.

## Positive Consequences

- Las claves de configuración desconocidas pueden preservarse durante lectura o migración sin ser aceptadas por el registry como configuración activa.
- La versión queda junto al valor y permite una conversión verificable.
- El historial previo de migraciones conserva identidad, orden y protección append-only.

## Negative Consequences

- El servicio futuro debe validar cada JSON contra el tipo y los rangos del registry antes de aplicarlo.
- La tabla por sí sola no cifra valores; la política de configuración no secreta debe cumplirse en las fronteras de escritura.

## Failure Modes

- Se guarda una credencial en `value_json` en vez de su referencia opaca del Vault.
- Un valor JSON válido tiene un tipo o rango distinto al esperado por el registry y se activa sin validación.
- Una conversión de versión reemplaza un valor desconocido o inválido con un default sin preservar el dato original.
- Se modifica o elimina un evento de `schema_history` fuera de Alembic o se deshabilitan sus triggers.

## Reversal Strategy

Cambiar formato JSON, clave, versión o políticas de secreto requiere ADR y migración con backup. Las conversiones deben validar tipo, rango y versión por clave; los valores no convertibles se conservan para recuperación. `schema_history` se mantiene append-only y no se reconstruye ni elimina.
