# ADR-0063 — HTML servido como imagen y JPEG corrupto

## Status

Accepted for K076 implementation — 2026-09-26.

## Context

Las fuentes pueden responder 200 a una petición de página y entregar HTML de login/bloqueo con un tipo de imagen, o bytes JPEG dañados. La validación futura debe comprobar los bytes reales y no confiar únicamente en el status o el MIME. Estos casos deben seguir aislados del PNG normal.

## Decision

- `HTML_AS_IMAGE` sirve HTML estático con HTTP 200 y `Content-Type: image/jpeg` solo para una ruta de página.
- `CORRUPT_JPEG` sirve un prefijo con firma JPEG pero estructura deliberadamente truncada, también con HTTP 200 y `Content-Type: image/jpeg`.
- Ambos bodies son constantes sintéticas, no contienen páginas de usuario ni respuestas remotas copiadas. `Content-Length` refleja el body enviado en esos escenarios.
- Los tests consumen la respuesta HTTP y piden a Pillow cargarla; cada body debe fallar decodificación con un error de imagen. Aceptar ambos como una imagen válida es fallo del test.
- Metadata, búsqueda y `/health` siguen usando sus responses normales. Los escenarios se eligen al construir una instancia distinta.

## Alternatives Considered

- **Devolver HTTP 4xx en lugar de una imagen falsa:** no prueba que un validador rechace 200 con payload que contradice MIME.
- **Servir HTML con `Content-Type: text/html`:** no ejercita el caso en que el servidor o proxy etiqueta el contenido como imagen.
- **Usar una foto real dañada de una fuente:** agrega contenido ajeno, tamaño innecesario y dependencia de Internet.
- **Corromper el fixture normal compartido:** volvería no deterministas todos los tests de descarga correctos.

## Consequences

- Un futuro Download Engine puede probar status/MIME/firma/decodificación como validaciones separadas.
- El test solo verifica que Pillow rechaza estos bytes; la política de clasificación y los `IMG_*` definitivos pertenecen a Fase 10.
- El servidor no almacena imágenes ni escribe el body en disco.

## Failure Modes

- HTML erróneamente aceptado como JPEG: test de carga falla.
- JPEG truncado decodifica sin error: fixture dejó de ser corrupto y el test falla.
- Modo inválido altera el escenario normal: test de PNG normal falla.

## Reversal Strategy

Cambiar MIME, status, bytes o alcance de ruta requiere actualizar K076 y la futura suite de validación. Los bytes corruptos deben seguir siendo sintéticos y deterministas.
