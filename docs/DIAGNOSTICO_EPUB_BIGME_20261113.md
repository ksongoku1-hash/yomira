# Compatibilidad EPUB y contador en Bigme

El generador crea un EPUB por serie con los capítulos descargados. La generación
y actualización son manuales, sin borrar los archivos de lectura ni el progreso.

## Problemas reproducidos

- El diseño fijo y las imágenes WebP dificultaban la compatibilidad. Se añadió
  un formato adaptable, índice NCX y conversión WebP a PNG con verificación de
  resolución y píxeles.
- Forzar el ancho de las imágenes producía páginas iniciales vacías en MuPDF.
  Se quitaron el ancho forzado y los atributos HTML de dimensiones.
- Un XHTML por imagen dejaba el contador del Bigme en 1/1, aunque los dibujos
  sí cambiaban al avanzar. La fotografía del dispositivo permitió distinguir
  ese comportamiento de un libro detenido en su portada.
- El formato portable-images-v4 reúne las imágenes de cada capítulo en un XHTML,
  conserva el orden del índice y separa las imágenes con saltos de página.

## Comprobaciones realizadas

- El usuario confirmó en el Bigme B6 Color que una muestra de dos capítulos
  y seis imágenes permite avanzar y muestra el contador del 1 al 6.
- La muestra tiene tres páginas por capítulo en MuPDF a 600 x 800 y 390 x 844.
- Se generó un libro de 102 capítulos y 4.313 imágenes, validado con EPUBCheck
  5.4.0 sin errores ni advertencias. Su índice tiene 102 destinos y sus imágenes
  mantienen los tamaños y CRC de la versión anterior.
- Se verificó íntegramente la copia del libro de aproximadamente 3,8 GB al
  dispositivo mediante lectura de vuelta y SHA-256 antes de retirar la anterior.
- Se conservó el progreso de la aplicación. Las muestras anteriores se
  retiraron después de verificar sus bytes y guardar una copia de recuperación.
- Pasaron las pruebas de EPUB e importación, Ruff y Pyright.

## Límites

El contador exacto depende del motor y la configuración de paginación. MuPDF
produce 4.302 páginas para las 4.313 imágenes del libro de prueba; la validación
del contenido no debe confundir esas dos cantidades. La confirmación física del
contador se limita a la muestra de seis imágenes. No se certificaron lectores
de otros modelos ni libros de 20 GB. No se modificó el firmware del dispositivo.
Los registros con datos de la instalación personal se conservan fuera del código
público del proyecto.
