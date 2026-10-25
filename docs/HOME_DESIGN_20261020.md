# Inicio de Yomira — 20 de octubre de 2026

Diseño activo en la instalación E:\yomira, http://127.0.0.1:5173/.

## Cambios

- Cabecera de Inicio con acceso directo a encontrar una historia.
- Destacado en cuadrícula, con portada y estado del servicio en áreas independientes.
- Bloque de lectura con capítulo, página, barra y porcentaje; botón compacto «Continuar lectura» con nombre accesible que identifica el capítulo.
- Portada visible en móvil y cabecera de una columna cuando no hay ninguna portada destacada.
- Tarjetas de progreso con accesos «Leer» o «Capítulos», mejor contraste y títulos limitados visualmente a dos líneas conservando el nombre accesible completo.
- Obras recientes con títulos alineados y desplazamiento horizontal contenido en móvil.
- Se reutilizan las portadas, componentes, consultas, límites y dependencias existentes.

## Verificación

- Tipos y revisión estática sin errores; compilación de producción correcta.
- 50 pruebas existentes de App, SeriesCover e IllustratedBackdrop aprobadas, incluidas navegación, capítulos terminados, estados vacíos, errores, reintentos y portadas ausentes.
- Revisión visual de escritorio y móvil de 320 y 390 píxeles, títulos largos, portada ausente y enfoque de teclado.
- Acceso «Encontrar una historia» probado hasta Buscar y vuelta a Inicio.
- Tras activar, se recargó y comprobó la compilación real en el puerto 5173, también a 390 píxeles.
- 44 registros de progreso y 896 trabajos conservados; el contenido de reader_progress no cambió durante la activación.
- Copia anterior: E:\yomira\archive\home-design-20261020. Incluye fuentes originales, distribución anterior y copia coherente de la base de datos.

## Alcance

Solo se modificaron HomePage.tsx, HomePage.module.css y dos selectores de App.test.tsx, junto con una comprobación del nuevo enlace. No se modificó el lector ni el servidor; no se ejecutó nuevamente su batería completa. No se generó instalador ni se publicó una actualización en GitHub.
