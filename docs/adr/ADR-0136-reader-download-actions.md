# ADR-0136 — Descargas explícitas desde el lector

## Estado

Accepted, 2026-10-11. Complementa ADR-0135; no cambia su lectura de páginas en memoria.

## Decisión

El lector React existente ofrece descarga del capítulo actual, selección de capítulos y manga completo. Las acciones usan el servicio y endpoint de trabajos existentes. El lector no inicia ninguna descarga al abrir un capítulo ni al consultar sus referencias.

`GET /api/v1/reader/{chapter_id}/download-source` resuelve exclusivamente metadata persistida: identificador de capítulo canónico, conector, serie remota y capítulo remoto. Se exige una asociación inequívoca a la serie y una fuente disponible con versión y capacidades compatibles. No se exponen URLs de páginas ni secretos. Una fuente no utilizable produce `data: null`; no se selecciona otra edición. Un capítulo inexistente produce 404 y un servicio ausente 503.

El catálogo y disponibilidad se consultan al abrir el panel. El catálogo comparte caché de consulta con búsqueda; la disponibilidad se actualiza solo mientras el panel está abierto. Se muestran 100 filas por página y se conserva la selección al paginar. Mantener el panel abierto evita que el modo inmersivo oculte sus acciones.

La creación de trabajos de búsqueda y lector comparte un helper que valida toda la selección antes de enviar, elimina referencias duplicadas y respeta el máximo de 10.000 capítulos únicos y lotes de 100. Un fallo detiene los lotes posteriores y conserva los anteriores. Reintentar reutiliza los trabajos mediante la idempotencia del backend. No se alteran publicación atómica, ComicInfo.xml, controles de fuente, reserva de disco ni identidad de progreso.

## Verificación y límites

Se prueban la identidad del capítulo, conservación de progreso, fuentes ambiguas o no disponibles, respuestas inválidas, ausencia de consultas cuando el panel está cerrado, selección entre páginas, lotes de 105 capítulos y fallos parciales. En un navegador real sobre Windows se verifica lectura online sin archivos, descarga desde el lector, un único CBZ íntegro al repetir y reanudación local en la misma página tras interrumpir la sesión online.

La aceptación usa fixtures sintéticos en una biblioteca aislada; no certifica fuentes externas ni el instalador. La descarga completa comprende las referencias disponibles en el catálogo autorizado. No incorpora navegación automática entre capítulos ni recuperación de un proveedor reiniciado, que siguen pendientes.
