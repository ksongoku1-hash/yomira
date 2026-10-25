# Mejoras del lector — 16 de octubre de 2026

- Precarga real de las dos páginas siguientes (o el límite elegido) en página única y doble, respetando el presupuesto existente. El perfil e-reader conserva su precarga reducida.
- Se espera la posición guardada antes de solicitar la primera ventana de imágenes. No se carga el capítulo completo. Los fallos de precarga no ocultan la página actual ni guardan como leída una página online que no se ha mostrado.
- Ajustes agrupados en Lectura, Dirección y encuadre, Comodidad visual y Controles; encabezado accesible al desplazar el panel. El contenido del panel se retira mientras está cerrado para reducir trabajo de navegación.
- Fondo oscuro, sepia y claro; brillo de páginas entre 50 y 100 %, con preferencias locales validadas. No se modifican imágenes ni CBZ. El perfil e-reader usa blanco y ningún filtro.
- Salto de página editable, validación de límites, foco al abrir y regresar; Escape cierra primero el panel sin salir del modo inmersivo. Miniaturas con espacio reservado para evitar saltos del botón Ir y ajustadas a pantallas estrechas.

Validación: 74 pruebas de experiencia, preferencias, caché, presentación y descargas aprobadas; 32 casos de regresión cubiertos, incluidos recorridos de 100, 500 y 2.000 páginas. Un caso se repitió después de fijar precarga cero en su fixture de cancelación, ya que su propósito es aislar una solicitud antigua. Tipos, ESLint y compilación de producción correctos. Revisión visual a 320 y 390 píxeles, salto con botón y teclado y perfil e-reader sin filtros.

Activado en la copia local desde E:. Historial real y ajustes sin cambios durante la activación. Copia de recuperación: archive/reader-polish-20261016-200824.

No se ha recompilado el instalador 0.1.14 ni publicado una Release con estas mejoras. La primera imagen online y los fallos de la fuente siguen dependiendo del proveedor.
