# ADR-0112: Retiro reversible de trabajos de la cola

## Estado

Aceptada para esta implementación, solicitada para resolver la acumulación de trabajos visibles.

## Contexto

La vista Descargas permite cancelar trabajos en curso y reintentar fallos, pero no retirar trabajos terminales de la lista. Las filas `download_jobs` pueden tener filas `download_pages` que las referencian con `ON DELETE RESTRICT`; además, pueden conservar temporales, hashes, manifests y referencias necesarias para recuperación y diagnóstico. Borrar esas filas en cascada rompería la trazabilidad o podría eliminar evidencia y contenido recuperable.

## Decisión

1. «Quitar de la cola» archiva el trabajo en una tabla auxiliar `download_job_archives`; no borra el trabajo, sus páginas, manifest, temporales, CBZ ni archivos de biblioteca.
2. Solo se pueden archivar trabajos terminales `COMPLETED`, `FAILED` y `CANCELLED` que no tengan una cancelación pendiente. `INTERRUPTED` sigue visible porque puede requerir recuperación; los trabajos activos siguen usando cancelación segura.
3. La cola normal excluye trabajos archivados. La vista «Trabajos retirados» los lista con la misma paginación acotada y permite restaurarlos. Las operaciones son idempotentes cuando el trabajo ya está en el estado solicitado.
4. La referencia de archivo tiene una clave foránea `RESTRICT` al trabajo y una fecha durable. No se incorpora al manifest de recuperación, que continúa representando el estado de ejecución y sus artefactos.
5. El endpoint de archivo rechaza estados no terminales con HTTP 409, y ambos endpoints devuelven 404 para un trabajo inexistente. El listado informa el total del filtro solicitado.

## Consecuencias

- Se reduce la cola visible sin perder datos ni evidencia histórica y cada trabajo puede restaurarse.
- El archivo lógico no reduce por sí solo el tamaño de la base de datos ni elimina temporales. La eliminación física requiere una política futura que verifique retención, referencias, contenido y recuperación antes de retirar evidencia.
- No se archivan automáticamente trabajos ni se cambia el comportamiento del dispatcher.

## Verificación requerida

- Un fallo con una fila `download_pages` puede archivarse y restaurarse; la fila del trabajo y la página permanecen idénticas.
- Un trabajo activo o interrumpido no se puede archivar y sigue visible.
- Listar la cola y los archivados conserva límites y paginación; los totales reflejan cada vista.
- La operación no toca filesystem, manifests, páginas ni archivos CBZ.
