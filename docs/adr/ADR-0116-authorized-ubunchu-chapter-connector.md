# ADR-0116 — Fuente autorizada acotada para Ubunchu! capítulo 9

## Status

Accepted — 2026-10-04.

## Context

La Alpha requiere demostrar búsqueda, descarga, creación de CBZ y lectura con una fuente real cuyo permiso de redistribución no dependa de una presunción. La página de Tarek Saier identifica *Ubunchu!* como manga de Hiroshi Seo, indica que él tradujo los capítulos 9–15 y presenta el capítulo 9 bajo CC BY-NC 4.0. El repositorio público contiene las páginas JPEG del capítulo 9 y una nota de licencia de la obra original CC BY-NC 3.0.

El registro existente no debe convertir ese permiso puntual en autorización para descargar catálogos arbitrarios ni para asumir que otras fuentes comerciales permiten almacenar sus imágenes.

## Decision

- Añadir el connector first-party `yomira.ubunchu.cc`, limitado al catálogo de un manga y al capítulo 9 en inglés. La búsqueda acepta el título y términos inequívocos del manga, consulta primero el proyecto público fijado y no enumera otros repositorios ni títulos.
- Usar exclusivamente `https://gitlab.com` para metadatos del proyecto, listado de páginas y transferencia de imágenes. No se usan cookies, tokens, URLs de páginas controladas por el usuario ni redirects a otros orígenes.
- Resolver la rama predeterminada a un commit SHA de 40 caracteres y fijar ese SHA en las referencias de capítulo y página. Así todas las páginas de un trabajo pertenecen a la misma revisión del repositorio aunque la rama avance.
- Exponer el capítulo solo si el árbol remoto contiene exactamente `page0001.jpg` a `page0011.jpg`, en orden consecutivo y con cada entrada tipada como archivo. Una página faltante, repetida o con otro nombre/tipo falla cerrada; nunca se genera un CBZ parcial.
- Incluir en la descripción normalizada atribución a Hiroshi Seo y Tarek Saier y las licencias declaradas para la obra original y la traducción. No se crean portadas ni capítulos adicionales sin evidencia de su licencia y forma de publicación.
- Mantener los límites y rutas de descarga, procesamiento, publicación atómica y biblioteca definidos por los ADR existentes. Las páginas no se registran en logs.

## Consequences

- La fuente es pequeña, reproducible y útil para una verificación end-to-end real bajo los términos no comerciales declarados por el titular de la traducción.
- La búsqueda no representa un catálogo general de mangas y el capítulo está disponible en inglés; no sustituye la prioridad de contenido en español para conectores posteriores.
- Si GitLab, la rama, la estructura de archivos o la licencia pública cambia, el connector reporta un fallo trazable y no degrada silenciosamente el conjunto de páginas.

## Verification

- Sitio de traducción: <https://sirtetris.gitlab.io/ubunchu-translation/>.
- Proyecto fuente: <https://gitlab.com/sirtetris/ubunchu-translation>.
- La respuesta actual del árbol del repositorio contiene once JPEG, `page0001.jpg`–`page0011.jpg`, bajo `ch09_files/jpg/`.
- Las pruebas unitarias verificarán búsqueda acotada, validación de proyecto/revisión, secuencia completa y contigua de páginas, referencias fijadas por SHA y rechazo de rutas o manifiestos alterados.

## Reversal Strategy

Deshabilitar o sustituir esta fuente requiere retirar explícitamente su registro y conservar las referencias de los trabajos ya persistidos para que sean identificables como no disponibles. Añadir títulos, capítulos, dominios, mirrors o credenciales requiere otro ADR y evidencia de autorización para cada conjunto de contenido.
