# Recomendaciones y fuentes — 21 de octubre de 2026

Activado en la instalación de E:/yomira, http://127.0.0.1:5173/.

## Comportamiento

- «Populares online» muestra un ranking público de MangaDex por número total de seguidores. Se solicitan obras con capítulos disponibles en español; no se presenta como tendencia diaria.
- Se consultan hasta 24 títulos, con portadas, géneros, nombres alternativos, atribución y fecha visibles. Inicio muestra 12 populares y hasta 8 opciones de descubrimiento.
- «Buscar fuentes» abre la búsqueda existente por título. Se comprueba la disponibilidad allí; no se consulta cada título en todos los sitios al abrir Inicio.
- «Otros nombres» permite buscar títulos alternativos sin mezclar automáticamente ediciones diferentes.
- «Descubre algo nuevo» filtra por género y prioriza coincidencias con géneros de lecturas recientes cuando esos metadatos están disponibles. Con metadatos insuficientes muestra una selección general y lo explica. Ese cálculo se realiza localmente: no se envía el historial al proveedor del ranking.
- Caché pública persistente de 24 horas, última lista conservada hasta 7 días ante errores y pausa de 15 minutos entre intentos fallidos. Las dos secciones comparten la consulta. El historial, los archivos y el progreso personal permanecen separados.

## Fuentes

Se agregaron LeerCapitulo y MangaOni: 11 sitios en el motor FMD2 más MangaDex, 12 integrados en total. El desplegable «Fuentes habilitadas» muestra nombres e idiomas; los resultados confirman qué sitios responden para cada obra.

Se comprobaron búsquedas, catálogos y una imagen real de Look Back (LeerCapitulo, 143 páginas) y Eri Sayonara (MangaOni, 200 páginas), en un perfil de prueba. No se importaron estas obras al perfil personal. KuManga quedó fuera porque su visor público denegó el acceso; LectorManga no se habilitó por incompatibilidad con el módulo disponible.

## Validación y conservación

280 pruebas: 110 del servidor y 170 de la interfaz. Comprobaciones de tipos, estilo y compilación correctas. Ejecutable nativo compilado y fijado por SHA-256; scripts de restauración y distribución actualizados y analizados sintácticamente. Autenticación del motor conservada.

Se verificó en la aplicación activa el recorrido Solo Leveling → Buscar fuentes → resultados de LeerCapitulo y MangaOni, los nombres alternativos, el filtro Acción y la lista de 12 fuentes. Las cinco primeras portadas del ranking cargaron. Sin desbordamiento de página a 1440 ni 320 píxeles.

Tras activar y revisar la interfaz permanecen las 46 posiciones de lectura y los 896 registros de descargas. La activación no escribe en la base personal. Copia consistente anterior: profile-before.sqlite3; interfaz anterior: dist-before; motor anterior: fmd-before.exe; código anterior: originals/ y files.json.

La lista real inicial tardó aproximadamente 1,10 segundos en la prueba del servicio; la segunda consulta se sirvió de la memoria caché. Esto no mide ni garantiza el tiempo de carga de capítulos de terceros.

Versionado local 0.1.15. No se generó ni publicó un instalador o una actualización de GitHub en este cambio.
