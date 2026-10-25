# Pausas de servidores de imágenes — 2026-10-17

La ficha pública de NovelCoolES/The Boxer respondió HTTP 200 durante la comprobación.
Eso no acredita un bloqueo de IP ni descarta restricciones de un CDN distinto.
El aviso del capítulo 21 ya se había reproducido sin caché como contenido de la fuente.

DownloadTransport registra una pausa por hostname cuando recibe HTTP 429. Las
peticiones siguientes, tanto online como descargas y otros capítulos, se rechazan
localmente hasta que vence ese plazo. Se revisa dentro del turno de concurrencia
para no emitir otra petición en cola después de observar el límite.

Retry-After válido se respeta sin acortarlo: si supera los 60 segundos máximos
de espera activa de ADR-0086, se termina el intento sin reintentar antes de tiempo.
La pausa del host retiene el plazo completo, usando reloj monótono. Sin una cabecera
válida se conserva el backoff existente. HTTP 403 sigue sin reintentos automáticos.
No hay cambios en orígenes permitidos, credenciales, concurrencia ni límites de imagen.
Las páginas verificadas en memoria siguen disponibles, conservando el progreso.

Alcance: transporte de imágenes del core. Los motores externos conservan sus propias
políticas y no siempre trasladan los códigos upstream al core. La pausa está en
memoria y se reinicia al cerrar la aplicación; no certifica ausencia de bloqueos.

Validación: 89 pruebas; Ruff; Pyright de producción y test_download_transport.
test_online_reader mantiene cinco diagnósticos de tipos preexistentes fuera del cambio.
Pruebas de plazo 900 s, límite exacto, cabecera enorme, malformada y fecha pasada;
dos peticiones de capítulos distintos al mismo host bloqueadas, otro host operativo,
recuperación al vencer el plazo, caché y progreso online conservados.
App local activada y apertura de MangaDex con siete páginas comprobada.
No se publicó instalador ni actualización de GitHub.
