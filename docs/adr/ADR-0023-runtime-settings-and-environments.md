# ADR-0023 — Configuración de runtime y entornos

## Status

Accepted — decisión de implementación de Fase 3, registrada antes de K024 y K025.

## Context

ADR-0018 requiere settings centrales, tipados y validados. La especificación fija loopback como bind por defecto y reserva LAN para opt-in posterior. K025 pide separar dev, test y prod sin defaults divergentes ni lectura silenciosa de configuración inválida.

## Decision

- El backend lee solo estas variables conocidas: YOMIRA_APP_ENV, YOMIRA_API_HOST, YOMIRA_API_PORT, YOMIRA_LOG_LEVEL y YOMIRA_DATA_ROOT.
- Los entornos válidos son dev, test y prod. El default es dev.
- Los defaults de runtime son host 127.0.0.1, puerto 8000 y nivel INFO.
- `data_root` es una ruta absoluta tipada. Si `YOMIRA_DATA_ROOT` no está definida, usa `%LOCALAPPDATA%\Yomira\YomiraData`; si `LOCALAPPDATA` no está disponible, usa `%USERPROFILE%\AppData\Local\Yomira\YomiraData`. Un override relativo o mal formado impide el arranque con `SYS_INVALID_CONFIG`.
- YOMIRA_API_HOST debe ser una dirección IP loopback. Cualquier host público, de LAN o nombre no IP impide el arranque con SYS_INVALID_CONFIG.
- El puerto debe estar en el rango 1–65535. El nivel de log solo acepta DEBUG, INFO, WARNING o ERROR.
- La configuración inválida se informa por nombre de campo, sin registrar ni reflejar el valor recibido; el proceso no continúa con defaults alternativos.
- /docs y /openapi.json están habilitados en dev y deshabilitados en test y prod.
- Settings se puede inyectar al construir una app de pruebas; la factoría no modifica os.environ.
- No se leen archivos .env. `YOMIRA_DATA_ROOT` selecciona la raíz de bootstrap de SQLite; no sustituye los settings persistidos de biblioteca ni carga secretos.

## Alternatives Considered

- Cargar todas las variables de proceso: permite que ajustes ajenos cambien comportamiento inadvertidamente.
- Aceptar bind LAN y confiar en firewall: contradice el default local-first y adelanta el opt-in de Fase LAN.
- Sustituir valores inválidos por defaults: oculta errores y puede cambiar el comportamiento solicitado.
- Habilitar OpenAPI en producción: aumenta la superficie informativa sin ser necesaria para el servicio local.

## Positive Consequences

- Los límites del servidor son explícitos, verificables y locales.
- La ubicación inicial de datos es estable aunque cambie el directorio de trabajo, y se puede elegir explícitamente para un despliegue portable.
- Dev/test/prod comparten el mismo bind seguro y solo difieren en exposición de documentación.
- Una configuración incorrecta falla de forma trazable, sin degradación silenciosa.

## Negative Consequences

- No se puede habilitar LAN con variables de entorno; requerirá el flujo de opt-in previsto por la especificación.
- La configuración de usuario persistente y versionada se implementará con el dominio de settings en fases posteriores.
- Cambiar la raíz de datos no traslada automáticamente la base existente; requiere un flujo de migración de ubicación verificado.

## Failure Modes

- Se añade una variable de entorno de comportamiento sin agregarla a Settings y a este ADR.
- `YOMIRA_DATA_ROOT` se interpreta como ruta relativa o cambia sin actualizar este ADR.
- Un valor inválido aparece en logs o mensajes de error.
- Un entorno de producción publica OpenAPI accidentalmente.

## Reversal Strategy

Cambiar nombres de entorno, defaults, rangos, bind, exposición de OpenAPI o la resolución de `data_root` requiere ADR nuevo y pruebas de migración/compatibilidad para instaladores y documentación.
