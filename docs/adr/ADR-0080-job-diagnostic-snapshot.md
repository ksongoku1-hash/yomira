# ADR-0080 — Diagnóstico persistente de DownloadJob

## Status

Accepted — decisión de Arquitectura Principal del 2026-09-27, después de revisión independiente. Prerrequisitos cerrados: SPEC 1.1 APPROVED (`docs/YOMIRA_V1_SPEC.md`, 2026-09-27) y ADR-0079 Accepted (2026-09-27).

## Context

- La SPEC §3.5 exige contexto estructurado de errores; §50 expone `GET /api/v1/downloads/{job_id}`; `UI_LAYOUTS.md` requiere mostrar etapa, estado y código de error.
- `LOGGING_SPEC.md` define correlación y sanitización, pero la implementación actual escribe a `stderr` y no persiste logs. Los logs por sí solos no permiten consultar después de un reinicio el diagnóstico vigente de un job.
- ADR-0034 limita el registro base de job a identidad, estado, prioridad y timestamps. ADR-0073 exige que los diagnósticos de cancelación sean sanitizados. ADR-0079 define la clasificación de recovery y el resultado durable; este ADR no la modifica.
- K095 debe permitir entender por qué un job está fallido, interrumpido o bloqueado, sin crear un historial ilimitado.

## Decisión

### Snapshot durable

- K095 mantiene como máximo una fila de diagnóstico por `job_id`, separada del estado del job. La migración siguiente a `0017_job_recovery_checkpoint` crea `download_job_diagnostics`, con `job_id` como PK y FK a `download_jobs`. No crea una tabla de eventos ni sintetiza diagnósticos para jobs existentes.
- El snapshot contiene: `error_code` (código estable de `ErrorCode`), `diagnostic_stage`, `job_status_at_event`, `reason_code`, `action_code`, `retryable`, `first_trace_id`, `last_trace_id`, `first_seen_at_us`, `last_seen_at_us`, `occurrence_count` y `resolved_at_us` nullable.
- `diagnostic_stage` es un enum ASCII cerrado del paso técnico que detectó o clasificó el problema; no es un estado de job ni se deriva de él. El vocabulario inicial es `job_admission`, `metadata_fetch`, `page_plan`, `page_download`, `page_validation`, `page_processing`, `cbz_packaging`, `artifact_verification`, `library_indexing`, `job_cancellation`, `manifest_reconciliation`, `page_reconciliation`, `publication_reconciliation`, `job_reconciliation` y `diagnostic_persistence`. Por ejemplo, `manifest_reconciliation` puede coexistir con `job_status_at_event=INTERRUPTED`. Agregar valores requiere actualizar este ADR y sus pruebas.
- `job_status_at_event` es obligatorio y conserva el estado `DownloadJobStatus` observado en la primera ocurrencia del snapshot; no se cambia durante agregaciones posteriores. Su vocabulario es exactamente el conjunto de estados de ADR-0034.
- `reason_code` es un identificador ASCII estable, no texto humano. El vocabulario inicial es `ambiguous_evidence`, `durable_checkpoint_missing`, `durable_checkpoint_mismatch`, `resource_budget_unsafe`, `storage_io_uncertain`, `stage_input_invalid` y `stage_cancel_pending`.
- `action_code` describe el resultado durable aplicado, no una recomendación para la UI. Su vocabulario inicial es `block_job`, `fail_job`, `return_to_queue`, `resume_job`, `cancel_job` y `preserve_terminal`. La combinación con `error_code`, `reason_code` y `job_status_at_event` debe respetar la matriz de abajo; combinaciones inválidas se rechazan sin mutar job ni diagnóstico.
- Los UUID de traza se validan como UUIDv4 canónicos en minúsculas. `occurrence_count` es entero positivo dentro del rango SQLite. `first_seen_at_us`, `last_seen_at_us` y `resolved_at_us` son enteros UTC en microsegundos; `first_seen_at_us <= last_seen_at_us`. Si no hay snapshot, la API devuelve `diagnostic: null`.

### Matriz de action_code

La acción válida se determina por esta matriz; `error_code` debe pertenecer al catálogo y nunca sustituye a la clasificación estable `reason_code`.

| Condición demostrada | Job no terminal | Job terminal |
|---|---|---|
| Evidencia ambigua, checkpoint ausente, presupuesto inseguro, I/O incierto o cancelación pendiente sin punto seguro | `block_job`; snapshot activo y sin resolución | `preserve_terminal`; estado intacto y snapshot activo (`resolved_at_us=NULL`) |
| Contradicción durable probada o entrada de etapa inválida probada | `fail_job` solo al persistir `FAILED` por CAS con bytes preservados | `preserve_terminal`; estado intacto y snapshot activo (`resolved_at_us=NULL`) hasta remediación durable |
| Recovery prueba ausencia de contradicción y de cancelación pendiente, sin checkpoint reanudable | `return_to_queue` mediante CAS | No aplica |
| Recovery verifica checkpoint durable reanudable y no hay cancelación pendiente | `resume_job` mediante operación específica de RecoveryCoordinator | No aplica |
| Intención durable y evidencia segura completa según ADR-0073 | `cancel_job` al persistir `CANCELLED` por CAS | No aplica |

Una fila no puede usar `return_to_queue` o `resume_job` ante checkpoint faltante, contradicción o cancelación pendiente. Un estado terminal nunca recibe una acción que lo reabra.

- `block_job` conserva el estado que determine ADR-0079 y persiste `recovery_blocked=1` cuando aplique. No resuelve por sí solo el diagnóstico.
- `fail_job` no se resuelve al detectar o registrar el conflicto. Solo el CAS durable a `FAILED`, con candidatos preservados, permite fijar `resolved_at_us`.
- `return_to_queue`, `resume_job` y `cancel_job` requieren, respectivamente, la ausencia probada de contradicción/cancelación, un checkpoint durable verificado, o evidencia segura según ADR-0073. Cada acción solo resuelve el diagnóstico en la misma transacción/CAS que persiste el resultado.
- `preserve_terminal` conserva `COMPLETED`, `FAILED` o `CANCELLED` sin crear una arista ni reabrir el job; la acción por sí sola nunca resuelve el diagnóstico. Si una remediación aprobada verifica después el contenido y la causa, puede resolver la snapshot mediante su propia transacción durable sin cambiar el estado terminal.

### Ciclo de vida

- La fila está activa si `resolved_at_us IS NULL` y resuelta si `resolved_at_us IS NOT NULL`. La primera ocurrencia crea el snapshot con `first_*=last_*`, `occurrence_count=1` y `resolved_at_us=NULL`.
- Una nueva ocurrencia de la misma clasificación activa (`error_code`, `diagnostic_stage`, `reason_code`, `action_code` y `retryable` idénticos) incrementa `occurrence_count` y actualiza solo `last_trace_id` y `last_seen_at_us`. Conserva los campos iniciales, incluido `job_status_at_event`.
- Una clasificación distinta no sobrescribe la fila activa. El caller debe resolver primero la condición vigente con evidencia y una transición durable bajo CAS; si no puede, conserva la fila, bloquea nuevas mutaciones del job y falla cerrado. La garantía durable de K095 cubre la causa primaria activa; el evento distinto se emite a los logs estructurados sanitizados y puede dejar de estar disponible cuando esos logs cumplan su retención/rotación. Este ADR no afirma conservar un historial durable de todos los eventos.
- La resolución cambia `resolved_at_us` una sola vez, de NULL a timestamp, en la misma transacción/CAS que acredita la resolución durable; el instante debe ser igual o posterior a `last_seen_at_us`. La repetición idempotente no cambia ese valor. Una clasificación nueva, incluida una clasificación anterior ya resuelta, puede reemplazar atómicamente solo una fila resuelta e iniciar un snapshot nuevo con contador 1 y `resolved_at_us=NULL`. K095 no conserva historial de snapshots reemplazados.
- Registrar, bloquear o clasificar un fallo no constituye por sí mismo evidencia de resolución. Una mutación del job y el snapshot que explica su resultado se confirman o revierten juntos. Si falla el commit —incluido `SQLITE_FULL`— no se afirma que el estado o diagnóstico se hayan persistido; se mantiene cerrado el gate de escritura/recovery de ADR-0079.
- Consultar o registrar el diagnóstico nunca cambia por sí solo el estado del job, política de retry, clasificación `BLOCKED`/`CONFLICT` ni readiness.

### API y privacidad

- `GET /api/v1/downloads/{job_id}` incluye `diagnostic`. En la API, `origin_trace_id` reemplaza el nombre persistido `first_trace_id` y `last_trace_id` identifica la última ocurrencia agregada; no se duplican ambos nombres para el mismo valor. La API expone un solo campo de resolución, `resolved_at`, RFC 3339 UTC terminado en `Z`, o NULL; convierte `resolved_at_us` sin perder precisión. Estas trazas son distintas del `trace_id` de la solicitud actual y de `X-Trace-ID`.
- La colección `GET /api/v1/downloads` expone solo los campos compactos requeridos por la UI (`error_code` y `diagnostic_stage`); no introduce un endpoint de historial ni vuelca logs en la respuesta. Límites de página y orden siguen el contrato de API que se apruebe para esa tarea, no el endpoint de biblioteca por inferencia.
- El texto visible se genera desde el catálogo de errores y la localización. No se persiste ni retorna texto libre, stack trace, URL/referencia remota, query token, cookie, secreto, referencia del Vault, contenido de página/CBZ ni ruta absoluta.
- La sanitización de logs y Support Bundle continúa bajo `LOGGING_SPEC.md` y `SUPPORT_BUNDLE_SPEC.md`; este ADR no copia la DB ni expande el bundle.

### Cota de almacenamiento y retención

- Una fila por job limita el incremento diagnóstico a una fila por cada job retenido; no limita el tamaño total de SQLite, que crece con el número de jobs conservados. Este ADR no fija retención global, no autoriza eliminar jobs ni diagnósticos y no afirma que el almacenamiento total esté acotado.
- K095 no poda filas. Antes de cerrar K284, debe existir una política de retención de jobs aprobada que defina límites, borrado/archivo, preservación de páginas y artefactos, efecto en consultas y eliminación atómica de diagnósticos junto al job. K284 falla mientras esa política y su verificación de almacenamiento no existan.

## Verificación requerida para K095

- La migración preserva los datos existentes y deja los jobs legacy sin snapshot.
- La validación prueba por separado `diagnostic_stage` y `job_status_at_event`, incluyendo `INTERRUPTED/manifest_reconciliation` y `QUEUED/job_admission`; valores desconocidos no se aceptan.
- Inserción inicial, agregación repetida, clasificación distinta durante un snapshot activo, resolución, reemplazo tras resolución y CAS obsoleto tienen resultados deterministas. Ninguna clasificación distinta sobrescribe silenciosamente una causa activa.
- `resolved_at_us` solo cambia de NULL a UTC una vez, nunca retrocede y no cambia por repetición idempotente. Una nueva snapshot puede comenzar solo después de resolver la anterior.
- Pruebas de matriz cubren cada `action_code`, incluyendo que un job terminal solo acepta `preserve_terminal`; combinaciones inválidas no cambian job ni snapshot.
- Mutación de estado y snapshot son atómicos ante cierre inesperado y `SQLITE_FULL`; no queda transición sin diagnóstico ni diagnóstico que afirme una transición revertida.
- El snapshot sobrevive a reinicio; la consulta individual devuelve campos acotados. `origin_trace_id`/`last_trace_id` no sustituyen el trace id de la solicitud actual.
- Casos con cookies, tokens en query, URL remota, referencia Vault, ruta con nombre de usuario, contenido binario, texto remoto y stack trace no filtran esos datos a API, logs ni bundle.
- La colección no crea historial ni enumera jobs adicionales; se prueba conforme a su contrato de límites/paginación aprobado. K095 no ejecuta pruning y no presenta el total de SQLite como globalmente acotado.
- `SQLITE_FULL` durante una actualización conserva fila, diagnóstico y archivos anteriores; activa el gate global de ADR-0079.

## Alternativas consideradas

- **Usar solo logs:** descartado porque el log vigente no persiste y puede rotar; el usuario perdería el diagnóstico consultable del job tras reiniciar.
- **Guardar un evento por cada reintento sin límite:** descartado por crecimiento no acotado; K095 conserva solo el diagnóstico abierto por job y reemplaza el snapshot únicamente después de una resolución durable.
- **Persistir mensajes humanos o excepciones:** descartado por localización, inestabilidad y riesgo de filtrar rutas, datos remotos o secretos.
- **Crear un endpoint separado de historial:** descartado en K095 porque duplicaría la consulta por job y requeriría otra política de paginación/retención.

## Consecuencias

- Los jobs fallidos o bloqueados mantienen un diagnóstico consultable después de reiniciar, con una fila de diagnóstico como máximo por job retenido.
- El trace id del incidente permanece separado del trace id de cada request de consulta.
- El snapshot no reemplaza logs ni constituye historial de auditoría. Su ciclo de vida y la ausencia actual de una cota global de jobs son explícitos; K284 no puede pasar hasta cerrar la retención.
- La retención futura debe conservar la relación job/páginas/artefactos según sus propias ADRs; este documento no autoriza borrados.

## Reversión

Cambiar la forma de persistencia, la regla de una fila por job, los campos, la privacidad o el ciclo de vida requiere una ADR nueva y una migración aditiva. No se borra un diagnóstico durante recovery por inferencia ni se retrorellenan jobs históricos desde logs incompletos.

