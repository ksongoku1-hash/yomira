# ADR-0110 — Conexión de LectorXD y alta de series sin descarga

- Estado: ACCEPTED para el alcance solicitado por el usuario: cancelar la comprobación del navegador y conectar la fuente para alimentar la biblioteca.
- Fecha: 2026-10-02 (America/Bogota).
- Autoridad: SPEC, ADR-0054, ADR-0089 y petición explícita de incorporar LectorXD. No cambia los gates de integridad del downloader ni del CBZ.

## Evidencia y decisión

El [conector público de HaruNeko](https://github.com/manga-download/haruneko/blob/fce2710df1c49a6724a15f7ce80f0a7fc85e0f2e/web/src/engine/websites/LectorXD.ts), commit `fce2710df1c49a6724a15f7ce80f0a7fc85e0f2e`, documenta catálogo GET `/catalogo?page=1`, fichas `/(manga|manhwa|manhua|novela)/slug`, títulos en `section img[src*="/covers/"]` y capítulos en el island Astro `ChapterList`. Sus selectores de lector no enumeran orígenes de imágenes. La [serialización oficial de Astro](https://github.com/withastro/astro/blob/4c1470a7f907fe678ef5e7dceaa972ca83d297da/packages/astro/src/runtime/server/serialize.ts) permite leer las tuplas de `props` sin ejecutar JavaScript. Estas referencias son evidencia de un contrato publicado, no de una respuesta actual del sitio.

Se registra `yomira.lectorxd`, versión `0.1.0`, con SEARCH, METADATA y CHAPTER_LIST. PAGE_LIST permanece ausente; solicitar páginas o crear jobs para esta fuente devuelve `SRC_CAPABILITY_UNSUPPORTED` antes de persistir trabajos. La verificación del navegador queda cancelada por el usuario. Durante esta implementación no se accede a LectorXD ni a sus CDN mediante otra herramienta.

El único origen autorizado es `https://lectorxd.com`. Se reutiliza el transporte GET existente: TLS verificado, sin cookies ni credenciales, 4 MiB por respuesta, cinco redirects dentro de la allowlist, dos conexiones globales, una por host y timeouts vigentes. No se añaden browser runtime, cloudscraper, POST remoto ni dependencias.

## Catálogo y referencias

- La búsqueda rápida consulta como máximo tres páginas consecutivas del catálogo, empezando en 1. La persona puede solicitar una ampliación acotada a un máximo de veinte páginas; la consulta sigue siendo secuencial, se detiene ante una página sin entradas y devuelve como máximo 100 coincidencias únicas. El tope de veinte páginas se registró en ADR-0111. La interfaz diferencia la búsqueda rápida de la ampliada e informa que ninguna de las dos cubre todo el catálogo.
- Pegar una URL pública de ficha permite encontrar directamente esa serie sin recorrer el catálogo. Se aceptan únicamente las rutas de ficha documentadas en el origen exacto, sin credenciales, query ni fragment. Los identificadores persistentes son rutas canónicas sin URLs firmadas.
- El parser HTML mantiene las cotas existentes de 120 000 elementos, profundidad 256 y 4 MiB. Las colecciones se limitan a 10 000 capítulos. La lectura de Astro acepta exclusivamente los tipos de tupla requeridos para `chapters` y `chapter`; no ejecuta componentes ni scripts. Un esquema incompleto o inesperado produce un error trazable, no una lista vacía falsa.
- Se conserva el orden de capítulos de la fuente, las etiquetas y su atribución. Título y descripción proceden de la ficha; no se inventan autor, estado ni géneros. Una portada solo se conserva si es estática y pertenece al origen autorizado. Una portada externa o firmada queda ausente y puede usar el respaldo de metadatos ya existente.

## Alta en biblioteca

Se añade `POST /api/v1/connectors/{connector_id}/series/{remote_series_id}/library-series`, sin cuerpo. El host obtiene y normaliza metadatos mediante METADATA y guarda Source, Series y SeriesSource en una transacción. La asociación es idempotente por fuente y referencia. No crea capítulos, trabajos ni CBZ; no fusiona series de distintas fuentes por similitud de título.

Los metadatos siguen atribuidos a SeriesSource y respetan los valores canónicos y overrides locales. La interfaz ofrece «Añadir a biblioteca», confirma la identidad local devuelta e invalida el listado de biblioteca. La portada se asegura después de la transacción mediante el pipeline vigente; un fallo de portada se registra y no convierte un alta ya confirmada en un fallo ficticio. Los conectores no reciben DB ni filesystem.

La interfaz muestra capítulos únicamente con CHAPTER_LIST y controles de descarga únicamente con PAGE_LIST. La API también exige PAGE_LIST al crear jobs: el control de capacidades no depende de la interfaz.

## Verificación y límite práctico

Pruebas locales con fixtures sintéticos del contrato público: catálogo acotado, URL directa, metadatos opcionales, Astro, límites, HTTP fallido, origen ajeno, URLs firmadas y errores sin datos sensibles. Pruebas API: alta idempotente, cero jobs, aislamiento de fuentes y conservación de overrides. Pruebas UI: LectorXD seleccionable, alta y consulta de capítulos, sin controles de descarga. Ruff, Pyright, TypeScript y ESLint sobre el alcance modificado.

No se declara verificado el acceso actual a LectorXD ni una descarga desde esta fuente. Habilitar PAGE_LIST requiere documentar los orígenes efectivos y redirects de imágenes, probar referencias transitorias y superar el pipeline de validación/publicación CBZ existente. Esta integración tampoco corrige el HTTP 403 de ZonaTMO ni el HTTP 404 de ManhwaWeb.
