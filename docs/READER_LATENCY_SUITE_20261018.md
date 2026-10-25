# Menor latencia del lector y la interfaz — 18 de octubre de 2026

Activo en E:\yomira y http://127.0.0.1:5173/. Backend reiniciado; HTML activo comprobado contra la compilación.

## Cambios

- Apertura online, metadatos y progreso guardado se devuelven en una sola petición. La apertura no marca páginas como leídas.
- Cola de imágenes del navegador limitada a dos solicitudes, con prioridad de páginas visibles sobre precargas pendientes; las precargas en espera se cancelan al dejar de necesitarlas.
- MangaDex permite dos imágenes simultáneas, comparte peticiones duplicadas y conserva el motor nativo de otra fuente. Las imágenes tardías de una sesión renovada o pausada se rechazan antes de publicarse.
- Preparación de una sola imagen inicial o de reanudación al enfocar/pasar por Leer online durante 300 ms y cerca del final del capítulo. Solo MangaDex; el servicio verifica el conector elegido y rechaza preparar cualquier otra fuente, incluso si comparte un capítulo canónico. Preparar no puede renovar una sesión.
- Las consultas de biblioteca, serie, capítulos y trabajos pasan de 2 s a 15 s si no hay trabajos activos conocidos en la caché de la interfaz; conservan 2 s cuando sí los hay. Se detienen con la pestaña oculta. Otras consultas que tienen su propio intervalo mantienen su comportamiento.
- Ocho pantallas se cargan bajo demanda, con un estado de apertura accesible. Se conservan los recursos antiguos para las pestañas que ya estaban abiertas.

## Comprobaciones

115 pruebas del servicio y 440 de la interfaz aprobadas. La suite completa de interfaz pasó 439 casos; se corrigió la espera asíncrona de la prueba restante y se volvió a ejecutar todo App.test.tsx (41/41). frontend-passed.xml reúne los 440 casos únicos, sin fallos.
Comprobación de tipos, estilo y compilación aprobadas. Una advertencia de deprecación preexistente en Starlette/httpx.

Prueba real aislada: The Boxer 21, MangaDex, siete imágenes. Motor y perfil separados de los datos personales.

- Apertura inicial del capítulo y progreso: 557.82 ms; no incluye descargar la primera imagen.
- Reapertura con progreso: 9.93 ms.
- Imagen servida de caché: 8.29 ms.
- Dos imágenes en serie: 2877.03 ms; las mismas dos en paralelo tras renovar: 615.0 ms. Se verificó una concurrencia máxima de dos y contenido idéntico. Es una observación de esta conexión: la segunda ronda también se beneficia del calentamiento de la conexión/CDN, por lo que no representa una mejora garantizada atribuible solo al paralelismo.
- Preparación MangaDex mantuvo el dueño nativo activo; preparar una fuente nativa devolvió 409 sin cerrar su sesión.
- Progreso privado restaurado en la página 3; ninguna descarga nueva.

JavaScript inicial referenciado por HTML, incluyendo modulepreload: 557507 → 369207 bytes (-33.8 %). Gzip calculado por archivo: 158226 → 112359 bytes. No se suman recursos antiguos conservados que el nuevo HTML no pide.

Comprobación visual en perfil privado: abrir página 3, avanzar a 4, recargar y recuperar página 4 con imagen completa de 700 × 19589 píxeles. Opciones y modo vertical comprobados. Después de activar, la instalación principal muestra Servicio local listo y el progreso personal existente.

## Activación y recuperación

Copia consistente del perfil antes del reinicio y archivos originales en:
E:\yomira\archive\reader-latency-suite-20261017-182526

Se conservaron 39 registros de progreso y 896 trabajos. La activación no realizó escrituras de progreso. La interfaz anterior está en dist-before; los dos módulos del servicio tienen copia original. El script de activación incluye recuperación de esos módulos y del HTML anterior ante un fallo de arranque o comprobación.

## Límites

Los tiempos iniciales dependen de la fuente y la conexión; no se promete una descarga fría instantánea. Las fuentes nativas mantienen su acceso serial. Se conserva la caché persistente de imágenes existente (1 GiB), con los límites de memoria y validación anteriores. No se cambió el instalador 0.1.15 ni se publicó una versión en GitHub.
