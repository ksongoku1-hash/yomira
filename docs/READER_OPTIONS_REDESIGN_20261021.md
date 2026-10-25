# Panel de configuración de lectura — 21 de octubre de 2026

Se integró el diseño del HTML «Configuración de lectura (rediseño)» en el panel real `#reader-options` de Yomira.

- Panel de hasta 400 px, colores azul oscuro/cian, tarjetas con iconos y vista previa ilustrativa que refleja modo, dirección, zoom, fondo y brillo.
- Cuatro opciones de lectura: página única, vertical con separación, doble página y webtoon continuo. Webtoon activa el modo vertical sin espacios y reutiliza la navegación y conservación de posición existentes.
- Interruptores nativos accesibles, dirección RTL/LTR explícita, deslizador de zoom, encuadre, muestras de tema y acordeón para los controles avanzados.
- Todos los controles están conectados a las preferencias reales. Se mantienen zoom 50–300 %, brillo 50–100 %, atajos H/C, recuperación de imágenes, barra de progreso y controles compactos. Las teclas M/Tab del prototipo no se añadieron; Tab conserva la navegación accesible por teclado.
- Se preservan el perfil e-reader sin filtros y sus ajustes originales, los avisos de guardado y el cierre con Escape que devuelve el foco al botón de opciones.

Validación: tipos, análisis estático y compilación correctos; 100 pruebas aprobadas (68 de experiencia y 32 del lector, incluida la navegación de 2.000 páginas). Revisión en navegador a 320 px, 390 px y escritorio: sin desbordamiento horizontal del panel, controles operativos, preferencias restauradas al recargar y encabezado visible durante el desplazamiento. La vista previa usa figuras CSS locales y no solicita páginas adicionales.

Activado en `E:\yomira`, servido en `http://127.0.0.1:5173/`. Se conservaron los recursos anteriores para las pestañas abiertas. La activación comprobó que las 44 entradas de progreso personal permanecen iguales y que se conservan los 896 registros de descargas. Las interacciones de prueba se realizaron en un perfil temporal separado.

La copia de seguridad de los cuatro archivos modificados, la interfaz anterior y el perfil previo está en `E:\yomira\archive\reader-options-redesign-20261021`. Las capturas corresponden al perfil de pruebas con la misma compilación activada.

No se generó instalador ni se publicó una actualización en GitHub. La carga online y la caché quedan fuera de este cambio visual.
