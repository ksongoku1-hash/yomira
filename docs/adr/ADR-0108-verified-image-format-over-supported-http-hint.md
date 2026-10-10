# ADR-0108 — Formato de imagen comprobado ante MIME HTTP incorrecto

- Estado: ACCEPTED para resolver el fallo observado, bajo la autorización del usuario: «solucionalo aunque tengas que cambiar el plan principal».
- Fecha: 2026-10-01 (America/Bogota).
- Autoridad: SPEC §§27–29; ADR-0083, ADR-0089, ADR-0105, ADR-0106 y ADR-0107.

## Reproducción y causa raíz

El primer reintento real de Naruto 700 (`ee220772-0205-41fa-b0f1-79813f4b5e40`) obtuvo las 22 páginas, sin HTTP 403. El worker validó 17 WEBP y falló al comprobar la página 18 con `IMG_MIME_MISMATCH`. La inspección de los archivos preservados identificó PNG en las páginas 18–22; tamaño y SHA-256 coinciden con la evidencia de descarga. Todas tienen dimensiones 1800×2400. La comparación obligatoria entre MIME HTTP y firma del archivo impedía completar el capítulo con estos tipos permitidos.

SPEC §27 exige comprobar MIME, magic bytes, formato permitido, decodificación, dimensiones, límites y hash; no declara que una cabecera HTTP incorrecta deba prevalecer sobre el formato comprobado. Esta decisión sustituye el rechazo estricto anterior de una discrepancia entre dos tipos admitidos. No basta que el archivo tenga una extensión de imagen ni que el servidor responda 200.

## Decisión

1. El Content-Type debe seguir siendo exactamente uno de los MIME admitidos tras su normalización: `image/jpeg`, `image/png` o `image/webp`. Cabeceras ausentes, múltiples, malformadas, HTML, SVG, AVIF u otros formatos mantienen el rechazo vigente.
2. La firma debe identificar JPEG, PNG o WEBP y coincidir con el formato identificado por el decodificador. Se mantienen verificación del contenedor, decodificación completa de los frames, límites de bytes/dimensiones/píxeles, rechazo de truncados y comprobaciones de identidad/tamaño/SHA-256 antes y después.
3. Si cabecera y formato real son tipos permitidos distintos, se acepta únicamente el contenido que supera todas esas comprobaciones. El MIME devuelto es el del formato real. El worker ya usa ese MIME para extensión, manifest, persistencia, CBZ y lector. No se transforma ni recodifica la imagen.
4. La discrepancia queda registrada como warning `IMG_MIME_MISMATCH`, etapa `mime_reconciled`, con job_id y trace_id. El motivo contiene solo los dos MIME normalizados del catálogo. No se registran URLs, query strings, cookies, rutas ni cuerpos de respuesta. Se emite después de comprobar y cerrar el archivo.
5. La regla vive en el validador compartido. Las portadas reciben las mismas comprobaciones y metadatos reales. Los orígenes de red, retries HTTP, sesión y presupuestos permanecen como están definidos en los ADR vigentes.

## Verificación y plan

Prueba que falle con PNG anunciado WEBP; comprobación de metadata y bytes inalterados para discrepancias admitidas; regresión de MIME no permitido, archivo inválido/truncado, bombs, límites, hashes e identidad; worker completo hasta CBZ/lector y lectura tras reinicio; regresión de portadas. Ruff/Pyright y una nueva candidata empaquetada deben preceder el reintento real. El resultado se añade al informe de ADR-0107; no se renumeran K001–K284 ni se aprueba el Release V1 por este ajuste.
