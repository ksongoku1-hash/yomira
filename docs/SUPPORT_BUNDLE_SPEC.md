# Especificación del Support Bundle

**Estado:** APPROVED — K004 aprobado formalmente por el usuario el 2026-09-25.

## 1. Propósito

Crear un paquete local de diagnóstico que explique versión, salud y fallos recientes sin enviar contenido de la biblioteca ni secretos. La generación prevista corresponde a `POST /api/v1/diagnostics/bundle`.

## 2. Contenido permitido

- versión de app, API y schema;
- Windows y resumen de hardware;
- versiones de dependencias y connectors;
- configuración sanitizada;
- logs recientes sanitizados;
- schema de DB, sin datos de biblioteca;
- errores recientes con códigos y trazas;
- resumen de benchmark.

## 3. Contenido prohibido

- páginas de manga/manhwa o imágenes descargadas;
- CBZ y copias de exportación;
- cookies, tokens, passwords, PIN o credenciales;
- el directorio `data_root/secrets`, sus blobs DPAPI cifrados y referencias opacas `yomira-secret:v1:<uuid>`;
- contenido completo de HTML/JSON remoto;
- secrets en settings;
- datos privados no necesarios para explicar el error.

## 4. Sanitización

Antes de empaquetar:

1. Remover valores secretos conocidos y redactar headers/cookies.
2. Eliminar o reducir rutas que contengan el nombre de usuario.
3. Quitar query params sensibles de URL y reducir IP salvo necesidad diagnóstica justificada.
4. Excluir payloads binarios, contenido de página, CBZ y filas de metadata personal.
5. Revisar errores anidados para que no reintroduzcan secretos en `message` o traceback.
6. Incluir manifest del bundle con versión y lista de archivos incluidos; no incluir una copia de DB.

La sanitización se aplica antes de crear el paquete, no como paso posterior. Si no puede garantizarse, la generación falla cerrada con código diagnóstico y no exporta un bundle parcial.

## 5. Manejo y entrega

- Generación local y explícita por el usuario.
- No se envía automáticamente a un servicio remoto.
- Mostrar ubicación y resultado de creación; el usuario decide si comparte el archivo.
- Escribir primero a un temporal; verificar estructura y luego publicar el bundle completo.
- Aplicar límite de recursos/espacio configurado; tamaño máximo concreto se decidirá antes de implementación.

## 6. Escenario de aceptación documental: timeout de capítulo

| Etapa | Resultado esperado |
|---|---|
| Connector/HTTP recibe timeout | Emite `DL_TIMEOUT` (o `NET_HOST_UNREACHABLE` si esa es la causa clasificada), con `trace_id`, `job_id`, `chapter_id`, `source_id` y etapa |
| Política decide reintento | Registra retryability, intento y espera 1s/3s/8s cuando aplique; no registra cookies ni URL con query sensible |
| Reintentos se agotan | Job termina FAILED o INTERRUPTED según si hubo interrupción recuperable; nunca COMPLETED; `.part` se conserva/reconcilia según Recovery |
| UI presenta error | Muestra un resumen legible y código, conserva el job y permite acción segura según estado |
| Usuario genera bundle | Contiene logs, versión, schema, configuración sanitizada y error con correlación; excluye página, CBZ, token y password |
| Inspección previa a compartir | El usuario puede revisar el archivo; no hay envío automático |

## 7. Verificación documental

- El bundle es útil para reconstruir etapa/causa/recuperación.
- La sanitización cubre rutas, URL, secretos, blobs del Vault y sus referencias, logs anidados y contenido remoto.
- No incluye DB con datos, páginas ni CBZ.
- Un fallo de empaquetado no deja un archivo presentado como bundle válido.

