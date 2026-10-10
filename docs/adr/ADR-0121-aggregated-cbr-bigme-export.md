# ADR-0121 — Exportación agregada CBR para Bigme B6

## Estado

Aceptada por instrucción directa del usuario el 2026-10-04 y registrada en YOMIRA V1 SPEC 1.2. La decisión normativa queda aprobada; la implementación y la compatibilidad física del lector siguen pendientes.

## Contexto

ADR-0004 y la especificación vigente definen un CBZ por capítulo como unidad canónica de biblioteca. El usuario necesita reunir varios capítulos en un único archivo CBR para abrirlo con el lector nativo de un Bigme B6.

El CBR de muestra anterior contenía 111 páginas WebP de los capítulos 184–188. En la foto del Bigme, el lector mostraba una página vacía con el indicador `1/0`. Se generó otra muestra con las mismas páginas decodificadas y convertidas a PNG, en orden de capítulo y página, dentro de un RAR5 sin compresión. La comparación de dimensiones y hashes de píxeles RGBA confirmó que la conversión preservó todos los píxeles; WinRAR y 7-Zip verificaron el contenedor y su extracción. El nuevo archivo mide 111.786.732 bytes y su SHA-256 es `E9CCF87D11C4782D7409890457DD34772DB7E0306DDF086BA6F8BD8A89CCC1F7`.

El usuario todavía no ha confirmado que esa segunda muestra se abra correctamente en el Bigme. Las pruebas locales verifican integridad y preservación de píxeles; no demuestran que el lector físico acepte RAR5 o PNG.

## Decisión

1. Mantener los CBZ individuales e inmutables por capítulo como formato canónico de la biblioteca. El CBR es un formato derivado de exportación y solo se crea cuando el usuario solicita reunir varios capítulos completos en un único archivo.
2. El perfil inicial Bigme genera una sola entrada PNG por página. La conversión debe preservar exactamente los píxeles decodificados de la entrada, y se verifica por dimensiones y hash del buffer RGBA; no basta con comparar tamaños, nombres o CRC del archivo comprimido.
3. El orden estable es capítulo numérico ascendente y página numérica ascendente dentro de cada capítulo. Los nombres de página conservan ambos identificadores, por ejemplo `Capitulo 184 - 001.png`. El nombre del CBR identifica la serie y el rango de capítulos.
4. La extensión `.cbr` corresponde a un archivo RAR íntegro. La muestra de referencia fue creada en formato RAR5 con almacenamiento sin compresión. RAR5 todavía no se declara compatible con Bigme: una prueba física de lectura debe confirmar la versión antes de fijarla como requisito de perfil.
5. El archivo de compatibilidad inicial contiene solo páginas PNG. Añadir ComicInfo u otros miembros requiere prueba física para comprobar que el lector nativo los ignora de forma segura.
6. La construcción usa staging acotado, preflight de espacio, validación completa de miembros, decodificación, recuento, orden, hashes de píxeles e integridad del archivo antes de una publicación atómica. Ante cualquier error no se publica el CBR y se conservan íntegros todos los CBZ de origen.
7. Antes de implementar, se debe incorporar una tarea y sus quality gates al plan maestro, y aceptar otro ADR que defina el escritor RAR, su licencia y su forma de distribución. No se puede depender de que WinRAR esté instalado en la máquina del usuario.

## Consecuencias

- La biblioteca mantiene una copia verificable por capítulo, mientras que el usuario puede producir un único archivo portátil para un rango elegido.
- PNG puede consumir más espacio que WebP; el sistema debe reservar el espacio previsto antes de construir el CBR y fallar sin alterar los CBZ originales si no alcanza.
- La exportación no cambia base de datos, progreso de lectura ni ubicación de los CBZ canónicos.
- El perfil se describe como «orientado a Bigme B6» hasta completar y registrar la prueba física.

## Verificación

- En la muestra, 111 páginas quedaron en orden y pudieron extraerse; cada PNG se decodificó y coincidió con la entrada WebP en dimensiones y hash de píxeles RGBA.
- La integridad del archivo y su extracción pasaron WinRAR y 7-Zip.
- El archivo de muestra tiene 111.786.732 bytes y SHA-256 `E9CCF87D11C4782D7409890457DD34772DB7E0306DDF086BA6F8BD8A89CCC1F7`.
- **Pendiente:** abrir la muestra actualizada en el Bigme B6 y confirmar que el lector presenta las 111 páginas. Hasta entonces, no se considera superada la compatibilidad física.

## Estrategia de reversión

Retirar el modo de exportación CBR mediante un ADR posterior. No borrar ni reescribir CBZ canónicos durante una reversión; conservarlos y retirar solamente la función de crear nuevos paquetes agregados.
