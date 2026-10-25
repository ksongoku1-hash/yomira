# Filtros de Descargas — 17 de octubre de 2026

- La cola tiene siete filtros: Todas, En curso, Pendientes, Terminadas, Fallidas, Interrumpidas y Canceladas. Cada uno muestra el contador de toda la cola del ámbito elegido, incluidos los trabajos fuera de la página actual.
- El servidor filtra antes de paginar y mantiene el orden existente. Se conservan las páginas de 50 trabajos. En curso incluye preparación de metadatos, descarga, validación, procesamiento, empaquetado, verificación e indexación.
- La API admite estados repetibles y valida valores y cantidad. Los campos anteriores total y states mantienen sus totales generales; filtered_total solo se añade cuando se pide un filtro y controla la paginación de ese resultado. El cliente comprueba la coherencia entre estados, resultados y totales. No se añade una consulta para contar el filtro.
- La URL conserva filtro, página y vista de retirados. Al cambiar el filtro se vuelve a la primera página. La ficha permite volver a la misma selección de Descargas mediante un destino interno validado.
- Un filtro sin coincidencias indica su propio estado vacío y permite mostrar todos los trabajos. Si una página deja de tener resultados, se ofrece volver a la primera sin perder el filtro. Se mantienen pausa, cancelación, reintento, retirada y restauración.

Validación: 43 pruebas del servidor y 185 de interfaz, cliente API, ficha y aplicación aprobadas. Incluyen filtrado antes del límite, estados combinados, archivo, contadores generales, paginación, filtros vacíos, vuelta desde la ficha y controles existentes. Ruff, Pyright de la aplicación, TypeScript, ESLint y compilación correctos. Los avisos de tipado preexistentes del archivo de pruebas del servidor no forman parte de este cambio; se conserva su diagnóstico separado.

Activado en E: después de comprobar que no había descargas pendientes ni en curso. Se conservaron las 34 posiciones de lectura y la configuración tras el reinicio. Revisión visual real con 19 trabajos terminados: selección de Terminadas, Fallidas sin resultados, Mostrar todas y vuelta desde una ficha conservando el filtro. Comprobado a 320 píxeles sin desbordamiento horizontal. Sin errores ni advertencias en la consola de esa revisión. No se abrieron capítulos ni se iniciaron o modificaron descargas reales; los controles de modificación se verificaron en pruebas aisladas.

Las fuentes anteriores, la interfaz anterior y una copia privada de recuperación de la base de datos están guardadas en el archivo local de Yomira. El ZIP contiene únicamente los diez archivos de código cambiados y estas notas, sin datos personales.

No se añadieron dependencias ni migraciones. No se recompiló el instalador ni se publicó una Release de GitHub.
