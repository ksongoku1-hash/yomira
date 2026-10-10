# ADR-0058 — Servidor de manga falso limitado a loopback

## Status

Accepted for K071 implementation — 2026-09-26.

## Context

Fase 7 requiere un servidor HTTP falso que reproduzca respuestas normales y fallos remotos de manera local y determinista. Es una dependencia de pruebas para K080 e integración futura; no forma parte del runtime de producto. El servidor debe sobrevivir a pruebas abortadas sin abrir servicios en la LAN ni crear un número ilimitado de workers.

## Decision

- Implementar el servidor como test support con módulos de biblioteca estándar de Python; no añadir dependencias de producción.
- Enlazar siempre a `127.0.0.1` y puerto 0 del sistema operativo. No exponer un parámetro para cambiar host o puerto.
- Aceptar como máximo cuatro requests concurrentes. El backlog y el número de workers quedan acotados; una conexión fuera de capacidad se cierra sin crear otro hilo.
- Exigir `start()`/`stop()` o uso con `with`; `stop()` es idempotente, cierra el socket y espera la terminación del hilo de servicio.
- Usar GET y rutas fijas; no aceptar rutas de archivos, comandos, código o destinos de proxy desde el request.
- Las respuestas de escenarios posteriores tendrán fixture bytes y tiempos finitos explícitos, sin reloj aleatorio ni dependencia de Internet.
- Ningún módulo de aplicación, registro de producción o endpoint de usuario arranca este servidor.

## Alternatives Considered

- **Bind a `0.0.0.0`, hostname o puerto fijo:** podría exponer el fixture en interfaces externas, colisionar con otros procesos y volver inestables las pruebas paralelas.
- **Crear un hilo por conexión sin límite:** permite que tests de carga consuman recursos sin una cota definida.
- **Framework ASGI adicional:** añade una dependencia o un runtime más para un fixture HTTP pequeño; las herramientas estándar cubren el protocolo y las desconexiones controladas.
- **Usar servicios públicos como fuentes de prueba:** hace que los resultados dependan de conectividad, cambios remotos y contenido ajeno.

## Consequences

- Tests de protocolo HTTP pueden ejecutarse offline con endpoint y puerto aislados por instancia.
- La concurrencia máxima del servidor queda en cuatro; escenarios lentos pueden ocupar slots, y la saturación se puede reproducir sin crear workers sin límite.
- Los clientes del servidor deben leer su `base_url` después de iniciar la instancia y cerrarla en un bloque context-managed.
- Un puerto efímero cambia entre ejecuciones por diseño; la suite no debe asumir un número de puerto fijo.

## Failure Modes

- Inicio repetido en una instancia activa: falla claramente y no crea otro socket.
- Cierre repetido: no cambia el estado ni genera un error.
- Fallo de startup: el socket parcial se cierra antes de propagar el error.
- Worker fuera del límite: la conexión se cierra sin crear un worker adicional.
- Thread que no termina dentro del límite configurado: `stop()` informa el fallo en vez de declarar cierre exitoso.

## Reversal Strategy

Permitir bind externo, puerto configurable, más workers o dependencia de servidor nueva requiere ADR, pruebas de aislamiento y un presupuesto explícito de recursos. El servidor debe permanecer exclusivamente en test support.
