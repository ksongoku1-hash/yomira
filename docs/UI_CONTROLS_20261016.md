# Controles del lector y ficha — 16 de octubre de 2026

La barra del lector usa iconos coherentes y conserva nombres accesibles. En pantallas pequeñas muestra controles compactos. El menú Capítulos permite buscar, cambiar de capítulo y descargar capítulos individuales; si no hay una fuente disponible, muestra las copias locales. La descarga múltiple se conserva y la descarga del manga completo se agrupa en Más opciones.

El panel se mantiene abierto en modo inmersivo, responde a Escape y devuelve el foco al botón al cerrarlo. La ficha conserva lectura, descargas, actualización, información y administración con la misma lógica de negocio.

Validación: 91 casos distintos de interfaz cubiertos, incluidos recorridos de 100, 500 y 2.000 páginas. Un caso se repitió tras actualizar el nombre accesible del enlace de regreso. TypeScript, ESLint y compilación de producción correctos. Revisión visual a 320 y 390 píxeles, sin desbordamiento horizontal. Las posiciones y fechas de guardado del historial real se mantuvieron al activar la entrega. Las interacciones de lectura se comprobaron con datos de prueba aislados.

No se modificaron contratos de API, el motor de descargas ni la estructura del perfil. No se añadieron dependencias. No se ha publicado una nueva versión en GitHub.

Copia de recuperación: archive/ui-controls-20261015-235621
