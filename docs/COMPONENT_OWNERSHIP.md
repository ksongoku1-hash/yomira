# Ownership de componentes

**Estado:** APPROVED — K003 aprobado formalmente por el usuario el 2026-09-25.

La columna **owner de fallo** identifica quién registra, clasifica y entrega el fallo al siguiente límite; no habilita a ese componente a saltarse la responsabilidad de otro.

| Componente | Entrada | Salida | Puede | No puede | Owner de fallo |
|---|---|---|---|---|---|
| React UI | Acciones del usuario y respuestas API | Solicitudes tipadas, estado visible, mensajes de error | Mostrar `PENDING`, errores y progreso | Leer archivos locales, DB o fingir `COMPLETED` | UI boundary para error de render; API conserva el error operacional |
| API | Requests HTTP local/LAN | Respuesta de éxito/error con contrato común | Validar schemas, crear `trace_id`, invocar servicios | Contener parsing específico de fuente en rutas | API boundary: validación y serialización |
| Job Manager | Solicitud de job, prioridad, estado persistido | Transiciones legales, cancelación, resultado | Coordinar etapas, retry/cancel según estado | Hacer scraping, escribir CBZ o saltar etapas | Job Manager: estado, etapa e intento |
| Source Connector | Query/IDs remotos y capabilities tipadas del host | Metadata, capítulos y referencias normalizados | Declarar capacidades y health | Importar DB/filesystem, escribir archivos/DB/library, manipular job o fabricar validación | Connector: error de parsing/red y health status |
| Download Engine | Referencias de página validadas y límites | Temporales `.part`, hash, progreso | Stream, timeout, retry permitido y límite por host | Usar `response.content` para imágenes grandes; publicar final | Downloader: error de transferencia y retryability |
| Content Validator | Bytes descargados y cabeceras | Página aceptada o error tipado | Verificar status, MIME, magic bytes, formato, decode y límites | Confiar solo en HTTP 200 o acceder a fuentes | Validator: causa de contenido inválido |
| Image Processor | Página validada y modo `OFF/SAFE/E_READER` | Original sin cambio o transformación registrada | Clasificar página y recortar solo si es seguro | Hacer HTTP o forzar recorte dudoso | Processor: decisión de transformación y error |
| CBZ Builder | Páginas aceptadas, orden, metadata conocida | CBZ temporal | Crear ComicInfo con valores conocidos y empaquetar | Consultar fuente, crear metadata inventada o indexar | Builder: fallo de ZIP/XML/escritura |
| CBZ Verifier / Publisher | CBZ temporal y expected manifest | Artefacto final publicado o fallo | Abrir ZIP, validar páginas/hashes y hacer rename atómico | Exponer final parcial o duplicados arbitrarios | Verifier/Publisher: aceptación de archivo final |
| Repository / SQLite | Comandos y entidades tipadas | Transacciones, IDs y lecturas | Aplicar constraints y persistir estados | Ejecutar SQL arbitrario del connector o coordinar red | Repository: errores DB/migración/integridad |
| DiskBudgetManager | Espacio libre, estimaciones y reservas | Decisión preflight con motivo | Bloquear lote inseguro antes de escribir | Asumir que espacio observado seguirá disponible | DiskBudgetManager: presupuesto insuficiente/cambio de volumen |
| Recovery Manager | DB, manifests, archivos y estado de arranque | Clasificación recoverable/retryable/orphaned/corrupted/cleanup | Reconciliar y preparar Safe Mode | Borrar temporales ciegamente o declarar incompleto como válido | Recovery: decisión y acción de reconciliación |
| ReaderAssetService | Chapter/page ID y solicitud autorizada | Stream de una página y cache headers | Resolver asset, aplicar caché con límite y servir | Aceptar rutas del cliente como filesystem paths | Reader service: page/CBZ/stream failure |
| Export service | CBZ publicado y destino removible | Copia verificada en destino y registro ExportJob | Detectar destino/espacio, copiar `.part`, verificar hash y renombrar | Considerar válida una copia parcial | Export service: disconnect, disk full o hash mismatch |
| Logging / Diagnostics | Eventos tipados y datos de entorno | Logs rotados y Support Bundle sanitizado | Correlacionar, rotar, excluir secretos/contenido | Registrar passwords/tokens o páginas/CBZ | Diagnostics: sanitización y generación del bundle |
| Recovery UI / Safe Mode UI | Diagnósticos permitidos y acciones de reparación | Estado, ayuda y comando de recuperación | Cambiar raíz, restaurar backup, reconstruir y exportar bundle | Encolar nuevas descargas en Safe Mode | Safe Mode flow: error de reparación presentado al usuario |

## Reglas de asignación

1. El componente que detecta un fallo conserva la causa técnica y el código; el Job Manager decide el estado persistente del trabajo.
2. Solo el Publisher afirma que el artefacto final fue publicado; el Repository confirma el indexado. La API informa `COMPLETED` después de ambos.
3. Si no es claro quién posee una operación o cleanup, no se ejecuta destructivamente: se registra para Recovery/Diagnostics.
4. Ningún módulo convierte un fallo de integridad en éxito para mejorar velocidad o apariencia de progreso.

