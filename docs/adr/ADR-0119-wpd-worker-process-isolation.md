# ADR-0119: Aislar COM WPD/MTP en un proceso auxiliar

## Estado

Aceptada para contener fallos nativos COM y mantener disponible la API. La estabilidad física de WPD/MTP y K206 siguen pendientes hasta probar una exportación en un lector compatible.

## Contexto

La Alpha Windows registró una excepción nativa 0xC0000005 en _ctypes.pyd durante la ruta de exportación WPD. No hay minidump que señale la llamada COM concreta, y el destino no estaba conectado durante la última validación; por ello no es posible afirmar que una corrección puntual del contrato COM resuelva el cierre.

Si el proceso principal muere dentro de COM, desaparecen también la API loopback y la página del lector local, aunque la biblioteca local permanezca intacta. Los fallos al exportar necesitan continuar asociados a su traza y conservar el origen y cualquier objeto .part remoto de resultado ambiguo.

## Decisión

1. Ejecutar cada enumeración y exportación WPD/MTP en un proceso auxiliar de una sola solicitud. El proceso principal sigue siendo dueño de SQLite, historial de exportación y lifecycle de FastAPI.
2. Comunicar ambos procesos con DTO JSON por stdin/stdout, codificados explícitamente en UTF-8. Limitar la solicitud a 64 KiB y la respuesta a 2 MiB. No serializar proxies COM, credenciales ni contenido CBZ.
3. En el ejecutable empaquetado, despachar Yomira.exe --wpd-worker antes de inicializar FastAPI o adquirir el mutex de instancia. En desarrollo, usar python -m app.services.wpd_worker.
4. Limitar enumeración de destinos a 120 segundos y exportación a 1 hora. Un timeout de enumeración es reintentable porque no muta el destino. Un timeout, salida no cero o respuesta inválida durante una exportación es terminal y no reintentable porque la mutación remota puede ser ambigua.
5. Traducir terminación nativa a un fallo EXP_ sanitizado, con etapa, trace_id, tipo de causa y estado de reintento. Descartar stderr del auxiliar; nunca incorporarlo a logs de aplicación.
6. Mantener sin cambios el protocolo de staging y readback WPD de ADR-0113. No borrar un .part remoto después de crash/timeout, no alterar el CBZ local y no registrar la exportación como exitosa sin confirmación del auxiliar.

## Consecuencias

- Un acceso inválido del backend WPD termina el auxiliar, no la API ni el lector local.
- La petición de exportación en curso falla de forma visible y trazable; no se reintenta automáticamente cuando el resultado remoto puede ser ambiguo.
- Enumerar destinos deja de ser bloqueante indefinidamente y su timeout puede repetirse de forma segura.
- El inicio de cada operación WPD incurre en el coste de crear un proceso. La solución prioriza contención e integridad frente a latencia.
- El proceso auxiliar no corrige la causa interna del acceso inválido. Puede quedar un objeto temporal en el dispositivo y requiere reconciliación explícita.
- Los tests de proceso simulado no sustituyen la exportación física, el gate de desconexión ni K206.

## Verificación

- Antes del arreglo, las regresiones nuevas demostraron que faltaba UTF-8 explícito, que fallos de enumeración se reportaban como EXP_COPY_FAILED y que su timeout se marcaba como no reintentable.
- Después del arreglo, la suite enfocada de WPD, USB, historial, lifecycle y factory pasó 73 pruebas.
- Ruff y Pyright estricto pasaron para el worker y sus pruebas.
- La validación de empaquetado de --wpd-worker y la prueba física se registrarán al completar la candidata y conectar un lector compatible.
