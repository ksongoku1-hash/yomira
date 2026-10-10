# ADR-0068 — Matriz de transiciones y recuperación de jobs

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26. No se atribuye esta decisión al usuario; se adopta conforme a la autoridad arquitectónica delegada y las prioridades aprobadas de integridad y recuperación. Puede revisarse mediante ADR posterior.

## Context

La especificación define los estados, la ruta normal, tres ejemplos de transiciones y los estados terminales, pero no una matriz exhaustiva. También exige que `INTERRUPTED` sea recuperable sin definir a qué etapa vuelve un job. El Job Manager necesita una regla cerrada antes de implementar K082–K083.

## Requisitos ya fijados por la especificación

- La ruta de éxito es `QUEUED → FETCHING_METADATA → DOWNLOADING → VALIDATING → PROCESSING → PACKAGING → VERIFYING → INDEXING → COMPLETED`.
- `DOWNLOADING → COMPLETED` es ilegal.
- `COMPLETED`, `FAILED` y `CANCELLED` son terminales.
- Una transición ilegal produce excepción.
- `INTERRUPTED` debe ser recuperable.
- La cancelación inmediata aplica a `QUEUED` y `FETCHING_METADATA`; en las demás etapas respeta puntos seguros, cleanup de escritura crítica y límites de transacción.

## Decisión

La matriz adoptada para K082–K083 define las únicas transiciones genéricas permitidas; una transición ausente, una repetición del mismo estado o un salto entre etapas debe lanzar `IllegalJobTransition` sin mutar el job.

| Estado actual | Avance de éxito | Fallo no recuperable | Cancelación solicitada | Interrupción detectada |
|---|---|---|---|---|
| `QUEUED` | `FETCHING_METADATA` | `FAILED` | `CANCELLED` inmediata | Permanece `QUEUED` |
| `FETCHING_METADATA` | `DOWNLOADING` | `FAILED` | `CANCELLED` inmediata | `INTERRUPTED` |
| `DOWNLOADING` | `VALIDATING` | `FAILED` | `CANCELLED` tras detener solicitudes y cerrar el stream | `INTERRUPTED` |
| `VALIDATING` | `PROCESSING` | `FAILED` | `CANCELLED` en punto seguro | `INTERRUPTED` |
| `PROCESSING` | `PACKAGING` | `FAILED` | `CANCELLED` entre páginas | `INTERRUPTED` |
| `PACKAGING` | `VERIFYING` | `FAILED` | `CANCELLED` después de terminar la escritura crítica o completar cleanup | `INTERRUPTED` |
| `VERIFYING` | `INDEXING` | `FAILED` | `CANCELLED` en punto seguro | `INTERRUPTED` |
| `INDEXING` | `COMPLETED` | `FAILED` | `CANCELLED` solo fuera de una transacción DB | `INTERRUPTED` |
| `INTERRUPTED` | Según la opción elegida abajo | `FAILED` si DB, manifest o archivos se contradicen | No aplica | No aplica |
| `COMPLETED` | Ninguna | Ninguna | Ninguna | Ninguna |
| `FAILED` | Ninguna | Ninguna | Ninguna | Ninguna |
| `CANCELLED` | Ninguna | Ninguna | Ninguna | Ninguna |

Una petición de cancelación diferida deja el job en su estado actual hasta llegar al punto seguro indicado; la fila persistida nunca afirma `CANCELLED` mientras una escritura o transacción crítica siga activa. Cualquier estado distinto de `INTERRUPTED` que reaparezca activo tras cierre/crash se convierte en `INTERRUPTED` durante recovery, salvo `QUEUED`, que puede permanecer en cola.

El guard genérico solo permite `INTERRUPTED → FAILED` para fallos de reconciliación. No permite reanudar directamente: el Recovery Manager deberá verificar el checkpoint durable y usar una operación de recuperación específica, que se implementará en K091/K094.

### Recuperación desde `INTERRUPTED`

- **Decisión adoptada — Opción A: reanudar desde el último punto seguro verificado.** Persistir la etapa de retorno y el checkpoint durable; al iniciar, verificar manifest, DB y archivos implicados. Reanudar la etapa más avanzada cuyos prerrequisitos durables coincidan; si no hay checkpoint válido, volver a `QUEUED`. Si DB, manifest y archivos se contradicen, pasar a `FAILED` con diagnóstico trazable y no sobrescribir contenido dudoso.
- **Alternativa descartada — Opción B: volver a `QUEUED` y reconciliar desde el inicio.** Conservaría el mismo `job_id`, validaría manifest y temporales y repetiría las etapas. Solo reutilizaría páginas después de volver a verificar contenido e identidad. Es recuperable, pero repite más trabajo y no aprovecha el último checkpoint verificado.

En ambas opciones, `INTERRUPTED` solo sale mediante una acción del Recovery Manager; no se permiten saltos arbitrarios entre etapas. Pausa temporal no cambia el estado persistido y queda para K089. Reintentar un job `FAILED` o `CANCELLED` crea un `job_id` nuevo y conserva el registro anterior.

## Consecuencias previstas

- K082 implementará el grafo aprobado como regla pura de dominio.
- K083 probará cada transición válida e inválida y verificará que fallar una transición no muta el job.
- K091/K094 implementarán el estado de interrupción y la evidencia de recuperación conforme a la opción aprobada.
- Los cambios de semántica posteriores requerirán una nueva ADR y pruebas de regresión.

## Alternativas consideradas

- Permitir transiciones entre cualquier estado: hace posible ocultar páginas sin validar o declarar éxito prematuramente.
- Inferir éxito por la existencia del archivo final: no demuestra conteo, integridad ni sincronización de metadata y contradice ADR-0007.
- Retornar siempre a una etapa activa sin checkpoint: puede repetir o saltar trabajo sin demostrar qué artefactos son válidos.
