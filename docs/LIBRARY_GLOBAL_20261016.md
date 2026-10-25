# Biblioteca global — 16 de octubre de 2026

- La búsqueda consulta toda la biblioteca, con el filtro de lectura seleccionado, antes de contar y paginar los resultados. Se envía al pulsar Buscar o Enter para evitar una consulta por cada tecla. El botón Limpiar búsqueda conserva el resto de filtros.
- El orden por título, incorporación o actualización se aplica a toda la colección antes de paginar. No se descarga la colección completa al navegador: la interfaz conserva páginas de hasta 100 títulos.
- Se buscan el título canónico y el título visible, incluyendo las correcciones del usuario y su prioridad cuando están vacías o a null. La comparación ignora mayúsculas y minúsculas Unicode; los caracteres % y _ se buscan literalmente.
- Búsqueda, filtro, orden, vista y posición de paginación quedan en la dirección de Biblioteca. Los enlaces de portada y título conservan esa dirección para Volver a Biblioteca desde la ficha. El destino de vuelta solo admite rutas internas de Biblioteca o Descargas.
- Al cambiar la búsqueda, el estado o el orden se vuelve a la primera página. Si una página ya no tiene elementos porque la colección cambió, se ofrece volver a la primera sin perder los filtros. Se diferencia una búsqueda sin resultados de una biblioteca vacía.

Validación: 153 pruebas de interfaz, cliente API y ficha aprobadas; pruebas de servicio, API, vistas recientes y bibliotecas de 100, 1.000 y 10.000 mangas aprobadas; 19 pruebas de la conexión a la base de datos aprobadas. TypeScript, ESLint, Ruff, Pyright y compilación de producción correctos.

La búsqueda global del último título en el catálogo de prueba de 10.000 mangas y 100.000 capítulos tardó 24,43 ms en este equipo. Es una medida de esa consulta local, no una garantía para todas las colecciones o equipos. El listado y los resultados siguen paginados.

Comprobación real: búsqueda Boxer con Leyendo y vista compacta, entrada a la ficha y vuelta con los mismos controles; revisión a 320 píxeles sin desbordamiento horizontal; API real de búsqueda, orden, ausencia de coincidencias y rechazo de un orden inválido. Se conservaron 34 posiciones de lectura y los ajustes al activar la copia de E:. No se abrieron capítulos reales ni se iniciaron descargas durante esta comprobación.

No se añadieron dependencias ni migraciones de datos. El instalador y la Release de GitHub no se han actualizado en esta entrega.
