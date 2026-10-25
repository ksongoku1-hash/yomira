# Corrección de tiras estrechas en página única y doble — 18 de octubre de 2026

Activa en http://127.0.0.1:5173/, fuente y compilación en E:\yomira.

La corrección anterior aplicaba un ancho legible solo al ajuste automático. Un ajuste al alto guardado conservaba prioridad y encogía las páginas largas como en la captura del usuario. Ahora fittedPageWidth mantiene el ancho disponible para relaciones ancho/alto menores de 0,5, incluso con ajuste al alto, en página única y doble. Las páginas normales siguen admitiendo el ajuste completo al alto. No se borran preferencias. El panel explica este comportamiento de tiras largas.

80 pruebas aprobadas: ReaderExperience, readerPresentation y readerPreferences. Se verificó altura seleccionada y guardada, ambos modos y sus cambios. Tipos, estilo y compilación aprobados.

Comprobación visual privada en The Boxer 21, páginas 5 y 6. Página única: 1241 píxeles de ancho; doble: 619,5 por imagen. Imagen 5: 700 × 19230 píxeles, imagen 6: 700 × 18826. Se mantuvo el resultado tras recargar con Ajustar al alto guardado; captura double-fixed.png. El perfil y las preferencias utilizados para esta comprobación fueron privados.

La activación no reinició el servicio ni escribió progreso. Conservó 40 registros de progreso y 896 trabajos existentes. Archivos originales, copia consistente del perfil, interfaz anterior y parche en:
E:\yomira\archive\reader-fit-guard-20261018-202307

El cambio está aplicado a la instalación local; el instalador de GitHub sigue pendiente de actualización. Basta recargar la pestaña para recibir la interfaz corregida.
