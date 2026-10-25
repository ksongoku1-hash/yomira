# Búsqueda global de capítulos guardados — 17 de octubre de 2026

- La ficha busca por número o título en todos los capítulos guardados del manga. El servidor filtra antes de paginar, manteniendo las páginas de 100 resultados y el orden numérico existente. Los contadores y la paginación reflejan las coincidencias.
- Se busca al pulsar Buscar o Enter, sin peticiones por cada letra. Limpiar búsqueda restablece el texto, los resultados y la primera página. Los controles siguen disponibles ante un error o una búsqueda sin coincidencias.
- La coincidencia ignora mayúsculas y minúsculas Unicode. Los signos % y _ se buscan literalmente. El servidor y el cliente limitan el texto a 200 caracteres. La API mantiene su comportamiento anterior cuando no se pide búsqueda.
- El botón de orden muestra Primeros capítulos o Últimos capítulos. El acceso Leer primer capítulo o Leer capítulo más reciente usa la lista inicial sin filtrar, incluso al paginar o buscar; no confunde el primer resultado con el principio del manga. Se reutiliza la caché existente de capítulos sin filtrar.
- Si una página queda vacía tras cambiar los capítulos disponibles, se puede volver a la primera página sin borrar la búsqueda.

Validación: 69 pruebas del servidor y 218 de interfaz aprobadas. Se verificaron la búsqueda fuera de la primera página, Unicode, signos literales, límites, orden, paginación, errores y reintento, limpieza, lectura, selección y exportación. Una colección aislada de 201 capítulos comprueba la segunda página filtrada y el capítulo 201 bajo el límite de trabajo SQL de la prueba existente. Ruff, Pyright de los archivos de aplicación modificados, TypeScript, ESLint y compilación correctos. Quedó omitida 1 prueba de enlaces especiales de Windows porque el entorno no permite crearlos. Las pruebas de interfaz se ejecutaron con dos trabajadores para evitar saturar el equipo.

Activado en E: tras verificar que no había descargas pendientes ni en curso. Progreso y configuración se conservaron tras el reinicio: 34 posiciones guardadas. Revisión visual de la ficha, búsqueda con coincidencias, búsqueda vacía y limpieza; pantalla estrecha sin desbordamiento horizontal. No se abrieron capítulos ni se modificaron descargas reales durante la revisión.

Las fuentes anteriores, la interfaz anterior y una copia privada de recuperación de la base de datos están en el archivo local de Yomira. El ZIP solo incluye los diez archivos de código modificados y estas notas. No contiene datos personales.

No se añadieron dependencias ni migraciones. La búsqueda afecta a los capítulos descargados y verificados; el catálogo online mantiene su buscador existente. No se recompiló el instalador ni se publicó una Release de GitHub.
