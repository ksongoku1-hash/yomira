# ADR-0131 — Registro de capítulos para lectura sin crear descargas

## Estado

Accepted para el incremento de identidad canónica de H1b, 2026-10-11, bajo la autonomía técnica del plan de lector híbrido. No declara H1b completa ni habilita lectura online.

## Contexto

Hasta H1a, un capítulo remoto obtenía su identidad local al crear un trabajo de descarga. Abrirlo para lectura necesita la misma identidad, pero no debe crear un job, adquirir un capítulo, publicar un CBZ ni generar staging como efecto oculto.

Las reglas existentes en `DownloadJobService` resuelven serie, edición, idioma, referencias antiguas FMD2 y capítulos locales disponibles. Replicarlas en el lector produciría dos criterios de identidad. El lector y el progreso siguen exigiendo un asset CBZ verificado hasta implementar una sesión online con plan validado.

## Decisión

- Extraer la selección y el registro transaccional dentro del servicio existente. `register_reader_chapter` y `create_selected_jobs` usan `_select_chapters` y `_register_selected_chapters`. Se conserva la secuencia registro → job dentro de una única transacción para una selección de descarga.
- Mantener el nombre y la composición de `DownloadJobService` en este incremento. Crear un segundo servicio que delegue sus métodos privados no aporta una responsabilidad independiente todavía. Una extracción de módulo se reconsiderará cuando exista el consumidor de sesiones online.
- Añadir `POST /api/v1/connectors/{connector_id}/series/{remote_series_id}/reader-chapter`, con `chapter_remote_id` estricto, no vacío y de hasta 512 caracteres. La selección se comprueba contra el catálogo obtenido del conector registrado, con capacidades PAGE_LIST, METADATA y CHAPTER_LIST.
- La respuesta contiene únicamente `series_id`, `chapter_id`, `display_label` y metadata de la petición. No incluye un número de páginas inventado, una URL de imagen, una ruta local ni un token. Registrar no significa que el capítulo esté listo para leerse.
- Compartir también la consulta de metadata/catálogo en las dos rutas. Mantener los errores sanitizados, la traza y el trabajo SQLite fuera del event loop.
- Una asociación de fuente existente tiene prioridad sobre una coincidencia posterior por número de capítulo. Esto evita cambiar el `chapter_id` de una futura lectura al aparecer otro CBZ.
- Cuando las reglas actuales permiten reutilizar un capítulo local disponible, persistir el vínculo de la fuente con ese capítulo. Si el archivo desaparece, una nueva petición conserva la identidad y el job posterior usa el mismo capítulo. No renombrar ni fusionar capítulos ya registrados con IDs distintos.
- Conservar el reconocimiento de referencias FMD2 v1/v2/v3 y sus reglas de edición, idioma, variante, volumen y catálogo. Rechazar una referencia exacta que ya pertenezca a otra serie, incluso si hay un CBZ con el mismo número en la serie solicitada.

## Compatibilidad y límites

No cambia el esquema SQLite, la tabla de progreso, el formato de CBZ/ComicInfo, el transporte, los manifiestos, el presupuesto de disco, los controles de fuentes o el frontend de producto. Los registros nuevos usan las tablas existentes; no se activa la ruta nueva en la instalación personal.

El lookup de progreso sigue siendo el lector CBZ. Una identidad remota sin páginas no es una lectura disponible y no admite guardar un índice. Para admitir progreso online hace falta primero un plan validado y versionado; no se debilita esa verificación para simular lectura web.

La biblioteca puede contener metadata de una serie registrada sin CBZ, igual que en la importación de metadata ya existente. Los endpoints de capítulos listos siguen filtrando por los assets verificados.

## Validación

Las pruebas cubren idempotencia, reapertura SQLite, ausencia de jobs/archivos/adquisición, registro simultáneo con descarga, reutilización del capítulo al publicar CBZ, selección rechazada sin escritura, identidad FMD2 tras reordenar, falta de capability, lineage y archivo local ausente. Las integraciones del worker comienzan registrando el capítulo y verifican que el job conserva sus IDs antes de probar transporte, recuperación, CBZ y lector.

Los resultados y las limitaciones están en [CONTINUATION_H1B.md](../hybrid/CONTINUATION_H1B.md). La prueba completa online → CBZ → offline con progreso sigue pendiente de H2–H4.

## Recuperación

El ZIP de H1a se conserva antes de modificar la copia. El parche de este incremento se aplica sobre el árbol H1a, también sobre su snapshot reconstruido `8c65368`. El snapshot tiene el mismo árbol Git que el commit anterior `f5dc6a3`.

Revertir este incremento retira la ruta y la extracción; no elimina filas, archivos ni progreso. No restaurar archivos de código sobre la biblioteca ni borrar asociaciones nuevas para deshacer una actualización.
