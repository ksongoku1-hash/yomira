# Iconos en la configuración del lector — 21 de octubre de 2026

Activos en E:\yomira, http://127.0.0.1:5173/.

Se añadieron iconos a modos de lectura, webtoon continuo, dirección, zoom, encuadre, temas de fondo, brillo, perfil e-reader, visibilidad, atajos, progreso, controles compactos y recuperación de imágenes. También se identifican visualmente las secciones del panel.

Los iconos son SVG integrados en el componente ActionIcon existente. Se conservan los 14 iconos anteriores y se añaden 17 dibujos nuevos, sin dependencias, imágenes remotas ni cambios en las preferencias almacenadas. Los SVG son decorativos (aria-hidden y focusable=false) y los controles conservan sus textos o nombres accesibles. Alejar y acercar incorporan descripción al pasar el cursor.

En móvil, los botones de modo y tema colocan el icono encima del texto. Los controles de zoom se mantienen juntos para evitar que el porcentaje se separe de sus botones. El resto de la lógica del lector no cambió.

Verificación: tipos, revisión estática y compilación correctas; 66 pruebas de ReaderExperience aprobadas. Se revisaron escritorio, panel de 320 píxeles, controles avanzados, temas y los nombres accesibles. El panel no desborda y el zoom conserva sus tres elementos alineados. El módulo verificado en la vista privada coincide con el servido por la instalación activa.

La activación no modificó reader_progress; se conservaron 44 registros de progreso y 896 trabajos. Copia de seguridad, fuentes anteriores, distribución previa y copia coherente del perfil en E:\yomira\archive\reader-icons-20261021.

Alcance: panel de configuración del lector y ampliación compatible del componente compartido de iconos. No se generó instalador ni publicación en GitHub; no se repitió la batería completa del servidor ni se probó hardware e-reader físico.
