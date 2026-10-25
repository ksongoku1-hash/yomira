# Inicio basado en el HTML de referencia — 21 de octubre de 2026

Se adaptó «Yomira · Inicio (rediseño).html» a la aplicación real, conservando los datos y las rutas existentes.

- Barra lateral de 72 px con iconos SVG; se amplía a 200 px con el puntero o el foco de teclado sin desplazar el contenido. En móvil pasa a una barra inferior con Inicio, Biblioteca, Buscar, Descargas y Más. Se conservan Ajustes, Diagnóstico y Exportar USB.
- Inicio con buscador funcional y atajo `/`, bloque para retomar la última lectura, porcentaje circular, portada real y acceso directo a la lista de capítulos. La lista se desplaza hasta su sección cuando finaliza la carga de la ficha.
- Filtros Todo / En curso / Terminados sobre los cuatro registros recientes, sin nuevas consultas. «Terminado» se refiere al capítulo guardado al 100 %, no al estado editorial del manga.
- «Sorpréndeme» abre una de las cuatro obras recientes ya cargadas de la biblioteca. No consulta el catálogo completo ni inicia la lectura automáticamente.
- Tarjetas con portadas, porcentaje, fecha, acceso de lectura y capítulos. Abrir un capítulo terminado mantiene el progreso guardado; no se implementó un reinicio de lectura.
- La nueva paleta se aplica fuera del lector. Se conservan el panel de configuración de lectura anterior, sus preferencias y las opciones de accesibilidad existentes.
- Se corrigió el caso de disponibilidad en caché: un fallo posterior al estado listo muestra el error y permite reintentar.

Validación: comprobación de tipos, análisis estático y compilación correctos. Las 150 pruebas aprobaron: 44 de la aplicación, 68 de experiencia de lectura, 23 de ficha de serie, 10 de capítulos online y 5 de portadas. Se verificaron en navegador los filtros, búsqueda, elección aleatoria, acceso a capítulos, expansión de la barra con teclado, cierre con Escape y el lector. El inicio y el menú caben a 320 px sin desbordamiento horizontal.

Activado en `E:\yomira`, en `http://127.0.0.1:5173/`. La captura `home-desktop.png` corresponde a la aplicación real después de activar la compilación; la revisión interactiva se realizó en un perfil separado. La activación conservó las 44 entradas de progreso y los 896 registros de descargas; no se escribió progreso personal.

Se guardaron los ocho archivos originales, la interfaz anterior y una copia coherente del perfil previo en `E:\yomira\archive\home-reference-20261021`.

No se generó instalador ni se publicó una actualización en GitHub. No se cambiaron la caché ni los proveedores de imágenes.
