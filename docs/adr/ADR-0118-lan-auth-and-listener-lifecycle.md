# ADR-0118 — Autenticación LAN y ciclo de vida del listener

## Estado

Propuesto — requiere aprobación antes de implementar o habilitar el acceso LAN.

**Alcance:** V1, SPEC §§68–71; ADR-0010; tareas K264 y K281. Esta propuesta concreta los controles de autenticación y el efecto de activar/desactivar LAN. No cambia el almacenamiento ni los datos de la biblioteca.

## Contexto

La SPEC y ADR-0010 requieren LAN opt-in, puerto elegido, autenticación local con sesión temporal, rate limit y solicitud de firewall sólo al habilitar. La configuración actual mantiene el servidor en `127.0.0.1`; no existe flujo de login remoto ni regla de firewall administrada por Yomira.

El uso de una PIN o de una sesión sobre HTTP sin cifrado no protege frente a alguien que pueda observar el tráfico de la red local. V1 debe comunicar este límite y restringir el uso a redes privadas de confianza. Cambiar a TLS requiere certificado confiable en cada dispositivo y queda como alternativa que la propietaria puede elegir antes de implementar.

## Decisión propuesta

1. **Desactivado por defecto:** iniciar siempre en `127.0.0.1`; no solicitar ni crear una regla de firewall durante instalación o uso local.
2. **Activación explícita desde el PC:** LAN se habilita desde la interfaz local, con puerto validado. Antes de abrir el listener se solicita una regla de Windows Firewall sólo para el ejecutable y puerto elegidos, con perfil `Private`. Cancelación, fallo de UAC, puerto ocupado o estado de firewall no verificable mantienen LAN desactivada; no se elige otro puerto en silencio.
3. **PIN local:** al activar, generar una PIN aleatoria de 8 dígitos y mostrarla una sola vez en la interfaz local del PC. Guardarla aislada de la configuración mediante `SecretVault`; nunca escribirla en logs. Para cambiarla o recuperarla, el usuario debe regenerarla desde la interfaz local, lo que revoca las sesiones existentes.
4. **Sesiones:** una PIN válida crea un token aleatorio de 256 bits, entregado al navegador y enviado sólo en el encabezado `Authorization`. Mantener sesiones únicamente en memoria, con vencimiento de 8 horas y tope de 32 sesiones activas. No persistir tokens ni usar cookies de autenticación.
5. **Rate limit:** permitir como máximo 5 intentos fallidos por dirección remota en una ventana móvil de 15 minutos; después, bloquear esa dirección durante 15 minutos. Las respuestas no revelan si una PIN parcial o credencial existe; registrar sólo resultado, dirección normalizada y traza, nunca PIN/token.
6. **Frontera de autorización:** las solicitudes por loopback conservan el comportamiento local actual. Toda ruta API sensible recibida por una interfaz no-loopback requiere una sesión válida; liveness, readiness, archivos estáticos y el endpoint de login son las únicas excepciones. Diagnósticos, biblioteca, lector, trabajos, exportación y ajustes requieren sesión.
7. **Desactivación segura:** al solicitar desactivar LAN desde el PC, bloquear inmediatamente nuevas solicitudes remotas, revocar las sesiones y retirar únicamente la regla de firewall que Yomira creó. Completar un reinicio ordenado del proceso para devolver el listener a `127.0.0.1`; si el reinicio falla, conservar LAN bloqueada y mostrar un error trazable.
8. **Alcance de red:** V1 usa HTTP sólo en una red privada de confianza y muestra esta limitación antes de habilitar. No se promete confidencialidad frente a captura de tráfico o a un atacante con acceso a la LAN. Si la propietaria exige protección frente a ese riesgo, se debe aprobar una decisión TLS antes de implementar.

## Alternativa que requiere selección

**HTTPS con certificado confiable por dispositivo.** Evita enviar PIN y tokens en claro, pero añade alta/renovación de certificados y configuración de confianza en cada móvil/e-reader. La integración LAN debe esperar si esta alternativa es requisito de V1.

## Consecuencias

- La activación requiere autorización local del usuario para firewall y reinicio controlado.
- Si DPAPI/SecretVault, firewall o reinicio no están disponibles, Yomira se mantiene en loopback y comunica la causa con código y traza.
- El vencimiento y el límite de sesiones acotan memoria y duración de credenciales.
- HTTP en red privada satisface el acceso autenticado previsto por la propuesta de SPEC, pero no resiste un observador de la red; la interfaz debe explicitarlo.
- La prueba PASS exige cliente remoto real, autenticación, rechazo de acciones sensibles sin sesión, rate limit, expiración, cierre de LAN y firewall en Windows. Un diagnóstico que sólo confirma loopback no puede declarar LAN PASS.

## Verificación requerida

- Tests rojos previos para host/listener por defecto, autenticación remota, bypass por loopback, rechazo sin/tras expirar sesión, revocación, límite de intentos, límite de sesiones, puerto ocupado y desactivación.
- Tests del flujo de firewall: ninguna petición al instalar/desactivado; solicitud sólo tras opt-in; cancelación/fallo permanece cerrado; eliminación limitada a la regla propia.
- Prueba de reinicio ordenado y comprobación del listener loopback tras desactivar, sin perder ni alterar SQLite, CBZ, progreso o trabajos.
- Gate LAN en red privada con un teléfono/e-reader y firewall de Windows activo.

## Preguntas para aprobación

- ¿Apruebas la propuesta V1 sobre HTTP restringido a red privada de confianza, aceptando explícitamente que no resiste captura de tráfico local, o exiges HTTPS con certificado confiable por dispositivo antes de exponer LAN?
- Si apruebas HTTP, ¿apruebas la PIN de 8 dígitos mostrada una sola vez, sesiones de 8 horas, 5 fallos por 15 minutos y máximo 32 sesiones activas?
