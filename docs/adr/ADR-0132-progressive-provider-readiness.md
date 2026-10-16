# ADR-0132 — Páginas disponibles antes de completar el capítulo

## Estado

Accepted para H2a, 2026-10-11. Incremento del motor y acceso común; no completa H2 ni activa el lector online.

## Contexto

FMD2 ya adquiere capítulos bajo un UUID estable, pero `acquire_pages` y la ruta de imágenes esperan a la finalización. El inventario de disco, `DownCounter` y el marcador `D` no prueban que el postprocesamiento haya terminado. Ampliar la ruta antigua a tareas activas también expondría imágenes sin la autorización que requiere el nuevo consumidor.

## Decisión

- Mantener la adquisición y el pipeline actuales. Añadir observación de esa misma adquisición mediante `SourcePageService.get_ready_pages`, con contrato opcional `ProgressivePageProvider`. No crear otra descarga al consultar disponibilidad.
- En FMD2, publicar la página únicamente después de cerrar la escritura, terminar correctamente `OnAfterImageSaved`, comprobar la existencia del archivo y comprobar que el thread no está terminado. Un registro sincronizado separado conserva ordinal, nombre y generación del intento. No añadir archivos de control al staging existente.
- Fijar el total después de preparar los enlaces y antes de descargar imágenes. El snapshot usa ordinales originales, aunque las imágenes terminen en otro orden. Acotar el plan a 10.000 páginas. Un total de cero significa que todavía no hay plan.
- Renovar la generación al reanudar o reintentar. No inferir disponibilidad activa a partir de archivos encontrados tras reiniciar. Los archivos recuperados pueden volver por el inventario completo existente cuando finalice el capítulo; deben pasar la validación del core.
- Añadir al estado autenticado `page_generation`, `planned_pages` y `ready_pages`. La ruta nueva `/v1/downloads/{uuid}/ready/{generation}/files/{name}` exige el token de control, la generación vigente y una página publicada o una tarea completa. La ruta anterior sigue exigiendo capítulo completo, con su compatibilidad de transporte anterior.
- Resolver referencias opacas `fmd2-page:v2` dentro del core. `PageFetchRequest` contiene una `SecretReference`, nunca el token. El transporte existente resuelve la credencial en memoria; conserva límites, comprobaciones de origen y reglas de redirects.
- Distinguir disponibilidad del proveedor y validez de la imagen. Reutilizar `DownloadTransport` y `validate_image_file` antes de ofrecer bytes a un lector. `ConnectorPageAvailability` no representa un asset verificado ni autoriza publicar un CBZ.
- Comprobar generación, total, ordinales, nombres y evolución del snapshot. Después de establecer el plan, rechazar cambio de generación/total, pérdida o sustitución de páginas y regresión de una finalización. Permitir pasar de un plan desconocido a un plan establecido.
- Actualizar el restaurador, el parche y sus hashes. Conservar el hash del ejecutable de release anterior; registrar por separado el ejecutable de desarrollo comprobado. No instalarlo ni declararlo release.

## Límites y siguiente consumidor

La disponibilidad nativa es de memoria y su generación identifica el intento, no un hash permanente de contenido. H2b debe persistir sesión, plan y hashes verificados; aplicar presupuesto, propiedad de archivos, retención de páginas en uso y recuperación; coordinar la adquisición activa con descargas y con `_settle_acquisitions`. Ninguna consulta de salud ni una segunda adquisición podrá cancelar una sesión que aún tenga consumidores.

El lookup del lector y progreso sigue siendo CBZ. No se guarda progreso contra un total supuesto ni se ofrece una ruta FastAPI pública para bytes nativos. El frontend permanece intacto hasta disponer de ese consumidor seguro.

## Validación y reversión

Una prueba de integración del core demuestra recepción, hash y decodificación de la primera página antes de terminar la adquisición, con un solo POST. La aceptación nativa usa el ejecutable compilado, una fuente Lua sintética y servidor IPv4 loopback: pausa el callback después de escribir la primera imagen y pausa la segunda respuesta; comprueba ocultación durante el callback, lectura y validación posterior, autorización, generación obsoleta, página pendiente y cierre sin terminación forzada.

Ejecutar regresiones de worker, transporte, imágenes, CBZ, lector, progreso, registro y lifecycle; pruebas concurrentes nativas; Ruff y tipos afectados. La reversión usa el punto H1b y no requiere migraciones ni eliminar datos.
