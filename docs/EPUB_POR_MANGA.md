# Un EPUB por manga

En la ficha del manga aparece **Tu libro EPUB**. **Crear EPUB** reúne todos sus
capítulos descargados y comprobados, incluso si la lista está filtrada o paginada.
Cuando descargues más capítulos, pulsa **Actualizar EPUB**. **Guardar EPUB**
descarga una copia del libro preparado. La generación sigue siendo manual.

Los libros se guardan en la biblioteca configurada:

```text
EPUB/<identificador de la serie>/<título>.epub
```

Cada serie mantiene su propio libro, para separar ediciones e idiomas. Las
imágenes JPEG y PNG se conservan; WebP se convierte a PNG manteniendo resolución
y píxeles. La conversión puede aumentar el tamaño. Durante una actualización se
necesita espacio para el libro anterior y el nuevo. El anterior se sustituye
solo cuando el archivo temporal está verificado; si falla, sigue disponible.

El EPUB 3 adaptable incluye índice moderno y NCX. Cada capítulo reúne sus imágenes
en un documento para que el lector pueda contar varias páginas. La muestra de
dos capítulos y seis imágenes mostró un contador del 1 al 6 en Bigme B6 Color.
La paginación exacta depende del lector. No se certifican otros modelos ni libros
de 20 GB por esa prueba. El formato admite ZIP64, hasta 64 GiB y 100.000 imágenes.

Los libros anteriores aparecen pendientes de actualizar cuando cambia el
contenido o el formato compatible. Actualiza y vuelve a copiar el archivo al
lector. La app conserva los capítulos originales y el progreso. La carpeta de
salida EPUB se excluye de la importación automática para evitar duplicados.

## Verificación

Se comprueban el orden, integridad, referencias del índice, cancelación, errores
de escritura y conservación del libro anterior. La conversión de WebP verifica
los píxeles de salida. El generador usa el bloqueo y presupuesto compartidos de
almacenamiento y comprueba el espacio libre antes de escribir.

## Actualización y envío (0.1.19)

El EPUB incorpora un registro acotado de conversiones WebP. Al actualizar, el
constructor verifica el original descargado y los píxeles de la conversión previa
antes de reutilizarla. Un recurso corrupto se convierte otra vez. Los libros sin
este registro se procesan normalmente; no hay una segunda caché permanente.

En Windows, «Enviar al lector» usa el historial y la verificación USB existentes.
Solo reemplaza una versión anterior identificada por su SHA-256 en un envío
verificado del mismo manga al mismo destino. La publicación reserva y comprueba
la nueva copia antes de retirar la anterior. Los conflictos con archivos ajenos
se rechazan. Un corte durante la promoción puede dejar una copia `.previous` de
recuperación; no se borra a ciegas. El historial admite EPUB hasta 64 GiB.

La migración 0025 conserva el historial existente y amplía su límite de tamaño;
se rechaza volver a una versión del esquema que no pueda conservar libros grandes.
