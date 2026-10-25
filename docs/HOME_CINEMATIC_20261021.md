# Inicio cinematográfico de Yomira — 21 de octubre de 2026

Diseño aplicado a la instalación principal E:/yomira y servido en http://127.0.0.1:5173/.
Referencias: Yomira - Inicio rediseñado.html y Yomira - Guia de interfaz.md, conservadas sin modificar en el escritorio del usuario; copia de referencia en el archivo de esta fase.

## Cambios

- Paleta nocturna azul y cian, iconos coherentes, botones y tarjetas según la guía.
- Menú lateral de 248 px, contraíble a 76 px; barra inferior con cinco destinos en móvil.
- Buscador persistente en las pantallas principales, atajo / y Sorpréndeme sobre obras recientes guardadas. El lector conserva su propio espacio y controles.
- Banner con hasta cuatro obras reales, flechas, puntos, pausa y rotación cada ocho segundos. Se detiene con foco, ratón, pestaña oculta, movimiento reducido o gráficos sencillos.
- Continuar leyendo conserva el capítulo y la página exactos. Filtros locales sin peticiones adicionales.
- Biblioteca y descubrimiento por géneros usan los registros recientes existentes, con peticiones acotadas y compartidas por la interfaz. Las obras guardadas se identifican con una marca de biblioteca.
- Se conservan los errores con su traza, los reintentos permitidos y los datos en memoria durante la recuperación del servicio.

## Datos pendientes

Los conectores actuales no exponen clasificaciones diarias ni un listado global de capítulos nuevos. Tendencias y novedades muestran estados informativos; no hay popularidad, fuentes o fechas inventadas. Las sugerencias actuales se limitan a géneros de las cuatro obras recientes y al historial reciente: conectar información más amplia requiere trabajo adicional en los proveedores.

## Validación

TypeScript, ESLint y compilación correctos. 156 pruebas pasadas: aplicación (45), banner (5), portadas (5), ficha (23), capítulos online (10), experiencia del lector (68).
Revisión visual e interactiva a 1440, 1024, 390 y 320 px. Menú 248/76 px, cinco destinos móviles, etiqueta de Configuración completa y sin desbordamiento horizontal de la página. Búsqueda, sorpresa, filtros, selección de destacado, cierre con Escape y acceso a capítulos comprobados.
El lector se comprobó únicamente sobre un perfil temporal: página 3 de 7 restaurada y cuatro modos de lectura disponibles. La instalación activa se revisó con sus portadas reales, sin abrir un capítulo ni modificar su progreso.
La activación conservó los 46 registros de progreso y los 896 registros de descargas. La comparación de progreso antes y después de activar resultó idéntica. Las capturas muestran The Boxer capítulo 25, página 5, y la lectura reciente de Hajime no Ippo.

## Recuperación

E:/yomira/archive/home-cinematic-20261021 contiene los archivos originales, el listado de archivos nuevos, la compilación anterior y una copia consistente de la base de datos previa a la activación. La compilación activa retiene los recursos anteriores necesarios para pestañas ya abiertas.
No se añadió ninguna dependencia. No se publicaron instaladores ni releases en esta fase.
