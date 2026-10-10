# ADR-0129 — Un archivo por manga en Books

## Estado

Aceptada por instrucción del usuario del 2026-10-08: los mangas enviados al B6 Color deben ir a `Almacenamiento interno compartido/Books`, reunidos en un solo archivo.

## Decisión

- La ficha ofrece la exportación agregada CBR como único flujo de envío. Incluye todos los capítulos descargados y todas las páginas de la lista; un intervalo también produce un único archivo.
- La biblioteca conserva sus CBZ originales. El derivado CBR contiene páginas PNG, según ADR-0121. Los PNG de 8 bits ya verificados conservan sus bytes; PNG de 16 bits, JPEG y WebP se normalizan al perfil RGBA de 8 bits con comprobación de los píxeles decodificados.
- La numeración global de cinco dígitos precede al capítulo y a la página en cada miembro. Así el orden del contenedor coincide con el orden alfabético usado por algunos lectores.
- Se amplía el límite derivado de CBR y de su historial/transporte a 8 GiB, manteniendo 10.000 páginas. Se reservan hasta 8 GiB para staging y 8 GiB para el archivo final. Los límites de las descargas canónicas por capítulo siguen en 2 GiB.
- La migración 0023 conserva el historial de envíos y amplía su restricción de tamaño. Se bloquea su reversión mientras existan registros grandes que no cabrían en el esquema anterior.
- El transporte WPD resuelve o crea únicamente Books bajo el almacenamiento elegido, comprueba que sea una carpeta y rechaza conflictos. Copia, comprueba y publica dentro de ella, conservando la protección contra sobrescritura.
- El temporal WPD se identifica por el asset verificado. Un reintento comprueba y aprovecha una copia completa antes de publicarla; si es parcial, comprueba su prefijo y su permanencia en Books antes de retirarla y copiar desde el principio. Los temporales ajenos o de contenido distinto se conservan.
- El worker transmite avance limitado por etapa y bytes al servidor. El historial lo expone solo durante EXPORTING y la interfaz indica porcentaje de esa etapa, sin confundir la copia con la verificación final. Se reintenta el asset registrado sin crear otro CBR.
- Una unidad FAT32 rechaza antes de copiar cualquier archivo que supere su límite de 4 GiB menos un byte. No se divide el manga ni se declara terminado un envío parcial.

## Verificación

238 pruebas del servidor y 20 de interfaz aprobadas; cuatro pruebas de plataforma omitidas. Se comprobó Books con el controlador real del B6 Color. La activación conservó las nueve series, las quince descargas terminadas y los 210 registros de envío anteriores.

La integridad y la copia al dispositivo se verifican por separado de la lectura en pantalla. El 2026-10-09 el usuario confirmó que Vinland Saga abre y sus páginas se ven bien en el lector nativo del B6 Color. Esta confirmación corresponde a ese archivo y modelo; no certifica otros lectores.

Vinland Saga se publicó en Books como un CBR de 5.213.484.598 bytes, 208 capítulos y 5.453 páginas. Los 208 originales se cotejaron con sus huellas anteriores. El dispositivo se bloqueó al renombrar el temporal; tras reconectarlo se recuperó esa copia y un nuevo envío confirmó su SHA-256 sin duplicarla. No quedó ningún temporal. Los errores nativos 0x802A0006 y 0x8007001F se clasifican ahora como desconexión recuperable.
