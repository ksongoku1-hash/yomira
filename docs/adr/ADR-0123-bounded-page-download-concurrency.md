# ADR-0123: Paralelismo acotado para descargar páginas

## Estado

Aceptado por el usuario el 2026-10-05. Se permite un máximo de 2 páginas simultáneas por trabajo y host, con máximo global de 2 solicitudes.

## Contexto

La inspección de solo lectura del historial Alpha encontró capítulos completados de `yomira.novelcool` con 23 páginas y 3,24 MiB que tardaron 314,3 s, y 43 páginas y 5,39 MiB que tardaron 143,0 s. En ambos casos el número de intentos coincidió con el de páginas, por lo que esos trabajos no muestran tiempo añadido por reintentos. Otras fuentes completaron capítulos de 11 a 38 páginas entre 24,5 y 81,1 s. Estas duraciones incluyen el trabajo completo —incluida la validación y el empaquetado— y no equivalen a una medida aislada de red.

`DownloadWorker._download_pages` espera cada página antes de solicitar la siguiente. El transporte tiene una concurrencia global de 2 y de 1 por host, pero el bucle secuencial no solapa páginas de un mismo capítulo. Por tanto, la latencia de cada respuesta se acumula en capítulos con muchas páginas. Aumentar la concurrencia por host cambia el límite aprobado en ADR-0086 y puede provocar rate limiting en una fuente.

## Decisión

1. Mantener el máximo global de 2 solicitudes de descarga activas.
2. Elevar el máximo por host de 1 a 2 y permitir que cada trabajo mantenga hasta 2 páginas en vuelo, siempre sujetos al mismo semáforo global y por host.
3. Mantener sin cambios la allowlist de orígenes, TLS, límites de 64 MiB por página, validación de imágenes, timeouts, redirects, reintentos y el presupuesto temporal agregado de 2 GiB por trabajo.
4. Reservar de forma durable un intento y una ruta temporal exclusivos para cada página antes de iniciar su petición. Persistir la finalización por ordinal y construir el CBZ solo cuando todas las páginas requeridas hayan sido validadas.
5. Ante pausa o cancelación, detener nuevas peticiones, cerrar o drenar los streams ya iniciados y confirmar el estado seguro de cada página antes de reanudar o terminar el trabajo. Un fallo de página impide publicar el CBZ incompleto.
6. Rechazar la propuesta si las pruebas muestran que una página puede sobrescribir otra, que el presupuesto agregado se supera, que la pausa/cancelación deja requests abiertos o que una respuesta fuera de orden altera los ordinales.

## Consecuencias esperadas

- En capítulos cuyo tiempo está dominado por la latencia de cada petición, dos páginas en vuelo podrían reducir parte del tiempo acumulado. No garantiza duplicar la velocidad: el ancho de banda y los límites impuestos por cada fuente siguen siendo factores externos.
- Una fuente puede responder 429, 403 o fallar con mayor frecuencia al recibir dos solicitudes simultáneas. Los reintentos existentes siguen acotados; no se amplían de manera automática.
- El límite total de solicitudes permanece en 2, y memoria, temporales, páginas, tamaño de imagen y empaquetado mantienen sus cotas vigentes.
- La instrumentación actual no registra duración por página, así que la duración total de un trabajo no separa red, validación y CBZ. Una verificación con fixture medirá concurrencia y tiempos simulados; una descarga normal posterior permitirá observar el efecto con una fuente real.

## Verificación requerida si se aprueba

- Escribir primero regresiones que fallen para demostrar concurrencia máxima 2 global y por host, orden estable de ordinales, intentos/rutas temporales independientes, aplicación del límite agregado, y pausa/cancelación con cierre de streams.
- Ejecutar las suites de worker, transporte, reanudación, presupuesto temporal, CBZ y recuperación; Ruff y Pyright.
- Ejecutar una búsqueda/lectura de referencias sin registrar ni persistir URLs firmadas, y verificar que ninguna petición sale de los orígenes ya permitidos.
- Empaquetar y arrancar una candidata aislada antes de actualizar la Alpha.

## Reversión

Restaurar el máximo por host a 1 y el procesamiento secuencial de páginas con una ADR sucesora. Conservar archivos parciales, intentos durables, historial y CBZ ya publicados; no borrar datos para revertir la concurrencia.
