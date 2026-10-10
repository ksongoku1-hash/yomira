# Especificación de logging

**Estado:** APPROVED — K004 aprobado formalmente por el usuario el 2026-09-25.

## 1. Propósito

Los logs permiten reconstruir fallos y su recuperación. Son datos diagnósticos: no reemplazan el estado persistente de jobs, manifests, DB ni la verificación de archivos.

## 2. Registro estructurado

Cada evento incluye los campos aplicables:

| Campo | Uso |
|---|---|
| `timestamp` | Hora UTC del evento |
| `level` | Severidad técnica del evento |
| `component` | Módulo que detectó/emite el evento |
| `trace_id` | Correlación de una solicitud u operación entre componentes |
| `job_id` | Trabajo relacionado, si existe |
| `series_id` | Obra local, si existe |
| `chapter_id` | Capítulo lógico, si existe |
| `source_id` | Fuente remota, si aplica |
| `stage` | Fase del pipeline o diagnóstico |
| `error_code` | Código estable del catálogo; vacío en eventos no error |
| `retryable` | Indica si la repetición puede ser segura según política |
| `attempt` | Número de intento, si aplica |
| `duration` | Duración con reloj monotónico cuando corresponda |
| `resource_metric` | Métrica acotada del monitor de recursos, si aplica |
| `observed_value` | Valor numérico observado o identificador estable de fallo del sensor |
| `threshold_value` | Umbral de entrada/salida aplicado, si aplica |
| `reason` | Causa estable de pausa o recuperación |
| `action` | Acción del Job Manager, como pause request, resume request o bloqueo de admisión |
| `admission_allowed` | Si el Job Manager admite trabajo nuevo tras la decisión |
| `message` | Texto humano breve, no usado para lógica |

El formato estructurado exacto se fijará al iniciar tooling; el contrato de campos y la sanitización son normativos.

La implementación inicial escribe una línea JSON por evento en stderr, sin persistencia de logs. timestamp usa UTC ISO-8601; duration es un número de segundos medido con reloj monotónico. component procede del nombre de logger y trace_id usa el contexto de request. Los eventos de request añaden method y status_code. Un fallo inesperado incluye exception_type y hasta 12 marcos con nombre de archivo, función y línea; excluye el mensaje de excepción y rutas absolutas. La retención/rotación a archivo se implementará más adelante.

## 3. Correlación y propagación

- La API genera o acepta un `trace_id` válido para la solicitud local y lo propaga por servicios y adaptadores.
- El retry conserva la correlación del job y registra el intento; cada petición de red puede tener su propio evento correlacionado.
- Al reanudar tras crash se conserva `job_id`; no se finge continuidad de una conexión perdida.
- Errores API incluyen `code`, `message`, `trace_id` y `retryable`.
- El estado PENDING/FAILED/INTERRUPTED/COMPLETED de UI se basa en estado API persistido, no en inferir desde mensajes de log.

## 4. Privacidad y contenido

Nunca registrar passwords, PIN, tokens, cookies, secretos, referencias opacas del Vault o contenido de páginas/CBZ. URLs se sanitizan para remover query params sensibles; paths con username se reducen según las reglas del Support Bundle. Evitar incluir HTML remoto, cuerpos binarios, metadata privada o datos personales en `message`.

## 5. Retención y almacenamiento

Valores iniciales especificados: máximo sugerido de 10 MB por archivo y 5 archivos rotados; la política será configurable. La rotación no debe bloquear una escritura crítica ni consumir espacio reservado para DB/WAL. Fallo al escribir log se registra por canal disponible y nunca convierte una operación inválida en éxito.

## 6. Recorrido documental de timeout

| Paso | Evidencia exigida |
|---|---|
| Request vence | `NET_*` o `DL_TIMEOUT`, etapa y timeout connect/read/write/pool |
| Se decide retry | `retryable=true`, número de intento y espera aplicada |
| Agota reintentos | código final, `job_id`, `trace_id`, etapa y `retryable` final |
| API/UI muestran fallo | misma clase/código/trace; texto legible; estado no es `COMPLETED` |
| Support Bundle incluye | evento sanitizado y versiones, sin URL sensible ni contenido de página |

## 7. Verificación documental

- Los registros tienen contexto suficiente para distinguir timeout, rechazo HTTP, parser roto, disco lleno y corrupción.
- Mensajes humanos no son API de control.
- Retry y fallo final quedan trazables sin registrar bytes/secretos.
- El límite de rotación impide crecimiento ilimitado.

