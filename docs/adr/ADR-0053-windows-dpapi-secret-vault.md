# ADR-0053 — Secret Vault local protegido con Windows DPAPI

## Status

Accepted for K066 implementation — 2026-09-26.

## Context

K066 exige guardar tokens y cookies de larga duración cifrados, separados de `settings`, accesibles solo mediante una interfaz del Vault y ausentes de logs, exports de configuración y Support Bundle. La especificación prefiere Windows Credential Manager o DPAPI. Windows Credential Manager limita el tamaño de cada credencial genérica; los cookies de una fuente pueden exceder ese límite. La aplicación es local-first y no requiere sincronizar credenciales entre usuarios o equipos.

## Decision

- El Vault usa `CryptProtectData` y `CryptUnprotectData` de Windows DPAPI con scope de usuario actual. No usa `CRYPTPROTECT_LOCAL_MACHINE`; deshabilita prompts con `CRYPTPROTECT_UI_FORBIDDEN` y falla cerrado si DPAPI no está disponible.
- Cada registro se guarda como un blob independiente en `data_root/secrets/<uuid>.dpapi`. El nombre contiene solo un UUID aleatorio, no un nombre de fuente ni credencial. Los bytes persistidos son únicamente el ciphertext DPAPI; el payload versionado, el secreto y su vencimiento quedan cifrados. La entropía adicional liga el blob al propósito Yomira y a su UUID.
- El API `SecretVault` recibe y devuelve bytes solo en memoria. `store` devuelve `SecretReference` opaca `yomira-secret:v1:<uuid>`; leer o revocar exige esa referencia tipada. La representación de la referencia no se imprime en logs ni se incluye en exports. `settings` puede guardar la referencia, nunca los bytes del secreto.
- Cada blob se escribe primero a un temporal exclusivo, se vacía a disco y se publica mediante reemplazo atómico. Solo se devuelve la referencia después de publicar el blob. No se sobrescribe otra referencia. Un fallo de escritura limpia el temporal cuando es posible y no usa un destino en texto plano.
- Para recursos previsibles, el Vault limita cada secreto a 64 KiB y el almacén a 128 registros. Exceder cualquiera de estos límites falla sin truncar ni descartar secretos existentes.
- El vencimiento opcional se valida dentro del payload cifrado. Una lectura vencida falla con `AUTH_SECRET_EXPIRED` y conserva el blob; no lo borra ni renueva en silencio. La revocación elimina solo el registro indicado, es idempotente y nunca devuelve su contenido. Para sustituir un secreto vigente, el consumidor persiste primero la nueva referencia y revoca la anterior después.
- El backup normal puede copiar los blobs ciphertext preservando referencias. DPAPI decide si el restore puede descifrarlos; Yomira no promete restauración en otro usuario/equipo y nunca exporta una clave o credencial en claro. Si el descifrado falla, conserva el ciphertext para diagnóstico y solicita volver a autenticar la fuente.
- `revoke` revoca únicamente el acceso local de Yomira; no invalida el token en la fuente remota ni garantiza borrado físico de copias del ciphertext en SSD, snapshots o backups antiguos. La revocación remota, si el proveedor la admite, corresponde al owner de autenticación de la fuente. Restaurar un backup anterior puede restaurar su credencial antigua y debe volver a validarla antes de usarla.
- El Support Bundle excluye el directorio `secrets`, sus blobs y referencias opacas. K066 fija esta frontera en la especificación y prueba documental; el generador y su prueba de integración pertenecen a K232–K233.
- Fallos usan códigos `AUTH_SECRET_*`, mensajes fijos, `trace_id` activo o UUIDv4 y causa técnica reducida a tipo. No se registra payload, referencia, ruta, mensaje de excepción DPAPI ni bytes. Un error de `CryptUnprotectData` no distingue de forma fiable corrupción de contexto de usuario ausente, por lo que se clasifica `AUTH_SECRET_UNAVAILABLE` y se conserva el blob; `AUTH_SECRET_CORRUPT` se usa cuando el payload descifrado viola el formato o el límite.

## Alternatives Considered

- **Windows Credential Manager:** integración nativa y cifrado administrado por Windows, pero su límite de tamaño por credencial no ofrece margen fiable para cookies de sesión completos.
- **Cifrado propio con clave en archivo de configuración:** la clave quedaría junto al ciphertext y no mejora la protección del perfil de usuario.
- **DPAPI con scope de máquina:** permite a otras cuentas del equipo descifrar el secreto; no se necesita y amplía la exposición.
- **Fallback a texto plano ante error del sistema:** convertiría un fallo de seguridad o disponibilidad en filtración persistente y está prohibido.
- **Guardar el valor cifrado en `settings` o SQLite:** mezcla secretos con exportaciones, diagnósticos y backups no dedicados, además de ampliar los lectores autorizados.

## Consequences

- El almacén no necesita dependencia Python criptográfica adicional; utiliza las API del sistema operativo y funciona solo en Windows.
- El ciphertext está ligado a la protección de usuario de Windows. Cambio de identidad, perfil dañado o traslado de equipo puede volver un registro no disponible; el blob se conserva y la fuente puede requerir nueva autenticación.
- La revocación local bloquea lecturas futuras por la referencia activa, pero no reemplaza revocación con el proveedor ni destruye backups históricos.
- La protección cubre almacenamiento en reposo, no un proceso malicioso que ya se ejecuta como el mismo usuario ni el secreto mientras el consumidor lo usa en memoria.
- La referencia persistible no es una credencial ni permite descifrar por sí sola; el consumidor obtiene acceso únicamente al inyectar la interfaz `SecretVault`.
- Los límites iniciales son parte del contrato. Aumentarlos o cambiar política de scope, expiración, backup, formato o exposición requiere otro ADR.

## Failure Modes

- DPAPI ausente, perfil no disponible o verificación DPAPI fallida (que no distingue de forma fiable usuario incorrecto de ciphertext alterado): `AUTH_SECRET_UNAVAILABLE`; conservar ciphertext y no leer ni escribir en claro.
- Referencia inexistente: `AUTH_SECRET_NOT_FOUND`; no explorar rutas proporcionadas por el caller.
- Payload que pudo descifrarse pero tiene formato, versión o tamaño inválido: `AUTH_SECRET_CORRUPT`; no sobrescribir ni eliminar el blob.
- Secreto vencido: `AUTH_SECRET_EXPIRED`; conservar el ciphertext para que el owner decida renovarlo.
- Límite excedido o disco/permiso insuficiente: `AUTH_SECRET_STORE_FAILED`; no publicar referencia ni truncar datos.
- Revocación con bloqueo/error de filesystem: `AUTH_SECRET_REVOKE_FAILED`; conservar el registro y permitir retry explícito.

## Reversal Strategy

Un cambio de proveedor criptográfico requiere migración por usuario autenticado: leer y verificar un blob, escribir y verificar el formato nuevo y solo entonces revocar el anterior. Nunca se realiza migración mediante fallback en claro. Mantener las referencias opacas hasta confirmar todos los consumidores y backups; un formato desconocido queda preservado para inspección.
