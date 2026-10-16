# ADR-0137 — Continuidad entre capítulos online

## Estado

Accepted, 2026-10-11. Complementa ADR-0135 y ADR-0136 sin modificar el motor de descargas ni la base de datos.

## Decisión

La navegación local conserva el listado de CBZ disponibles. La navegación online usa las referencias persistidas del capítulo y el catálogo validado de la misma fuente/serie. Comparte la consulta de catálogo con búsqueda y el panel de descargas, y su comparación de `chapterSortKey` con búsqueda. Los vecinos pertenecen al mismo idioma y tienen referencias utilizables; no se elige otra fuente o serie ante un error.

Consultar vecinos no registra capítulos ni solicita páginas. Al elegir anterior o siguiente, se reutiliza el endpoint de registro canónico sin trabajos de descarga. El resultado debe pertenecer a la serie actual antes de navegar con `online=1`. Una respuesta tardía no redirige a quien dejó el lector. El fallo deja disponible el capítulo actual y sus botones de reintento; se detiene el avance automático.

Una metadata online cacheada no implica que su plan siga abierto: el servicio conserva un único plan actual. Cada entrada al capítulo reabre su sesión, incluso al volver a una clave de consulta cacheada, y bloquea las consultas de páginas/progreso hasta completar esa reapertura. La caché de catálogo sigue siendo reutilizable; los blobs de páginas conservan los límites y liberación existentes.

## Verificación y límites

Se prueban orden de fuente, idioma, referencias ausentes, ida y vuelta con progreso, reapertura con caché existente, registro fallido, serie discordante, respuesta tardía y continuación habilitada. Un navegador real sobre una biblioteca sintética demuestra dos capítulos online con cero archivos, ambos progresos conservados, descarga explícita a CBZ íntegro y lectura local en la misma página.

Este cambio no certifica acceso a capítulos de fuentes externas ni recuperación automática de un proveedor reiniciado. La integración activa se limita a assets estáticos sobre el backend 0.1.12, con recuperación de la interfaz anterior; los instaladores anteriores no incluyen este incremento.
