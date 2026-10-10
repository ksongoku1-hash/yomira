# ADR-0027 — Mantenimiento SQLite durante cierre seguro

## Status

Accepted — decisión de implementación de Fase 4, antes de K047.

## Context

La alta volatilidad de estados de jobs puede fragmentar SQLite. K047 requiere `VACUUM` y `PRAGMA optimize`, integrados al cierre seguro o a un proceso programado, con evaluación de espacio, trazabilidad y DB utilizable si el mantenimiento falla. Ejecutar `VACUUM` en cada cierre desperdiciaría I/O y espacio cuando no hay fragmentación; ejecutarlo con escrituras o transacciones abiertas puede fallar.

## Decision

- Integrar el mantenimiento al cierre seguro mientras el mutex de instancia única aún pertenece al proceso. Primero se desactiva readiness, se detienen escrituras, se vacían transacciones y se cierran sesiones y el pool; no puede quedar otro writer activo.
- Ejecutar `PRAGMA optimize` después de migraciones y antes de cerrar la DB en cada cierre limpio. SQLite recomienda esta operación para conexiones de corta vida al cerrarlas y después de cambios de esquema; normalmente termina rápido.
- Ejecutar `VACUUM` únicamente cuando la lista libre represente al menos 25% del total de páginas y haya espacio libre de al menos dos veces el tamaño actual de la DB más 64 MiB de reserva. Si no cumple, omitir la compactación, registrar la razón y cerrar normalmente. Estos umbrales son valores iniciales aprobados por este ADR y deben cambiarse mediante ADR y medición.
- Para `VACUUM`, mantener el mutex, abrir una conexión exclusiva dedicada, comprobar que no existe transacción y que la base está en buen estado, ejecutar el comando fuera de una transacción explícita y cerrar la conexión. Tras éxito, comprobar `PRAGMA integrity_check`.
- Si falta espacio o SQLite rechaza/aborta el mantenimiento, no borrar ni reemplazar la DB. Registrar un evento sanitizado con `DB_MAINTENANCE_FAILED`, cerrar de manera controlada y permitir que la siguiente ventana elegible lo reintente. Una falla de mantenimiento no se convierte por sí sola en fallo de integridad.

## Alternatives Considered

- `VACUUM` en cada cierre: consume I/O, puede duplicar el espacio temporal requerido y alargar cierres aunque la DB no tenga páginas libres.
- `auto_vacuum=FULL`: aumenta escrituras durante cada operación y puede agravar fragmentación; además no sustituye la compactación de páginas parcialmente ocupadas.
- Proceso programado separado: añade otro ejecutable/lock y una recuperación adicional; el cierre seguro ya ofrece exclusión con el mutex existente.
- Ignorar mantenimiento ante falta de espacio: elimina la trazabilidad y hace que el siguiente cierre repita el trabajo sin explicar por qué.

## Positive Consequences

- Las escrituras se detienen antes de modificar el archivo y el mutex evita una segunda instancia durante VACUUM.
- La compacción evita crecimiento indefinido, mientras que los prechecks protegen el espacio operativo y la DB activa.
- Una condición de disco lleno se registra y no se transforma en borrado de contenido.

## Negative Consequences

- Un cierre con fragmentación alta puede tardar mientras se reconstruye la DB.
- El umbral y la reserva deben revisarse con benchmarks en bases sintéticas representativas.

## Failure Modes

- El mantenimiento se ejecuta dentro de una transacción o con sesiones activas.
- Se libera el mutex antes de cerrar la DB.
- El cálculo de espacio ignora la DB WAL o los archivos temporales.
- Un error se oculta o lleva a marcar una DB no verificada como sana.

## Reversal Strategy

Antes de cambiar el momento de ejecución, umbrales o política de reintento, medir duración/espacio en fixtures de alta volatilidad. Conservar la DB original ante fallo, ejecutar `integrity_check` y actualizar el cierre y pruebas bajo un ADR nuevo.

## References

- [SQLite VACUUM](https://www.sqlite.org/lang_vacuum.html)
- [SQLite PRAGMA optimize](https://www.sqlite.org/pragma.html#pragma_optimize)
