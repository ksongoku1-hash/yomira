# Recuperación del lector tras perder la sesión — 2026-10-17

## Evidencia del fallo

La traza 813be9f1-e581-4d58-9c82-36bfef01d803 aparece en el registro de la aplicación a las 21:21:44.952 UTC con GET 404. Hubo también un PUT 404 a las 21:21:44.946 UTC. El servidor había arrancado a las 21:21:34.835 UTC, unos diez segundos antes. El código de apertura de páginas devuelve 404 cuando su sesión temporal ya no existe. La pestaña abierta retenía los datos del capítulo de la sesión anterior. Esto explica esta captura; no demuestra que todos los errores 404 tengan la misma causa.

## Corrección

ReaderPage detecta un 404 en una página visible, en la consulta de progreso o en su guardado. Reabre el capítulo una vez por visita con restart=false: conserva la caché persistente y la página actual. Si había un guardado fallido, vuelve a guardarlo cuando la página recuperada se ha decodificado. Varias páginas fallidas comparten un único intento.

Se usa la recuperación existente, con su comprobación de serie y cantidad de páginas; una estructura diferente detiene la lectura para evitar asociar progreso a otra edición. Los errores de precarga fuera de las páginas visibles no interrumpen la lectura actual. Si falla el intento automático, el usuario puede reintentar manualmente. Un error local no abre fuentes en línea. El mensaje durante la reconexión explica que se restablece la página actual; el 404 online persistente ya no afirma que la fuente carezca de la página.

Alcance: frontend/src/reader/ReaderPage.tsx y ReaderExperience.test.tsx. Sin cambios al backend, esquema personal ni políticas de integridad. No se añadieron dependencias.

## Verificación

88 pruebas de interfaz pasaron, con tipos, lint y compilación correctos. Incluyen lectura normal, vertical y doble, página 6 conservada, errores concurrentes, guardado posterior a decodificación, consulta inicial de progreso, precarga, fallo persistente, fuente caída o con páginas cambiadas, salida durante reconexión y lectura local.

Prueba visual con perfil y motor aislados, usando The Boxer 21 desde MangaDex: se cargó la página 1, se cerró su sesión mediante la API y se confirmó que la página 6 devolvía 404. Al saltar a la página 6, el lector reabrió la sesión automáticamente sin pulsar Reintentar. Se comprobó una sesión nueva, imagen validada y progreso guardado en índice 5. La prueba no modificó el historial personal de la aplicación habitual.

## Activación y límites

Interfaz activada localmente en E:/yomira y http://127.0.0.1:5173/ sin reiniciar el backend. Se conservaron los recursos de la interfaz anterior para las pestañas que los tengan referenciados. El activador no escribe la base de datos personal; guarda una copia consistente previa.

Cada visita permite una recuperación automática para impedir bucles; tras otra pérdida, queda el reintento manual. El backend sigue manteniendo un capítulo online activo, por lo que varias pestañas leyendo capítulos distintos todavía pueden reemplazar la sesión de la otra. El fallo real de una fuente o una imagen inexistente no se corrige inventando contenido. La corrección es local; no se publicó un instalador ni una versión de GitHub.

Respaldo: archive/reader-session-recovery-20261017-165527. Incluye fuentes anteriores, interfaz previa, perfil previo privado, parche y evidencia. No distribuir los perfiles de prueba ni el respaldo de datos personales.
