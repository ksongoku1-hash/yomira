# ADR-0024 — Mutex Windows para instancia única del backend

## Status

Accepted — decisión de implementación de Fase 3, registrada antes de K032.

## Context

ADR-0017 prohíbe que dos backends gestionen la misma instancia de la biblioteca. El producto V1 se ejecuta en Windows. Un archivo PID puede quedar obsoleto tras un cierre forzado, y borrarlo sin comprobar al propietario puede permitir dos escritores simultáneos.

## Decision

- Usar un mutex con nombre de Windows, Local\Yomira.V1.Backend, como exclusión por sesión de inicio de sesión.
- Crear el mutex durante el startup de FastAPI y conservar el handle durante toda la vida del backend.
- Liberar el handle durante shutdown. Windows elimina el objeto al cerrar el último handle, incluso si el proceso termina inesperadamente.
- Si el objeto ya existe, el segundo backend registra SYS_STARTUP_FAILED con una explicación segura y termina el startup; no escribe un PID ni intenta borrar el objeto existente.
- En K032 no habrá IPC para enfocar o abrir la UI existente. El segundo proceso se cierra con un mensaje trazable, opción admitida por ADR-0017.
- La garantía cubre procesos dentro de la misma sesión Windows. No se crea un lock global que afecte a otras sesiones de usuario.

## Alternatives Considered

- PID file: necesita detección segura de PID obsoleto, reutilización de PID y recuperación tras crash.
- Lock file: añade permisos, ruta de almacenamiento y limpieza persistente innecesarios para la exclusión de proceso.
- Mutex global: puede requerir ACL y coordinación entre sesiones que no forman parte del modo local-first V1.

## Positive Consequences

- El sistema operativo retira el lock después de un cierre forzado.
- No se agrega una dependencia ni un archivo persistente de control.
- El handle coincide con el ciclo de vida de un único proceso backend.

## Negative Consequences

- Una segunda instancia no enfoca la UI existente; se cierra con un error de startup.
- La implementación es específica de Windows, objetivo del producto V1.

## Failure Modes

- Se libera el handle antes de completar shutdown.
- Se reutiliza el mismo mutex para un servicio que no comparte datos.
- Se ignora ERROR_ALREADY_EXISTS y se sirve readiness aunque el proceso no sea propietario.

## Reversal Strategy

Cambiar a un lock interoperable entre sesiones requiere revisar aislamiento por usuario, permisos ACL, recuperación de locks y tests de terminación forzada en un ADR nuevo.
