# ADR-0086 — Límites de recursos para descarga de páginas V1

## Estado

Accepted — propuesta conservadora aprobada explícitamente por el usuario el 2026-09-27. Complementa ADR-0083 y fija los parámetros operativos que la SPEC nombra pero deja pendientes.

## Contexto

El transporte y el validador deben poner límites antes de escribir y antes de decodificar datos remotos. La implementación no puede inventar cifras por defecto, pero no puede activarse de forma segura con políticas incompletas. ADR-0083 requiere límites inyectados, streaming por chunks, HTTPS de producción, concurrencia acotada y los reintentos 1/3/8 segundos.

## Decisión

V1 usa los siguientes límites, suministrados por configuración del core y compartidos entre transporte y validador:

| Parámetro | Límite V1 |
|---|---:|
| Bytes máximos por página | 67 108 864 bytes (64 MiB) |
| Ancho máximo | 40 000 píxeles |
| Alto máximo | 40 000 píxeles |
| Píxeles máximos | 100 000 000 |
| Tamaño máximo de chunk HTTP | 65 536 bytes (64 KiB) |
| Timeout connect | 10 s |
| Timeout read | 30 s |
| Timeout write | 10 s |
| Timeout pool | 10 s |
| Redirects máximos | 5 |
| Espera Retry-After máxima | 60 s |
| Descargas globales simultáneas | 2 |
| Descargas simultáneas por host | 1 |
| Reintentos por página | 3 después del intento inicial |
| Esperas de reintento | 1 s, 3 s y 8 s |

El límite de bytes se aplica tanto a la respuesta de red como a la validación desde disco. `Content-Length` se usa para rechazar temprano cuando existe; el límite efectivo también se comprueba sobre cada chunk, pues el header puede faltar o ser incorrecto. El stream y el decodificador no cargan una página completa en memoria antes de verificar el límite.

Los reintentos se aplican únicamente a las causas transitorias indicadas en ADR-0083. La espera 429 derivada de `Retry-After` se limita a 60 segundos; los demás fallos no se reintentan indiscriminadamente. Las páginas previas `.part` conservan su evidencia y cada intento nuevo requiere una reserva/path exclusivo conforme a ADR-0083.

Los redirects se validan contra la misma allowlist exacta de origen del conector en cada salto. Producción solo usa HTTPS. HTTP se admite únicamente con loopback, transporte de prueba inyectado y servidor fixture.

La política no limita por sí sola el tamaño agregado de todos los capítulos almacenados. El máximo de páginas por manifest continúa siendo el definido por el contrato existente; una política de presupuesto agregado de disco requiere una decisión separada y no se infiere de estos topes por página.

## Alternativas consideradas

- No fijar cifras y dejar el downloader sin configurar: rechazado porque no permite ejecutar el pipeline V1.
- Acumular respuestas completas en memoria: rechazado por el riesgo de picos impredecibles.
- Reintentar cualquier respuesta o ignorar Retry-After: rechazado porque puede agravar rate limits y ocultar fallos permanentes.

## Consecuencias

- Todos los transportes y validadores V1 deben recibir estos límites desde el composition root; no pueden definir defaults divergentes.
- Si una respuesta excede 64 MiB, se preserva la evidencia existente, no se valida ni se empaqueta esa página y el job no puede completarse.
- Los topes pueden excluir imágenes legítimas de tamaño excepcional. Cambiarlos exige una revisión versionada y pruebas de recursos/regresión.
- El presupuesto total de almacenamiento de la biblioteca queda fuera de esta decisión; disk-full debe fallar de forma trazable y conservar CBZ previos.

## Verificación requerida

- Tests al borde y un byte sobre límite para `Content-Length`, stream chunked y validación desde disco.
- Tests de dimensiones y píxeles al borde y un valor superior, antes de decodificar por completo.
- Tests de timeout separado, redirect exacto/externo, Retry-After pasado/malformado/por encima de 60 s y máximo de tres reintentos.
- Tests de concurrencia global 2 / host 1 y memoria acotada por chunks.
- Integración offline que confirma que transporte y validador consumen la misma política desde composición.
