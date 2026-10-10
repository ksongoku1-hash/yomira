# ADR-0075 — Umbrales Windows y backpressure del Job Manager

## Status

Accepted — decisión de Arquitectura Principal el 2026-09-26 para ejecutar K090, derivada de los requisitos de recursos acotados de la especificación y la prioridad aprobada de integridad. Los umbrales son valores iniciales sujetos al benchmark K239; no se atribuye esta decisión al usuario.

## Context

K090 exige observar RAM disponible e I/O de disco, detener nuevas admisiones durante presión crítica, pausar jobs activos en sus safe points y reanudarlos sin cambiar sus estados durables. La especificación define un equipo mínimo como Windows x64 modesto, pero no fija capacidad mínima de RAM ni velocidad de disco. K089 ya ofrece una pausa temporal y conserva `DownloadJob.status`.

La política debe ser independiente de la velocidad del hardware, evitar oscilaciones por muestras aisladas y distinguir una pausa de usuario de una pausa automática. No se añadirá una dependencia Python nueva para leer métricas Windows.

## Decision

- Leer memoria física total/disponible mediante `GlobalMemoryStatusEx`. Medir actividad e I/O del disco mediante los contadores PDH en inglés `PhysicalDisk(_Total)\\% Disk Time` y `PhysicalDisk(_Total)\\Avg. Disk sec/Transfer`. Se usa el agregado del sistema para cubrir la DB, library y temporales sin inferir el dispositivo desde paths. PDH se consulta cada 1 s; la admisión no se habilita hasta obtener muestra válida de los dos contadores.
- RAM entra en presión cuando la disponible es `<= max(512 MiB, 10% de RAM física total)`. Sale solo con `>= max(768 MiB, 15% de RAM total)` durante tres muestras válidas consecutivas.
- Disco entra en presión si `% Disk Time >= 90%` o latencia media `>= 250 ms` durante tres muestras consecutivas. Sale solo con `% Disk Time <= 70%` y latencia `<= 100 ms` durante cinco muestras consecutivas. La latencia es el promedio del contador del sistema, no un límite de latencia garantizado para cada archivo.
- Si la muestra falta, está incompleta o supera los 2 s de antigüedad, el monitor pasa a `MONITOR_UNAVAILABLE`: no admite nuevos jobs y solicita pausa segura de los activos. Tras una falla, exige tres muestras completas consecutivas para retirar esta causa. Una falla de muestreo no libera una causa de presión previa.
- El monitor conserva solo el último sample, streaks finitos y hasta ocho snapshots de jobs activos. No almacena historial ilimitado. El controlador emite un evento estructurado solo al cambiar la presión, la admisión o una pausa; incluye métrica, observado, umbral, acción, razón, `job_id` y etapa cuando corresponda. `SYS_RESOURCE_MONITOR_UNAVAILABLE` identifica la medición fallida y puede reintentarse en el siguiente intervalo.
- Las causas de pausa son independientes y tipadas: `USER`, `BACKPRESSURE` y `MONITOR_UNAVAILABLE`. La recuperación de RAM/disco retira solo `BACKPRESSURE`; la recuperación del sensor retira solo `MONITOR_UNAVAILABLE`. Una pausa de usuario permanece activa hasta su propia reanudación. Cancelación durable conserva precedencia.
- Cualquier causa activa deshabilita nuevas admisiones; los jobs `QUEUED` permanecen intactos en SQLite y el cursor de fairness no avanza mientras no se despacha trabajo. Los jobs activos mantienen su status durable, checkpoints y artefactos; el worker se pausa mediante K089. Tras crash, K091/K094 reconcilian los estados activos y sus archivos antes de reanudar.
- Si Windows no proporciona los contadores PDH o `GlobalMemoryStatusEx` falla, el monitor informa error trazable y bloquea el trabajo hasta restaurar mediciones; la biblioteca y otras funciones locales siguen disponibles.

## Alternatives Considered

- Añadir `psutil`: simplifica el acceso a métricas, pero añade una dependencia nativa que debe versionarse, auditarse y empaquetarse; PDH y `GlobalMemoryStatusEx` ya están disponibles en Windows.
- Usar solo bytes transferidos por el propio proceso: no detecta carga ajena de antivirus, indexadores, sistema operativo u otras aplicaciones.
- Pausar por una sola muestra de disco: reacciona a picos breves y produce pausa/reanudación repetida; se exige persistencia temporal e histéresis.
- Poner RAM/disco dentro de `DownloadJob.status`: añade estados y migraciones, mezcla scheduler con ejecución durable y contradice ADR-0068.
- Permitir jobs mientras no se mide el sistema: trata información ausente como recursos ilimitados y no satisface la regla de recursos acotados.
- Reanudar todos los jobs al despejar presión: elimina pausas manuales de usuario y puede dejar una cancelación solicitada sin atender.

## Positive Consequences

- La política usa métricas del sistema que incluyen procesos externos y no suma memoria por backlog.
- Umbrales y tiempos son reproducibles y se pueden someter a fault tests.
- La histéresis reduce oscilación y las causas de pausa no se cancelan entre sí.
- Si un sensor falta, el sistema conserva la biblioteca y evita admitir más trabajo sin observabilidad.

## Negative Consequences

- Un Windows con contadores PDH deshabilitados no podrá iniciar jobs hasta que el sensor vuelva a estar disponible.
- Los umbrales iniciales pueden pausar antes de tiempo o reaccionar tarde en unidades de almacenamiento inusuales; K239 debe validarlos en perfiles mínimo, referencia y stress.
- Un disco del agregado puede estar saturado aunque los archivos activos usen otro volumen; el comportamiento es conservador.
- Las pausas conservan permisos de concurrencia y una pausa manual puede mantener cerrado el trabajo aunque los recursos se recuperen.

## Failure Modes

- `GlobalMemoryStatusEx` falla, PDH no encuentra un contador o el valor no es válido: cerrar/retener recursos nativos de forma segura, emitir `SYS_RESOURCE_MONITOR_UNAVAILABLE`, impedir nuevas admisiones y solicitar pausa segura.
- Contador de tasa aún no tiene dos muestras válidas: mantener el estado de calentamiento y no habilitar admisiones.
- Una pausa no es segura en la etapa actual: mantener la solicitud y dejar que K089 la atienda en el siguiente safe point; no cancelar escrituras ni transacciones.
- RAM vuelve pero disco sigue saturado (o viceversa): mantener la pausa `BACKPRESSURE` hasta recuperar todas las métricas que originaron presión.
- El proceso termina pausado: no publicar artefactos parciales; el recovery de K091/K094 verifica DB, manifest y files antes de continuar.

## Reversal Strategy

Modificar los umbrales, el agregado de discos, ventanas de histéresis, estrategia de falta de sensores o prioridad entre causas requiere una ADR nueva y fault tests reproducibles. Si se retira el monitor, primero se debe impedir admisión sin verificación equivalente; nunca tratar valores desconocidos como recursos ilimitados.
