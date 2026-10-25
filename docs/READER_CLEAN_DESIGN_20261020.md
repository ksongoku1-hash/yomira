# Diseño limpio del lector, inicio y ficha

Dirección autorizada por el usuario tras revisar Manga Million. Aplicada en E:\yomira y disponible en http://127.0.0.1:5173/.

## Interfaz

- El capítulo actual es un desplegable en la cabecera del lector, con búsqueda, lectura de otros capítulos y descargas extras a través del componente existente.
- La cabecera conserva regreso a la serie, descarga, pantalla completa y opciones; el modo inmersivo pasa a Configuración > Más opciones.
- Barras discretas, botones secundarios sin recuadros en el marco principal y colores compartidos azul/navy. Se retira el acento morado local del lector y de la ficha.
- Progreso de capítulo en una fila encima de la navegación de páginas; las dimensiones reales siguen midiéndose para ajustar la superficie de lectura.
- Modo, encuadre y comodidad visual quedan primero. Controles, perfil e-reader y recuperación de imágenes se agrupan en un details nativo cerrado inicialmente.
- Escape cierra el panel de capítulos o descargas y devuelve el foco al botón que lo abrió.
- Inicio y fichas reducen bordes, superficies y sombras anidadas. La portada de la ficha adapta su ancho para dejar espacio al título y a las acciones en ventanas estrechas.
- Las filas de capítulos usan su altura natural y limitan visualmente el título a dos líneas; los nombres accesibles completos y destinos permanecen disponibles.

## Comprobación

- Tipos y revisión estática aprobados; compilación de producción final correcta.
- 184 casos distintos aprobados de ReaderPage, ReaderExperience, ReaderDownloads, App, SeriesDetailPage y OnlineChapters.
- Tras detectar un fallo del nuevo caso de foco con Escape, se corrigió el componente compartido y se repitieron sus dos suites afectadas: 79 casos aprobados. frontend-passed.xml reúne la última ejecución aprobada de cada suite, sin duplicar casos.
- Revisión visual real de página única, doble y vertical con The Boxer 21 en un perfil de prueba separado; las dos imágenes de doble página se cargaron con ancho de 619.5 píxeles cada una.
- Comprobados el grupo avanzado cerrado y oculto, cierre con Escape, panel móvil a 390 píxeles y selector de capítulo de 44 píxeles de altura.
- Inicio y ficha revisados en la instalación activa. Se comprobaron las filas de títulos largos en anchos de escritorio y 830 píxeles; ficha móvil de 390 píxeles sin desbordamiento horizontal.
- Se conservaron 44 registros de progreso y 896 trabajos. reader_progress no cambió durante las activaciones; las pruebas de lectura emplearon una copia privada del perfil.
- Versiones anteriores, fuentes originales y copia coherente del perfil en E:\yomira\archive\reader-clean-design-20261020.

## Alcance

Sin dependencias nuevas ni cambios del servidor, formatos de progreso, caché o fuentes. La revisión visual con cambios de modo y navegación se hizo en un perfil privado. No se repitió toda la batería del servidor ni se probó en hardware e-reader físico. No se generó instalador ni se publicó una actualización en GitHub.
