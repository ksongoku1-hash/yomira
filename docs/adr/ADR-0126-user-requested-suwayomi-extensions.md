# ADR-0126: Extensiones Suwayomi solicitadas por el usuario

## Estado

Accepted — fuentes seleccionadas por las solicitudes previas del usuario para integrar MangaDex, NovelCool y ManhwaWeb.

## Contexto

ADR-0125 prepara el servidor local y configura el catálogo comunitario Keiyoushi, pero no instala extensiones automáticamente. Sin extensiones instaladas, el puente de Yomira responde sin error pero no tiene fuentes de manga para consultar.

Suwayomi instala extensiones como módulos Java ejecutables dentro de su proceso. No hay sandbox del sistema operativo que las aísle. El usuario ya pidió incorporar MangaDex, NovelCool y ManhwaWeb al conjunto de fuentes de Yomira.

## Decisión

1. Instalar solamente estas extensiones presentes en el catálogo Keiyoushi: `eu.kanade.tachiyomi.extension.all.mangadex`, `eu.kanade.tachiyomi.extension.all.novelcool` y `eu.kanade.tachiyomi.extension.es.manhwaweb`.
2. Usar la operación de instalación propia de Suwayomi desde el índice configurado; no cargar APK/JAR de URLs manuales ni añadir repositorios adicionales.
3. No instalar la variante comunitaria de ZonaTMO porque Yomira ya tiene un conector directo, ni instalar MangaXD/LectorXD si no están publicados en este catálogo.
4. Mantener el servicio enlazado a `127.0.0.1:4567`; no exponer la API ni las extensiones a la LAN.
5. La instalación no descarga capítulos. Las búsquedas de prueba usarán títulos ya indicados por el usuario y comprobarán solo la respuesta de catálogo.

## Consecuencias

- Las búsquedas de Yomira podrán consultar las fuentes que aporten esos tres módulos; cada búsqueda depende de que el sitio de origen esté disponible.
- Los módulos se ejecutan con los permisos de la cuenta local dentro del proceso separado de Suwayomi.
- Las extensiones no seleccionadas permanecen sin instalar.

## Verificación

- Confirmar que Suwayomi marca como instaladas las tres extensiones seleccionadas y las expone como fuentes.
- Ejecutar búsquedas sin crear trabajos ni descargar capítulos.
- Confirmar que Yomira recibe resultados o un error de origen trazable, nunca un fallo de conexión al servicio local.
