# ADR-0089 — Cliente HTTP acotado para conectores de fuentes

## Estado

Accepted — 2026-09-27. Resuelve la dependencia `Connector → cliente HTTP acotado` ya declarada en `DEPENDENCY_RULES.md` y extiende de forma explícita la capability inicial de ADR-0054. La selección de ZonaTMO y ManhwaWeb y la autorización declarada por el usuario para descargar esos capítulos se registraron el 2026-09-27.

## Contexto

K221 requiere al menos un conector real compatible en V1. La inspección de ZonaTMO y ManhwaWeb encontró páginas públicas para buscar obras, consultar fichas y enumerar capítulos, pero no una API de connector documentada. El SDK actual solo entrega `SecretVault` a cada connector; sin un puerto HTTP, cada adaptador podría crear su propio cliente y saltarse límites de origen, redirects, tamaño de respuesta, timeout y pool global.

Los sitios pueden cambiar el HTML o presentar una página de autenticación, CAPTCHA, bloqueo u otro control. Debemos conservar errores trazables y parar sin automatizar una evasión. El código de connectors sigue siendo first-party confiable en proceso; este puerto no pretende ser un sandbox frente a código hostil.

## Decisión

1. El host inyectará `ConnectorHttpClient` dentro de `ConnectorHostContext`. `ConnectorRegistry` construirá un cliente scoped a los `network_origins` de su `ConnectorRegistration`; el connector no podrá elegir ni ampliar esa allowlist.
2. El puerto expone solo `GET` de documentos HTML y devuelve status, bytes, URL final y content type. No expone `httpx.AsyncClient`, cookies, credenciales, filesystem, sesiones DB ni acceso al job manager.
3. Cada redirect se valida antes de solicitar el siguiente destino. Se permiten como máximo cinco saltos; todos los orígenes deben pertenecer a la allowlist exacta del connector. Se exige HTTPS salvo los orígenes HTTP loopback usados exclusivamente por tests.
4. Los timeouts son connect/read/write/pool de 10/30/10/10 segundos. El pool compartido se limita a dos conexiones globales. Cada cuerpo HTML queda limitado a 4 MiB y se cuenta mientras se lee; una cabecera engañosa no aumenta el presupuesto.
5. El cliente no reintenta operaciones de búsqueda o metadata. Rechaza errores de red, redirect no permitido, exceso de tamaño o respuesta inválida con `SourceConnectorError` sanitizado: no registra URL, query, headers, cookies ni cuerpo.
6. Un connector debe fallar cerrado ante challenge/CAPTCHA, login inesperado, bloqueo, `403`, `429` o cambio de esquema que impida identificar los datos. No intenta eludir CAPTCHA, DRM, paywall, autenticación, rate-limit ni controles de acceso. Los reintentos de la transferencia de páginas siguen únicamente ADR-0086 y el contrato del `DownloadTransport`.
7. Las referencias persistentes de página no contendrán credenciales ni URLs con query tokens. Si el sitio solo entrega URLs efímeras o autenticadas que no pueden representarse sin guardar un secreto, PAGE_LIST/RESOLVE_PAGE se declara incompatible hasta una decisión y soporte explícitos del Vault.
8. Para preservar el alcance: ZonaTMO se evalúa primero como Connector A de K208/K221; ManhwaWeb queda como candidato Connector B para K215–K220 en V1.1, conforme a ADR-0084. Ambos nombres y URLs quedan registrados como candidatos seleccionados; el segundo no se convierte en requisito del Release Gate V1.

## Orígenes iniciales observados

- ZonaTMO: `https://zonatmo.org`, `https://storage.zonatmo.org` y `https://storage2.zonatmo.org`, observados en fichas/lectores públicos el 2026-09-27. No se aceptan subdominios comodín.
- ManhwaWeb: `https://manhwaweb.com`; su posible host de imágenes debe identificarse y revisarse antes de registrar `PAGE_LIST`/`RESOLVE_PAGE`.

## Alternativas consideradas

- Cada adapter crea su propio `httpx.AsyncClient`: rechazado porque permitiría límites divergentes y pools no contabilizados.
- `ConnectorHostContext` ofrece un cliente genérico con acceso a cualquier host: rechazado porque un connector podría cruzar el límite del registro o llegar a red interna.
- Ejecutar requests sin límite y parsear cualquier HTML recibido: rechazado porque un cambio de sitio podría consumir memoria sin cota o confundirse con contenido legítimo.
- Seguir redirects automáticamente: rechazado porque el host no podría validar cada destino antes del acceso.
- Automatizar autenticación o controles de acceso: rechazado porque excede la autorización de fuentes públicas y puede quebrar las reglas de los sitios.

## Consecuencias

- `ConnectorHostContext` deja de tener `SecretVault` como única capability: incluye un cliente HTTP tipado, read-only, acotado y scoped por registro. Se conservan las prohibiciones de DB, rutas y escritura local de ADR-0054.
- Los adapters reales pueden consultar HTML público sin cargar una dependencia de parser adicional; se usarán parsers estándar y fixtures offline.
- Los cambios de los sitios pueden dejar un connector temporalmente incompatible. El connector preserva el fallo trazable y no descarga ni indexa contenido incompleto.
- Agregar métodos HTTP, cookies persistentes, origins nuevos o capacidad de autenticación requiere ADR, ajuste de pruebas negativas y revisión de secretos/privacidad.

## Verificación requerida

- Probar cada connector con fixtures HTML offline y un test contractual del SDK; incluir orden, duplicados, referencias malformadas, HTML vacío, challenge y schema change.
- Demostrar que URL/origen externo y redirect fuera de allowlist son rechazados antes de abrir socket; cubrir timeout, `403`, `429` y body de 4 MiB + 1 byte.
- Verificar que la sesión compartida nunca excede dos conexiones, que los recursos se cierran en shutdown y que requests no registran URLs ni bodies.
- Ejecutar una comprobación real pequeña, opt-in y sin secretos: búsqueda pública → ficha → capítulos → resolver referencias de página; las pruebas de bytes usan siempre fixtures sintéticos.
- Completar el E2E local del Connector A solo después de confirmar orígenes exactos y parser estable: Search → job durable → `.part` → validación/hash → CBZ verificado/publicado → índice/reader → cierre/reapertura.
