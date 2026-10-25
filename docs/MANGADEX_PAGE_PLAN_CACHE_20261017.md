# Reutilización de listas de páginas de MangaDex — 2026-10-17

## Resultado

El conector MangaDex conserva durante cinco minutos las listas de imágenes que ya validó. Una reapertura de sesión o un cambio de pestaña ya no necesita repetir la misma consulta mientras la lista sea válida. Las peticiones simultáneas comparten la consulta de cada capítulo y calidad. Se mantienen separadas la variante ligera de lectura y la original de descarga: consultar una no modifica la calidad ni los requisitos de la otra.

La caché es solo de memoria, con hasta 32 combinaciones capítulo/calidad y 10000 referencias de imágenes en total. Elimina primero las menos usadas. No se guardan respuestas fallidas ni listas sin validar. La validación previa de UUID, hash, archivos y origen fijo uploads.mangadex.org se conserva. Las respuestas no pueden sustituir el origen por el campo baseUrl. No se añaden dependencias ni migraciones.

«Renovar imágenes del capítulo» y el reintento manual existente invalidan las listas ligera y original del capítulo antes de reabrirlo, además de renovar la caché de imágenes según el comportamiento previo. La invalidación espera cualquier consulta anterior para evitar que una respuesta vieja vuelva a ocupar la caché después de renovar.

## Verificación

106 pruebas del backend pasaron, incluidas 12 nuevas. Cubren consultas simultáneas, expiración exacta a 300 segundos, límites por entradas y referencias, calidad original, respuestas defectuosas, origen no fiable, invalidación con petición en curso y renovación por el servicio real del lector. Ruff y Pyright de los tres archivos modificados pasaron.

Prueba real en perfil y motor aislados, The Boxer 21 desde MangaDex: abrir la sesión requirió 577.90 ms inicialmente y 7.02 ms tras pausarla y reabrirla. Reabrir y volver desde una sesión FMD consumió en total una sola consulta de lista online. La imagen guardada coincidió byte a byte y el progreso de la página 3 se conservó. Renovar volvió a consultar la fuente; se comprobó también que invalidara la lista original de descarga. Las dos calidades realizan consultas separadas y reutilizan cada una sus propios resultados.

Activación local verificada en http://127.0.0.1:5173/: primera apertura de sesión 553.69 ms y reapertura con lista guardada 13.75 ms. Se verificaron identidades de sesión diferentes y la misma imagen recuperada. Son tiempos de las consultas de apertura de sesión, no de toda la interfaz ni de una descarga de capítulo completo.

## Alcance y límites

Cambios: backend/app/connectors/mangadex.py, backend/app/services/online_reader.py y backend/tests/test_mangadex_page_plan_cache.py. El lector de varias pestañas anterior y la caché persistente de imágenes continúan activos. La lista de páginas se olvida al cerrar la aplicación o al vencer sus cinco minutos; una primera apertura vuelve a consultar la fuente. No se cambiaron las consultas nativas de FMD. No se publicó una versión ni se regeneró el instalador de GitHub.

El activador no envió escrituras de progreso ni creó descargas. Se conservaron los 39 registros de progreso y 896 registros de descarga presentes antes del reinicio. Respaldo: archive/mangadex-page-plan-cache-20261017-174129, con fuentes previas, perfil privado, parche y resultados. No distribuir el perfil personal ni las copias de las pruebas.
