# ADR-0083 — Contrato de obtención de páginas y reanudación básica V1

## Estado

Accepted — decisión de arquitectura para continuar el flujo V1 autorizado por el usuario el 2026-09-27. Complementa ADR-0051, ADR-0054, ADR-0076, ADR-0078 y ADR-0079; no cambia las tecnologías ni las rutas públicas ya aprobadas.

## Contexto

La aplicación ya puede persistir y listar `DownloadJob`, pero `SourceConnector.get_pages()` solo devuelve referencias opacas. No existe todavía una frontera común para convertir una referencia en una solicitud HTTP, transmitir sus bytes al `.part`, ni registrar los cambios de `DownloadPage`. El mock en memoria y `MockMangaServer` tienen contratos distintos y no deben combinarse implícitamente.

SQLite y el manifest son dos representaciones durables relacionadas. La escritura atómica individual del manifest no sustituye la intención coordinada que ADR-0079 exige para cambios normales de filas de páginas. V1 necesita poder terminar descargas normales sin habilitar el replay avanzado de recovery.

## Decisión

### 1. Referencias y solicitudes

- `ConnectorPageReference.remote_reference` permanece opaca, ordenada y no contiene rutas locales. Los duplicados y el orden entregados por la fuente se conservan; V1 no descarta páginas porque sus referencias o hashes se repitan.
- El contrato de conector añade una resolución tipada de referencia a `PageFetchRequest`. La solicitud contiene una URL absoluta HTTP(S), un `Referer` opcional y, solo si hace falta autenticación, una referencia opaca a `SecretVault`. Nunca transporta contraseñas, cookies, tokens, cabeceras de autenticación en claro ni una ruta de filesystem.
- Cada conector declara orígenes de red confiables en su registro. El transporte rechaza URL con credenciales embebidas, esquemas distintos de HTTP(S), destinos fuera de los orígenes declarados y redirects fuera de esa misma política. Producción requiere HTTPS. HTTP de loopback se admite únicamente en pruebas, con transporte y servidor de fixture inyectados; no se habilita en el runtime normal.
- Los conectores resuelven referencias y normalizan datos; no hacen HTTP de páginas, no escriben archivos y no reciben acceso a SQLite ni a la biblioteca. Un conector que necesite sesión persistente no se habilita hasta pasar K066/ADR-0053.

### 2. Transporte y escritura de páginas

- Un único `DownloadTransport` del core usa `httpx.AsyncClient` compartido. Controla estado HTTP, redirects permitidos, timeouts, retries acotados, cancelación, semáforos estáticos, límite de bytes, streaming por chunks y SHA-256. No acumula la respuesta completa en RAM.
- El core deriva paths solo de `job_id`, ordinal y número de intento. Un `.part` se crea de forma exclusiva bajo el `data_root` identificado; nunca se sobrescribe un destino preexistente. El conector no escoge ni devuelve paths.
- `DOWNLOADED` solo se registra tras EOF, cierre correcto del stream, flush/cierre del archivo y conciliación de tamaño/hash. Después, el validador vuelve a leer los bytes y comprueba tipo, firma, límites, decode, dimensiones y truncamiento. Una página validada se promociona sin reemplazar archivos existentes y pasa a `VALIDATED` solo después de verificar el destino publicado.
- El builder recibe exclusivamente páginas validadas. La secuencia continúa con CBZ temporal, ComicInfo.xml, verificación desde disco, publicación atómica e indexación posterior, según la SPEC y K143–K157.

### 3. Coordinación DB ↔ manifest

- Toda mutación de filas representadas en `DownloadJobManifest` usa el protocolo normal de intención durable de ADR-0079: mutex del job; proyección `D_before` y propuesta `D_after`; reserva `ManifestWriteReservation`; una transacción SQLite breve que hace CAS de las filas y guarda `download_manifest_state(publication_pending=true)`; escritura/publicación y verificación del manifest; y CAS que cierra el marcador.
- `reserve_write` y el rechazo de `save` sin reserva pertenecen al gate V1 de escritura normal. Los tests de replay de candidatos tras cada frontera, clasificación completa de sidecars y reparación avanzada siguen en V1.1.
- Una transacción DB no queda abierta durante HTTP ni filesystem. Si el marcador queda pendiente, un hash/tamaño/generación diverge o un artefacto no se puede clasificar, V1 preserva filas y archivos, bloquea ese job y lo presenta como pendiente; no reconstruye el manifest por inferencia, no promociona staging y no limpia candidates ambiguos.

### 4. Reintentos y reanudación ordinaria

- Una página `DOWNLOADED` o `VALIDATED` solo se reutiliza después de que DB y manifest concuerden en job, ordinal, estado, path, tamaño y SHA-256, y el archivo vuelva a pasar la verificación correspondiente.
- Un `.part` cuyo estado durable sea `DOWNLOADING` no se añade con HTTP Range ni se declara completo por tamaño o nombre. Al reintentar, el core registra antes una nueva generación de intento con path único `jobs/<job_id>/pages/<ordinal>/attempt-<n>.part` y lo abre en modo exclusivo; conserva el intento previo. El contador durable acota el número de intentos conforme a K101. Si no queda un intento seguro o el filesystem no permite reservar espacio, el job falla o queda bloqueado con sus artefactos preservados.
- Después de reiniciar, una página completa con evidencia verificable se puede reutilizar y el job puede continuar desde ella. Un intento interrumpido puede repetirse desde cero en otro path; no se afirma reanudación de bytes parciales. Los fallos de DB/manifest o de promoción dejan el job visible como `INTERRUPTED`/bloqueado hasta una salida verificable.
- Los temporales generados por Yomira tienen límites por página e intento. Su limpieza solo se permite cuando ownership, raíz, path y estado terminal estén probados; V1 no borra archivos ambiguos.

### 5. Fixtures

- El gate HTTP usa un adaptador de prueba que resuelve referencias a la URL de `MockMangaServer`, ejecutado en loopback y puerto efímero bajo una política de test explícita. Debe cubrir contenido válido, MIME/firma incorrectos, desconexión parcial, límites, HTTP 404/429/500, timeout y orden/duplicados.
- El `MockConnector` in-memory actual conserva su contrato de metadata y referencias. No se declara que entregue bytes hasta implementar un adaptador que cumpla este ADR.

## Alternativas consideradas

- Poner URLs en `ConnectorPageReference`: rechazado porque pierde la separación entre identidad remota y mecanismo de transporte, y vuelve las referencias dependientes de una URL concreta.
- Dejar que cada conector descargue o escriba sus páginas: rechazado porque dispersa límites, trazas, retries, hash, secretos y ownership de temporales.
- Actualizar SQLite y guardar JSON sin marcador: rechazado porque un crash puede dejar dos estados durables contradictorios sin intención verificable.
- Continuar una transferencia parcial con Range sin validador remoto durable: rechazado porque bytes de versiones distintas podrían mezclarse.
- Abrir un destino `.part` existente con truncado: rechazado porque sobrescribe evidencia sin probar ownership ni estado.

## Consecuencias

- El siguiente incremento debe implementar primero el contrato de resolución, la reserva/escritura coordinada y el transporte central; una cola `QUEUED` no implica descarga.
- La política preserva los bytes parciales, pero puede repetir una página desde cero. El uso de disco sigue acotado por límites de tamaño, intentos, concurrencia y cola.
- V1 no promete resolver automáticamente toda contradicción DB/filesystem/manifest. Su salida segura es preservar, bloquear el job afectado y permitir que los demás datos sanos sigan accesibles.
- Esta ADR no habilita un conector externo específico ni autoriza eludir controles de acceso. K221 seleccionará y gateará un conector real compatible antes de declarar V1 listo.

## Verificación requerida

- Contract tests de referencias opacas, resolución de URL, allowlist/redirects y ausencia de secretos.
- Tests rojos para page-row writer, reservation/marker/manifest, streaming `.part`, hash por chunks, reintentos y reanudación de páginas completas verificadas.
- Tests de fallo representativos que prueben que `publication_pending`, desconexión, disk full, lock temporal y bytes/hash discordantes no publican ni indexan páginas/CBZ parciales y conservan la evidencia previa.
- Integración offline `MockMangaServer → DownloadJob → DownloadPage → .part → validación → CBZ` antes de conectar UI de progreso o una fuente real.
