# ADR-0007 — Estado persistente de jobs

## Status

Accepted — aprobado formalmente por el usuario el 2026-09-25.

## Context

Descargas y empaquetado pueden ser interrumpidos por red, suspensión, reinicio, cierre inesperado o disco lleno.

## Decision

Persistir cada DownloadJob y su manifest versionado. Validar transiciones con una máquina de estados explícita; una transición ilegal produce excepción. `INTERRUPTED` puede recuperarse; `COMPLETED`, `FAILED` y `CANCELLED` son terminales.

## Alternatives Considered

- Estado solo en memoria: se pierde al cerrar el backend.
- Un booleano de progreso: no permite distinguir etapa ni recuperar con seguridad.
- Permitir cualquier transición: oculta saltos de validación.

## Positive Consequences

- El estado puede reconciliarse con temporales al arrancar.
- UI y diagnóstico pueden informar etapa, intento y error.

## Negative Consequences

- Cambios de estados y manifests requieren versionado y migración.
- Cada transición añade escrituras y debe respetar el escritor SQLite central.

## Failure Modes

- Estado DB incoherente con archivos, transición ilegal, manifest truncado o job que permanece activo tras crash.

## Reversal Strategy

Versionar el manifest y migrar estados con backup. Si se cambia la máquina, conservar historial y proveer mapeo explícito; no inferir `COMPLETED` a partir de la mera existencia de un archivo.
