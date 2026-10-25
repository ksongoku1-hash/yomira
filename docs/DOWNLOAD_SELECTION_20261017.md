# Selección de descargas en la ficha y el lector — 17 de octubre de 2026

- La selección muestra el total de capítulos pendientes elegidos y cuántos quedan fuera de la página visible, incluidos los ocultos por una búsqueda. Se conserva al cambiar de página, buscar o cerrar y volver a abrir el panel durante la misma visita.
- Seleccionar pendientes visibles marca solo los capítulos que aparecen en la página actual y todavía no están guardados ni en cola. Cuando todos están marcados, Desmarcar visibles retira únicamente esas casillas y conserva el resto. Limpiar selección borra la selección completa sin iniciar descargas.
- Descargar pendientes del manga muestra la cantidad exacta y explica que incluye todas las páginas, aunque haya una búsqueda activa. En la ficha está dentro de un desplegable; el lector conserva Más opciones. Se usa la misma cola y los lotes existentes de 100 capítulos.
- Descargar un capítulo suelto en el lector desmarca únicamente los capítulos enviados correctamente; las otras casillas permanecen elegidas. Si falla un envío parcial, la actualización de estados excluye de la selección efectiva lo que ya está guardado o en cola; el reintento solo envía lo que sigue pendiente. No hay reintentos automáticos.
- Los capítulos que pasan a guardados o en cola dejan de aparecer marcados. Los botones de descarga siguen bloqueados si no se ha podido comprobar el estado. Las comprobaciones de pertenencia usan conjuntos para evitar recorrer toda la selección por cada capítulo.
- La lista sigue limitada a 100 filas visibles. El resumen nuevo mantiene el contraste del perfil de tinta electrónica.

Validación: 224 pruebas de interfaz, lector, ficha, API y aplicación aprobadas. Incluyen selecciones entre páginas y filtros, marcar/desmarcar, capítulos guardados y en cola, descarga individual, error parcial y reintento, desplegable de descarga completa y un catálogo de 10.000 capítulos sin ampliar las 100 filas. TypeScript, ESLint y compilación correctos. No se modificó el servidor.

Activado en E: tras comprobar que no había descargas pendientes ni en curso. Se conservaron 34 posiciones de lectura y los ajustes. La revisión visual de la ficha comprobó selección, búsqueda, resumen fuera de la página, desplegable y limpieza, además de una pantalla estrecha sin desbordamiento horizontal. No se descargaron capítulos reales ni se abrieron capítulos en el lector durante esa revisión; sus envíos y navegación se comprobaron con datos aislados.

Las fuentes y la interfaz anteriores, junto con una copia privada de recuperación de la base de datos, están guardadas en el archivo local de Yomira. El ZIP solo contiene los seis archivos modificados y estas notas, sin datos personales.

No se añadieron dependencias ni migraciones. No se recompiló el instalador ni se publicó una Release de GitHub.
