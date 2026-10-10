# ADR-0034 — Identidad y persistencia de DownloadJob

## Status

Accepted — decisión de implementación de K043, conforme a ADR-0007 y la sección 17–20 de la especificación.

## Context

La especificación requiere jobs persistentes, define sus estados y prioridades y ejemplifica un manifest con `job_id`, `source_id` y `chapter_id`, pero no enumera las columnas de `DownloadJob`. `ChapterSource` identifica una versión concreta del capítulo en una fuente; relacionar un job solo con capítulo y fuente no identifica cuál versión remota originó la descarga.

## Decision

- `DownloadJob` tendrá `job_id` UUIDv4, `chapter_source_id`, `status`, `priority`, `created_at` y `updated_at`.
- `chapter_source_id` será obligatorio y tendrá FK `ON DELETE RESTRICT` a `chapter_sources.chapter_source_id`. El vínculo conserva la selección exacta de versión remota; `chapter_id` y `source_id` del manifest se derivan de esa asociación.
- Los estados permitidos serán exactamente `QUEUED`, `FETCHING_METADATA`, `DOWNLOADING`, `VALIDATING`, `PROCESSING`, `PACKAGING`, `VERIFYING`, `INDEXING`, `COMPLETED`, `FAILED`, `CANCELLED` e `INTERRUPTED`, según la especificación. La matriz y las excepciones ante transiciones ilegales permanecen bajo la máquina de estados de ADR-0007; el `CHECK` SQL impide valores fuera del vocabulario aprobado.
- Las prioridades permitidas serán exactamente `HIGH`, `NORMAL` y `LOW`. Un job nuevo se crea en `QUEUED` con prioridad `NORMAL`, salvo prioridad explícita validada por quien lo solicite.
- `created_at` y `updated_at` son fechas conscientes de zona horaria, almacenadas como Unix UTC en microsegundos. Al crear, ambas tienen el mismo instante; cada cambio persistido del registro actualiza `updated_at`.
- Dominio y esquema aplican UUIDv4, timestamps enteros y FKs `RESTRICT` conforme a ADR-0026. Se indexan `chapter_source_id` y `status` para resolver historial por versión y filtrar jobs por estado.
- Esta entidad registra el trabajo persistente; el manifest, las páginas, la coordinación del Job Manager y las transiciones operativas se implementan en sus tareas asignadas.

## Alternatives Considered

- Guardar solo `chapter_id` y `source_id`: no distingue versiones de `ChapterSource` y permite cambiar la versión remota asociada al mismo job.
- Guardar `chapter_id`, `source_id` y `chapter_source_id` redundantes: permite combinaciones contradictorias sin una clave compuesta adicional.
- Permitir que el estado acepte texto libre: facilita estados no recuperables y discrepancias con el contrato de API.
- Usar FK `CASCADE`: borrar una versión remota podría eliminar historial de jobs.

## Positive Consequences

- Cada job conserva una referencia verificable a la versión remota que seleccionó.
- SQLite rechaza referencias inexistentes, borrado accidental de contenido relacionado, estados o prioridades ajenos al contrato y timestamps no enteros.
- La fila mínima es compatible con las responsabilidades posteriores de manifest, páginas y Job Manager.

## Negative Consequences

- Una `ChapterSource` referenciada no puede borrarse hasta que exista un flujo explícito que resuelva sus jobs relacionados.
- Recuperar `source_id` y `chapter_id` requiere unir `download_jobs` con `chapter_sources`.

## Failure Modes

- Una actualización de esquema modifica o elimina la asociación exacta de un job.
- Un mapper interpreta un estado desconocido como un estado válido.
- Una referencia a `ChapterSource` desaparece por una FK permisiva.

## Reversal Strategy

Cambiar la identidad de origen, estados, prioridades o timestamps requiere una nueva ADR y una migración Alembic con backup, validación de cada referencia existente, `integrity_check` y pruebas de preservación/restauración. No se reescriben manifests ni se reasignan jobs automáticamente.
