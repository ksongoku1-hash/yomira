# ADR-0041 — Generación y cache de miniaturas

## Status

Accepted — decisión de implementación de K052, 2026-09-26.

## Context

La sección 34 de la especificación requiere un `ThumbnailService`, variantes `small`, `medium` y `large`, y cache por hash. No fija dimensiones, formato derivado ni política de recorte. ADR-0009 protege las imágenes originales de transformaciones irreversibles, y ADR-0002 ya incluye Pillow en el stack aprobado.

## Decision

- Definir las variantes como límites máximos, no como dimensiones forzadas: `small` 160×240, `medium` 320×480 y `large` 640×960 píxeles.
- Conservar la proporción completa con remuestreo LANCZOS; nunca recortar ni ampliar. Una imagen menor que el límite se conserva en sus dimensiones de origen.
- Emitir WebP sin pérdida (`lossless=true`, `quality=80`, `method=2`, `exact=true`) para conservar color y alfa con un encoder de coste acotado. El resultado omite EXIF y conserva el perfil ICC si Pillow lo expone, hasta 1 MiB; un perfil mayor invalida el derivado. El Asset original no se modifica ni se sobrescribe.
- El servicio recibe una imagen ya validada, decodificada, orientada y normalizada a RGB o RGBA por el pipeline de validación. Trabaja sobre el objeto en memoria, no abre rutas y no modifica su posición ni sus píxeles. Para una fuente animada, la llamada usa el fotograma 0.
- El cache de proceso es LRU, limitado a 32 MiB y 64 entradas por instancia. Una entrada individual mayor que el presupuesto no se guarda. La identidad deriva del SHA-256 de la fuente, variante, límites, parámetros de codificación, versión de algoritmo y versión de Pillow; un nuevo hash de portada o una nueva generación obtiene otra clave.
- El resultado incluye el WebP, su SHA-256, dimensiones, MIME y clave de cache. No persiste archivos: la capa de filesystem podrá almacenar el derivado usando esa identidad sin introducir rutas en el procesador.
- Una imagen fuera del contrato, o un fallo de resize/encode, produce `IMG_THUMBNAIL_FAILED`; se conserva el original y no se presenta la miniatura fallida como válida. La llamada es síncrona y debe ejecutarse fuera del event loop; cada instancia serializa la generación para evitar codificaciones simultáneas sin límite.
- Fijar Pillow 12.3.0 en `requirements.in` y `requirements.lock`. Esta versión publica wheel Windows x64 para CPython 3.14 y declara licencia MIT-CMU; ADR-0002 ya aprueba Pillow para validación y procesamiento de imágenes.

## Alternatives Considered

- Recortar al aspecto 2:3: podría eliminar texto o ilustración en portadas panorámicas; se conserva el cuadro completo.
- JPEG o WebP con pérdida: reducen tamaño, pero degradan texto fino y pueden destruir alfa.
- Guardar PNG: es sin pérdida, pero puede ocupar mucho más que WebP para portadas fotográficas.
- Reescribir la portada original con la variante pequeña: impide recuperación exacta; se mantiene el original como Asset separado.
- Cache sin límite o escrituras directas por ruta: consumo impredecible y dependencia del filesystem dentro del procesador.

## Positive Consequences

- Las tres variantes tienen límites claros de píxeles y no aumentan la imagen fuente.
- El cache tiene techo explícito de memoria y se invalida al cambiar el hash de la portada o los parámetros del generador.
- La transformación es regenerable y no pone en riesgo el Asset original.

## Negative Consequences

- WebP sin pérdida puede ocupar más espacio que un formato con pérdida.
- El cache de proceso se vacía al cerrar la aplicación; la persistencia y el presupuesto de disco se conectarán desde la capa de filesystem.
- El tamaño de cada variante puede requerir ajuste si las dimensiones reales de la UI lo justifican; cualquier cambio altera la identidad del cache.

## Failure Modes

- El input no fue validado, no está orientado o no se normalizó a RGB/RGBA antes de llegar al servicio.
- Pillow o libwebp falla durante el remuestreo o la codificación.
- Se omite actualizar la versión de generación cuando cambian algoritmos o parámetros.
- Una nueva interfaz escribe una miniatura sobre el archivo fuente en vez de almacenar un Asset derivado.

## Reversal Strategy

Deshabilitar el uso de miniaturas y continuar sirviendo la portada original. Cambiar dimensiones, modo de recorte, formato o calidad requiere actualizar este ADR, incrementar la identidad de generación y verificar que las portadas originales permanecen intactas.

## References

- [Especificación Yomira V1, sección 34](../YOMIRA_V1_SPEC.md#34-thumbnail-pipeline)
- [ADR-0002 — FastAPI + Python](ADR-0002-fastapi-python.md)
- [ADR-0009 — Procesamiento SAFE de imágenes](ADR-0009-safe-image-processing.md)
- [Pillow 12.3.0 en PyPI](https://pypi.org/project/pillow/12.3.0/)
- [Pillow — opciones de escritura WebP](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#webp)
- [Pillow — Image.resize](https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.resize)
