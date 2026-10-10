# ADR-0105 — Cotas de conectores y renovación de firmas transitorias

## Estado

Accepted — 2026-09-30. Corrección de los hallazgos de la revisión NovelCool/ManhwaWeb, autorizada explícitamente por el usuario con «resuelve esos problemas y déjame abrir la pagina para empezar a probar». Conserva el plan inicial, la identidad durable y el contrato síncrono de `resolve_page` de ADR-0083.

## Contexto

La revisión del 2026-09-30 reprodujo tres fallos P2: HTTPX descomprimía antes de comprobar la cota de 4 MiB; el parser NovelCool duplicaba texto en cada enlace o encabezado anidado; y una firma CDN podía caducar entre `get_pages` y la descarga, especialmente tras una pausa. Ninguno justifica ampliar los orígenes, persistir firmas ni cambiar las cuotas.

## Decisión

1. El cliente HTML/JSON solicita `Accept-Encoding: identity`, como el transporte de imágenes existente. Rechaza cualquier otra codificación antes de leer el stream y lee bytes crudos bajo la cota vigente de 4 MiB. Las respuestas ya materializadas de los transportes de prueba siguen sujetas a la misma cota; no hay descompresión automática en el cliente del conector.
2. El parser NovelCool rechaza enlaces anidados y encabezados h1/h2/h3 anidados antes de almacenar el segundo colector. Un enlace dentro de un encabezado sigue permitido; cada grupo conserva como máximo un colector activo. Se mantienen las cotas de cuerpo y nodos existentes.
3. `NovelCoolConnector.resolve_page` sigue siendo síncrono y no hace I/O. Revalida la firma transitoria en RAM al resolver. Una firma del cache que ha perdido validez genera el error sanitizado existente `SRC_CONNECTOR_FAILED`, operación `RESOLVE_PAGE`, `retryable=True`. Nunca devuelve la URL vencida. Una referencia inválida o ausente sigue fallando sin retry.
4. Ante ese error concreto, el worker usa `get_pages` con la referencia de capítulo que ya posee, mediante la espera cancelable existente. Solo intenta una renovación por resolución de página; la segunda resolución no abre un bucle de renovaciones. La secuencia nueva debe coincidir exactamente con todo el plan anterior en identidad, cantidad, orden y duplicados. Si difiere, usa la clasificación existente de `DownloadWorkerIntegrityError`: `INTERRUPTED`, `recovery_blocked=True` y `DL_JOB_RECOVERY_BLOCKED`, preservando el plan y los artefactos. No reemplaza filas ni manifiestos para acomodar otro contenido. Una segunda firma vencida o un error ordinario de fuente usa la salida `FAILED` trazable existente.
5. Las firmas renovadas viven exclusivamente en el cache acotado del conector y en `PageFetchRequest`. SQLite, manifest, nombres de archivos, CBZ y logs siguen recibiendo únicamente identidades opacas y errores sanitizados. Tanto renovación como descarga usan las mismas allowlists y límites. La renovación no añade retries a HTTP 403/429, no sortea challenges y no aumenta el límite durable de intentos de imagen.

## Consecuencias

- Se reutilizan el SDK, el cliente scoped, la cancelación, el control de integridad del worker y la política identity/raw del downloader; no se añaden dependencias, métodos del SDK ni acceso a filesystem para el conector.
- Un servidor que ignore `identity` y entregue contenido comprimido se rechaza explícitamente. Soportar compresión requeriría un decoder con cota demostrada; no se habilita como fallback silencioso.
- Renovar puede relistar hasta las 10.000 páginas ya permitidas. El trabajo es finito y solo sucede cuando una resolución pierde su firma; no hay refresco periódico ni renovación ilimitada. Si la fuente entrega firmas ya vencidas, cambia las referencias o falla, el trabajo conserva su salida trazable y no publica un CBZ incompleto.
- Esta corrección no constituye aprobación del instalador ni de los gates físicos o visuales pendientes.

## Verificación

- RED → GREEN para codificaciones gzip/br/deflate antes de leer bytes, anidamiento de enlaces/encabezados y caducidad posterior a `get_pages`.
- Renovación del worker con un capítulo sintético: conserva referencias, completa descarga/CBZ/lector y sobrevive a reapertura; rechaza drift y renovación sin firma válida, sin un tercer intento.
- Pruebas focales de todos los conectores, API, worker y transporte; Ruff/Pyright con el runtime fijado.
- Arranque de una candidata nueva en una raíz local de pruebas, readiness y UI/API servidas por el mismo origen loopback. No se repite la descarga real de la serie ya probada.
