# ADR-0122 — Escritor RAR5 propio, store-only, para CBR

## Estado

Aceptada como decisión de arquitectura el 2026-10-04, en cumplimiento de ADR-0121. La implementación no puede comenzar hasta pasar K285 y los gates siguientes. La compatibilidad física con Bigme sigue pendiente de K288.

## Contexto

ADR-0121 y SPEC 1.2 §122 exigen que Yomira produzca un CBR agregado válido sin depender de WinRAR instalado ni alterar los CBZ canónicos. La muestra de referencia es RAR5 sin compresión. La elección de un escritor externo requeriría resolver su redistribución, licencia, packaging y runtime.

SharpCompress declara soporte RAR solo para lectura, así que no puede crear el archivo requerido ([matriz oficial de formatos](https://github.com/adamhathcock/sharpcompress/blob/master/docs/FORMATS.md)). RARLAB prohíbe incluir su instalador de prueba dentro de otro paquete sin permiso escrito y prohíbe usar o invertir sus binarios/código fuente para recrear el algoritmo de compresión ([EULA de RARLAB](https://www.rarlab.com/license.htm)). Su documentación pública de RAR 5.0 describe las cabeceras y define el método 0 como almacenamiento sin compresión ([formato RAR 5.0](https://www.rarlab.com/technote.htm)).

## Decisión

1. El escritor será código original de Yomira que produce el subconjunto mínimo de RAR5 necesario para almacenar las páginas PNG sin comprimir. No se implementará ni se copiará ningún algoritmo de compresión RAR.
2. El escritor usará únicamente bibliotecas estándar del runtime empaquetado para CRC32, lectura secuencial y escritura. No incorporará `rar.exe`, WinRAR, UnRAR, código de UnRAR ni una dependencia de archivado de terceros.
3. El archivo solo contendrá páginas PNG con rutas/nombres deterministas y seguros. No incluirá compresión, cifrado, volúmenes, solid mode, streams alternativos, comentarios, recuperación ni miembros de metadata en esta primera versión.
4. El límite agregado es 2 GiB por CBR y 10.000 páginas. La preparación PNG queda limitada a 2 GiB; el preflight reserva hasta 4 GiB para preparación y archivo final, además de la reserva habitual de recuperación/SQLite. Si no puede reservarse, se rechaza antes de procesar páginas. El límite de 2 GiB coincide con la cota existente de exportación USB.
5. No se distribuirá ningún ejecutable o fuente de RARLAB. El producto no requerirá instalar o licenciar WinRAR para generar CBR. El EULA de RARLAB permite a quienes usan copias licenciadas distribuir archivos que produjeron sin regalías adicionales; esta referencia no se trata como licencia para el escritor ni autoriza a redistribuir software RARLAB.
6. La implementación seguirá únicamente la especificación pública del formato RAR5. No se inspeccionarán ni usarán ejecutables o código fuente RARLAB como base de ingeniería inversa. Si un requerimiento futuro exige compresión RAR, esa función queda fuera de este ADR y exige nueva decisión y permisos.
7. El paquete de producto no necesita un lector RAR externo. K287 verificará el contenedor con un lector independiente disponible solo en el entorno de QA, además del validador interno; estas herramientas no se empaquetan ni son necesarias durante el uso.
8. El contenedor queda “orientado a Bigme B6”, no “compatible”, hasta que K288 demuestre en el dispositivo que RAR5 store-only con PNG abre y conserva páginas y orden.

## Consecuencias

- La generación no requiere conexión a internet, instalación adicional ni licencias de runtime de terceros.
- Los PNG ya están comprimidos; almacenarlos sin compresión evita trabajo y evita implementar un algoritmo RAR.
- Los archivos que excedan 2 GiB o 10.000 páginas deben dividirse en varias exportaciones CBR independientes.
- El escritor implementa un subconjunto deliberadamente pequeño. Debe rechazar entradas y opciones fuera de ese perfil y no anunciar soporte general para RAR.
- La integridad del CBZ fuente se mantiene como invariante; todo CBR se construye en staging y se publica solo tras verificación completa.
- Esta decisión es específica para el perfil CBR de Yomira. No convierte el producto en un archivador RAR general.

## Verificación exigida antes de release

- K285: revisión del diff de dependencias, empaquetado y avisos de terceros; comprobar que el binario final no incluye componentes RARLAB.
- K286: implementación del subconjunto RAR5 store-only y publicación atómica.
- K287: verificación estructural, CRC, extracción independiente y comparación del contenido/píxeles; fault cases sin mutación de los originales.
- K288: prueba física Bigme B6 con la muestra completa de 111 páginas y evidencia de navegación/reapertura.

## Reversión

Deshabilitar la creación CBR con un ADR posterior. No borrar ni modificar CBZ, estados de biblioteca ni progreso de lectura. Eliminar el escritor solo después de confirmar que no quedan tareas CBR recuperables.
