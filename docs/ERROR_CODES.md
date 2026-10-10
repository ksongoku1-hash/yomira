# Catálogo de errores

**Estado:** APPROVED — K004 aprobado formalmente por el usuario el 2026-09-25.  
Los códigos son identificadores estables para máquinas y diagnóstico. El texto visible puede traducirse; la lógica no debe comparar mensajes humanos.

## 1. Contrato

Los errores de validación pueden añadir hasta 20 detalles con nombres de campo y tipos de problema sanitizados; nunca incluyen valores enviados por el cliente.

Una respuesta de error API expone `code`, `message`, `trace_id` y `retryable`. Los logs incluyen además, cuando aplique, `component`, `job_id`, `series_id`, `chapter_id`, `source_id`, `stage`, `attempt` y `duration`.

`retryable = true` significa que repetir puede ser seguro después de aplicar la política correspondiente; no significa reintento infinito ni siempre automático. Un retry debe conservar trazabilidad, incrementar intento y limpiar/reconciliar su temporal antes de escribir de nuevo.

## 2. Severidad

| Nivel | Significado |
|---|---|
| P0 | Pérdida o destrucción de datos |
| P1 | Corrupción o riesgo de integridad |
| P2 | Funcionalidad mayor rota |
| P3 | Problema menor |
| P4 | Problema visual o cosmético |

Un P0/P1 detiene el flujo afectado y exige diagnóstico; los códigos siguientes no afirman que esos incidentes estén actualmente presentes.

## 3. Códigos iniciales propuestos

| Código | Componente/etapa | Severidad | Reintentable | Significado y acción esperada |
|---|---|---:|---|---|
| `DB_BUSY_TIMEOUT` | DB/transaction | P2 | Sí, tras espera acotada | SQLite o el coordinador de escritura permaneció bloqueado hasta el timeout. No repetir una transacción de resultado incierto; reconciliarla y reintentar una operación segura. |
| `DB_CONNECTION_FAILED` | DB/connection | P2 | Sí, tras corregir la causa | La DB no abrió o no aceptó la configuración SQLite requerida. No iniciar operaciones; conservar el archivo y revisar permisos, lock e integridad. |
| `API_REQUEST_REJECTED` | API/request | P3 | No | La API rechazó una petición HTTP válida como transporte pero no aceptable para el endpoint. Corregir ruta, método o autorización; el detalle no refleja valores privados. |
| `API_VALIDATION_FAILED` | API/request | P3 | No | Request no satisface el schema publicado. Devolver detalles estructurados y sanitizados; corregir el input antes de repetir. |
| `SYS_INTERNAL_ERROR` | Sistema/unexpected | P1 | No | Fallo inesperado; detener la operación afectada y conservar evidencia. No revelar stack trace al cliente. |
| `SYS_STARTUP_FAILED` | Startup | P1 | No | La aplicación no pudo inicializarse. Registrar componente y abrir diagnóstico/Safe Mode según corresponda. |
| `SYS_INVALID_CONFIG` | Configuración | P2 | No | Configuración inválida; explicar el campo y evitar comenzar operaciones con valores por defecto silenciosos. |
| `SYS_RESOURCE_MONITOR_UNAVAILABLE` | Resource monitor | P2 | Sí | No se puede verificar RAM/I/O del sistema. Bloquear nuevas admisiones y pausar jobs en safe points hasta obtener muestras válidas. |
| `SYS_STARTUP_CHECK_FAILED` | Startup | P1 | No | Self-test crítico falló. Registrar subsistema y abrir Safe Mode. |
| `SYS_DISK_BUDGET_EXCEEDED` | Disk budget/filesystem | P2 | No | El preflight encontró espacio insuficiente o el SO rechazó una escritura por falta de espacio. En preflight, no iniciar el lote; durante una escritura, detener la operación, conservar artefactos y no reintentar automáticamente. Liberar espacio y verificar antes de repetir. |
| `DB_MIGRATION_FAILED` | DB/migration | P1 | No | Migración falló. Detener arranque normal, preservar evidencia, intentar restore según política y abrir Safe Mode. |
| `DB_INTEGRITY_FAILED` | DB/diagnostics | P1 | No | SQLite integrity check falló. Bloquear nuevas escrituras y ofrecer recuperación/backup. |
| `DB_MAINTENANCE_FAILED` | DB/shutdown | P2 | Sí, en la siguiente ventana elegible | `PRAGMA optimize`, preflight o `VACUUM` falló. Conservar la DB, cerrar el proceso de forma controlada y registrar etapa/tipo sin incluir rutas ni mensajes internos. |
| `DB_BACKUP_FAILED` | DB/backup | P1 | No | Backup no se completó o no se pudo verificar. No continuar una migración que requiere ese backup. |
| `SRC_SCHEMA_CHANGED` | Connector/parse | P2 | No | Respuesta remota ya no satisface contrato/fixture del connector. Marcar health como degradado o incompatible; no inventar resultados. |
| `SRC_CIRCUIT_OPEN` | Connector/health | P3 | Sí, tras cooldown | Circuit breaker está OPEN. No hacer solicitudes normales hasta prueba limitada HALF_OPEN. |
| `SRC_CAPABILITY_UNSUPPORTED` | Connector/dispatch | P3 | No | El connector no declara la operación solicitada. Rechazar antes de I/O; no usar otro método como fallback. |
| `SRC_CONNECTOR_FAILED` | Connector/operation | P2 | Sí/No, clasificación explícita | Fallo de operación no cubierto por un código más específico. El owner clasifica retryability según una causa transitoria segura; cualquier retry es finito y conserva intento, operación y traza. El mensaje no contiene texto remoto ni secreto. |
| `DL_HTTP_429` | Download/HTTP | P3 | Sí | Rate limit remoto. Respetar `Retry-After` si existe y aplicar espera acotada. |
| `DL_TIMEOUT` | Download/HTTP | P3 | Sí | Timeout de conexión/lectura/escritura/pool. Registrar cuál; usar retries 1s, 3s, 8s según política. |
| `DL_STREAM_INTERRUPTED` | Download/stream | P2 | Sí | El stream terminó antes de validar. Conservar/reconciliar `.part`, no promover a página válida; reintentar según límite. |
| `DL_HASH_MISMATCH` | Download/verify | P1 | Sí, acotado | Hash no coincide con el manifest/resultado esperado. Aislar el temporal y repetir de forma limitada; nunca indexar contenido dudoso. |
| `DL_JOB_RECOVERY_BLOCKED` | Download/recovery | P2 | Sí, tras corregir la causa | Recovery no puede determinar de forma segura un estado autoritativo o completar la clasificación. Aislar solo ese job, conservar todos sus artefactos y no iniciar retry infinito; volver a evaluarlo en una invocación posterior de recovery. No contar la cuarentena como recuperación completada. |
| `DL_JOB_RECOVERY_CONFLICT` | Download/recovery | P1 | No | Recovery probó una contradicción entre DB, marcador, manifest o bytes. Registrar diagnóstico sanitizado y conservar todos los candidatos. Cambiar por CAS a `FAILED` solo si el job no es terminal; preservar el estado terminal sin aristas nuevas. |
| `DL_MANIFEST_PERSIST_FAILED` | Download/manifest | P1 | No | El staging, la verificación o la publicación del manifest falló. No informar éxito; conservar los candidatos que sigan existiendo para recovery. |
| `DL_MANIFEST_STALE_SNAPSHOT` | Download/manifest | P1 | No | El manifest desapareció o su digest no coincide con el snapshot esperado. No reemplazarlo; recargar el estado y reconciliar antes de otra publicación. |
| `DL_TEMPORARY_BUDGET_EXCEEDED` | Download/temp budget | P2 | No | Las páginas validadas y temporales existentes del job más la próxima escritura superarían los 2 GiB agregados. No escribir el chunk que excede; preservar cada artefacto. |
| `DL_TEMPORARY_PATH_CONFLICT` | Download/path reservation | P2 | No | La clave global de `temporary_path` ya está reservada o el destino exclusivo existe. No reemplazar archivos; resolver la reserva/conflicto antes de continuar. |
| `DL_TEMPORARY_PATH_UNAVAILABLE` | Download/path resolution | P2 | No | La raíz o el destino no está disponible, la ruta escapa de su raíz, contiene un reparse point, excede el límite admitido o el runtime no tiene los datos Unicode requeridos. Corregir la ruta/raíz o el runtime y repetir el preflight. |
| `IMG_DECODE_FAILED` | Image/validate | P1 | No | Bytes no decodifican como imagen permitida. Marcar página inválida y conservar diagnóstico sin publicarla. |
| `IMG_PIXEL_LIMIT` | Image/validate | P2 | No | Dimensiones/píxeles exceden límites. No decodificar por completo; informar límite aplicado. |
| `IMG_THUMBNAIL_FAILED` | Image/thumbnail | P3 | No | La imagen no cumple el contrato del ThumbnailService o falla la generación. Conservar el Asset original y no servir el derivado incompleto. |
| `CBZ_VERIFY_FAILED` | CBZ/verify | P1 | No | ZIP, metadata, conteo, decodificación o hashes no pasan validación. No renombrar ni indexar como CBZ final. |
| `LIB_PATH_UNAVAILABLE` | Library/filesystem | P2 | No | Raíz/archivo inaccesible por ruta, dispositivo o permiso. Preservar referencias y permitir diagnóstico/cambio de raíz. |
| `EXP_DISK_FULL` | Export/copy | P2 | Sí, después de liberar espacio | Destino sin espacio. Mantener `.part` como no válido y permitir retry solo tras nuevo preflight. |
| `EXP_DEVICE_DISCONNECTED` | Export/copy | P2 | Sí, tras reconectar | Destino removible desapareció. Marcar `FAILED` o `INTERRUPTED`; verificar de nuevo antes de continuar. |
| `EXP_DESTINATION_CONFLICT` | Export/publish | P2 | No | Ya existe un destino con bytes diferentes. No reemplazarlo; elegir otro nombre o inspeccionar el contenido existente. |
| `EXP_HASH_MISMATCH` | Export/verify | P1 | No | Los bytes del origen o staging no coinciden con el Asset verificado. No publicar; conservar staging y revisar el origen. |
| `EXP_COPY_FAILED` | Export/copy | P2 | Según causa | La copia falló por una causa local distinta de falta de espacio/desconexión. Conservar el staging existente y registrar etapa/causa tipada. |
| `EXP_PUBLISH_FAILED` | Export/publish | P1 | Según causa | El rename no pudo confirmarse. Conservar staging y destino; inspeccionar ambos antes de reintentar. |
| `EXP_SOURCE_INVALID` | Export/preflight | P1 | No | El origen no es un Asset CBZ verificado o no coincide con su ruta/metadata. No leer ni exportar un origen no confiable. |
| `AUTH_LAN_RATE_LIMIT` | LAN/auth | P2 | Sí, tras espera | Se excedió límite de intentos. Rechazar temporalmente y registrar sin incluir PIN/secretos. |
| `AUTH_SECRET_STORE_FAILED` | Vault/store | P1 | Sí, tras corregir la causa | No se guardó el secreto protegido. No devolver referencia, truncar el secreto ni usar texto plano. |
| `AUTH_SECRET_UNAVAILABLE` | Vault/DPAPI | P2 | Sí, tras recuperar perfil o DPAPI | El perfil no puede descifrar el registro, DPAPI no está disponible o su verificación falló; estas causas no se distinguen con fiabilidad. Conservar ciphertext y no intentar fallback. |
| `AUTH_SECRET_CORRUPT` | Vault/verify | P1 | No | Payload descifrado no cumple versión o estructura. Conservar ciphertext para diagnóstico; no sobrescribirlo. |
| `AUTH_SECRET_NOT_FOUND` | Vault/read | P2 | No | La referencia no corresponde a un registro; no explorar rutas arbitrarias ni revelar datos del almacén. |
| `AUTH_SECRET_EXPIRED` | Vault/read | P2 | No | Credencial vencida. Conservar registro y solicitar renovación explícita a su owner. |
| `AUTH_SECRET_REVOKE_FAILED` | Vault/revoke | P2 | Sí, tras liberar el bloqueo | No se pudo eliminar el registro solicitado; no declarar revocación completada. |
| `NET_HOST_UNREACHABLE` | Network | P3 | Sí | Host no disponible. Aplicar retry/circuit breaker según operación y fuente. |

## 4. Reglas de emisión

- Las respuestas incluyen X-Trace-ID; si son errores, el cuerpo y la cabecera contienen el mismo identificador.
- La validación puede exponer únicamente ruta de campo y tipo de problema, limitada a 20 entradas; no se serializan valores ni mensajes internos del validador.
- Las familias válidas son `API_`, `SYS_`, `DB_`, `SRC_`, `DL_`, `IMG_`, `CBZ_`, `LIB_`, `READ_`, `EXP_`, `AUTH_` y `NET_`. La familia `API_` fue agregada por ADR-0021 para errores del contrato HTTP.
- Un error se emite en la capa que detecta la causa y se propaga conservando el mismo `trace_id`.
- Errores de input inválido, 401, 403 persistente y 404 estable no se reintentan indiscriminadamente.
- Las etapas de validación, empaquetado e indexado no se convierten en éxito para ocultar el fallo.
- Las acciones destructivas se bloquean cuando el error amenaza contenido o DB.

## 5. Criterios de verificación documental

- Cada código tiene familia, componente/etapa, severidad, política de retry y acción segura.
- No se usa texto del mensaje como identificador.
- La relación código → trace → job → UI → Support Bundle está definida en `LOGGING_SPEC.md` y `SUPPORT_BUNDLE_SPEC.md`.


