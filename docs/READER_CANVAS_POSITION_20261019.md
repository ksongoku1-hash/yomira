# Reanudación dentro de páginas largas — 19 de octubre de 2026

Activa en E:\yomira y http://127.0.0.1:5173/. La instalación principal estaba apagada al continuar el trabajo y se inició con la tarea existente Yomira Local. La activación de la interfaz no necesitó reiniciar el servicio.

## Comportamiento

Página única y doble guardan el desplazamiento vertical y horizontal dentro de la página o del par visible. Cada modo conserva su propia posición. La restauración espera a que las imágenes visibles estén cargadas y tengan dimensiones reales; en doble espera a ambas, también al reutilizar imágenes de caché.

La posición se expresa como fracción del tamaño del contenido para conservar el punto al variar ancho o zoom. Los desplazamientos programados durante la restauración no sobrescriben la posición guardada. El desplazamiento con teclado, rueda, gestos y barra lateral se recoge mediante el evento de scroll; volver al inicio también actualiza el guardado.

Se guarda con un retraso de 400 ms para evitar escrituras continuas y se vacía el guardado pendiente al ocultar, abandonar o cerrar la página. Al cambiar de capítulo o modo se guarda el punto anterior con su identidad original. Solo se aplica una posición si coincide con la página confirmada por el progreso del servicio. Restaurar una posición interna no escribe progreso del capítulo.

El modo vertical mantiene sus registros existentes. Se reutiliza el almacenamiento del navegador yomira.reader.vertical-positions.v1; los registros paginados añaden el modo a la clave. La colección conserva hasta cincuenta posiciones recientes. El número de página sigue guardado en el perfil de Yomira en E; la posición interna es propia de este navegador y origen, sin sincronización entre equipos.

## Verificación

452 casos únicos de interfaz aprobados. La suite completa pasó 452 casos; tras simplificar la captura de scroll se volvieron a ejecutar los 86 casos del lector, presentación y preferencias. frontend-passed.xml reúne los resultados finales de cada módulo. Tipos, estilo y compilación aprobados.

Comprobaciones añadidas: cierre antes del guardado diferido, desplazamiento horizontal, regreso a cero, cambio de ancho, espera de las dos imágenes, imágenes ya decodificadas en caché, posiciones independientes y rechazo de un punto que corresponde a otra página guardada. Se mantienen los casos de lectura vertical anteriores.

Prueba real con perfil privado, The Boxer 21, página 5:

- Página única antes de recargar: 1260 píxeles; después: 1260.
- Doble página antes de cerrar la pestaña: 1890 píxeles; pestaña nueva: 1890.
- Al regresar a página única: 1260 píxeles, recuperando su posición independiente.

La compilación activada coincide con el HTML comprobado visualmente. Captura: reopened-double.png.

## Respaldo y límites

Se conservaron 44 registros de progreso y 896 trabajos existentes. La activación no escribió progreso personal.
Archivos originales, perfil consistente, interfaz anterior, parche, resultados y captura:
E:\yomira\archive\reader-canvas-position-20261018-215151

La mejora está en la instalación local; el instalador de GitHub sigue pendiente. La posición interna depende del almacenamiento del navegador. La recuperación del número de página en el perfil de Yomira no depende de ese almacenamiento local.
