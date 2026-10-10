# ADR-0125: Runtime local de Suwayomi para las fuentes agregadas

## Estado

Accepted — ejecución local autorizada por la solicitud de continuar la integración de fuentes Suwayomi.

## Contexto

El conector `yomira.suwayomi` ya forma parte de Yomira y solo consulta `http://127.0.0.1:4567`. La Alpha no administra el ciclo de vida de Suwayomi. En el entorno probado no había un servidor en ese puerto, por lo que las búsquedas terminaban en `SRC_CONNECTOR_FAILED` aunque Yomira estuviera disponible.

Suwayomi no incluye fuentes por defecto. Sus extensiones se ejecutan dentro del proceso de Suwayomi y no están aisladas por un sandbox del sistema operativo.

## Decisión

1. Preparar Suwayomi Server oficial x64 como runtime local separado, fuera del ejecutable de Yomira y en una carpeta propia bajo `%LOCALAPPDATA%\Yomira`.
2. Fijar la versión inicial a `v2.4.2366` y comprobar SHA-256 antes de extraerla.
3. Usar un directorio de datos independiente, desactivar la interfaz web duplicada y enlazar exclusivamente a `127.0.0.1:4567`. No abrir el servicio a la LAN ni cambiar la allowlist del conector.
4. Configurar el catálogo Keiyoushi (`https://github.com/keiyoushi/extensions/raw/repo/index.pb`) como repositorio comunitario de extensiones para que el servidor pueda ofrecer fuentes instalables.
5. No instalar extensiones automáticamente. Cada extensión es código de terceros ejecutado por Suwayomi; solo las fuentes elegidas por el usuario se instalarán.
6. Mantener en Yomira la propiedad de la biblioteca, las descargas, la validación de páginas, los CBZ y las exportaciones.
7. No modificar la base de datos, las descargas, la configuración de red ni los archivos del usuario de Yomira al preparar el runtime externo.

## Consecuencias

- Cuando Suwayomi esté iniciado, la consulta al conector deja de fallar por conexión rechazada; si no hay extensiones instaladas, la búsqueda devuelve cero resultados.
- Se necesita instalar al menos una extensión desde el catálogo para obtener resultados de esa fuente agregada.
- Yomira no inicia ni actualiza Suwayomi automáticamente en este incremento. Un reinicio de Windows requiere volver a iniciar el servidor.
- La actualización o integración permanente del ciclo de vida del runtime requiere una ADR posterior.

## Verificación

- Comprobar digest SHA-256 del paquete oficial antes de extraer.
- Confirmar que Suwayomi responde en `127.0.0.1:4567` y que no escucha en interfaces LAN.
- Confirmar que la búsqueda de Yomira ya no presenta error de conexión cuando el servidor está activo.
- Confirmar que no se instaló ninguna extensión automáticamente.

## Referencias

- [Suwayomi Server: README y paquetes Windows](https://github.com/Suwayomi/Suwayomi-Server)
- [Suwayomi Server: guía de extensiones](https://github.com/Suwayomi/Suwayomi-Server/wiki/Getting-Extensions)
- [Keiyoushi: catálogo comunitario](https://github.com/keiyoushi/extensions)
