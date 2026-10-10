# ADR-0081 — Rebaselining progresivo de releases y alcance

## Status

Accepted — decisión de alcance autorizada explícitamente por el usuario el 2026-09-27 al pedir que se siga la directriz de simplificación sin abandonar el desarrollo. Esta aceptación registra el mandato de rebaselining; no declara que Yomira V1, ninguna fase de código ni los gates de release hayan pasado.

## Context

La SPEC 1.1 fue aprobada por el usuario el 2026-09-27 y el plan maestro vigente V1.1 contiene las tareas permanentes K001–K284. Ambos documentos fijan una visión técnica completa, pero exigen en el primer release capacidades de recuperación, diagnóstico, almacenamiento, stress y distribución que el usuario ahora ordenó desplegar progresivamente para poder probar un producto funcional antes.

La directriz del usuario ordena conservar todos los requisitos, reducir el alcance inicial sin rebajar la calidad de lo implementado, mantener las decisiones arquitectónicas difíciles de cambiar y producir una secuencia V1, V1.1, V1.2 y V1.3+. También establece como flujo probatorio de V1 buscar, seleccionar una obra, consultar capítulos, descargar, validar, generar CBZ, indexar, leer, cerrar y volver a abrir.

El trabajo de K094 y otras tareas ya comenzado no se cancela por esta decisión. Los cambios existentes se conservan y se evaluarán contra esta secuencia. Esta ADR no autoriza revertir, borrar, descartar o reescribir trabajo, datos o historial para acomodar el nuevo orden.

## Decisión

### 1. Autoridad y conservación de la línea base

- Se conservan sin edición la SPEC aprobada y el plan maestro vigente como línea base histórica y fuente de los requisitos detallados.
- El documento nuevo docs/YOMIRA_MASTER_EXECUTION_PLAN_V1_SCOPE.md es un plan sucesor de alcance. No sustituye silenciosamente el archivo vigente, no renumera tareas y no cambia su texto canónico.
- Cada K001–K284 conserva su identidad, historial, detalle, prueba y trazabilidad. La matriz del plan sucesor asigna cada tarea a una versión y enlaza con la tarea íntegra del plan vigente. Ninguna tarea desaparece por esta clasificación.
- Para el alcance y la aceptación de cada release, esta ADR y el plan sucesor prevalecen sobre los plazos o los gates globales originales. Para los contratos técnicos, invariantes y prohibiciones, la SPEC aprobada y las ADR aceptadas continúan vigentes salvo que una ADR sucesora cite y modifique expresamente una decisión concreta.
- Esta ADR modifica solamente la secuencia de entrega. No reduce los controles aplicables a una característica una vez implementada. Los P0/P1 de integridad siguen bloqueando el release que los incluya.

### 2. Límites de las versiones

**Yomira V1 — producto funcional con fundamentos sólidos.** Entrega una aplicación local Windows que completa el flujo principal mediante React/TypeScript, FastAPI/Python, SQLite/migraciones, biblioteca y búsqueda, contratos y aislamiento de conectores, al menos un conector compatible más el mock determinista, capítulos, DownloadJobs persistentes, descarga a .part, reintento/reanudación razonable, validación segura de imágenes, CBZ y ComicInfo.xml verificados, publicación atómica, biblioteca, lector/progreso, LAN opt-in, uso con Bigme/e-reader, exportación USB segura, instalador y pruebas críticas. La transformación automática de páginas permanece desactivada en V1; originales validados se preservan. Los límites estáticos de cola, workers, streaming, caché y disco continúan siendo obligatorios.

V1 protege explícitamente el contenido ante interrupciones ordinarias, cierres inesperados, errores de red, archivos incompletos, nombres duplicados, disco lleno, permisos y locks transitorios de Windows. En startup, K094 enumera jobs activos en lotes acotados y ejecuta un CAS por job que registra interrupted_from_status al cambiarlo a INTERRUPTED; queda prohibido UPDATE masivo que pierda el origen. Sólo vuelve a encolar/reanudar un job cuando su punto de reinicio y sus archivos se verifican. Ante evidencia ambigua o cancelación pendiente en etapa no reversible, conserva DB y archivos y deja el job explícitamente INTERRUPTED y bloqueado. La reconciliación completa de generaciones, sidecars y checkpoints pertenece a V1.1.

**Yomira V1.1 — recuperación, diagnóstico y procesamiento controlado.** Añade RecoveryCoordinator completo, reconciliación avanzada, replay/checkpoints, clasificación de estados ambiguos, snapshots de diagnóstico durables, Support/Diagnostics mejorados y procesamiento SAFE de imágenes con sus datos y gates. Esta etapa convierte la preservación/bloqueo básico de V1 en recuperación y remediación verificables.

**Yomira V1.2 — integridad, almacenamiento y mantenimiento.** Añade políticas y herramientas de backup/retención/restauración, reconstrucción, verificación profunda, DiskBudgetManager y cleanup recovery-aware, Safe Mode extendido, VACUUM/PRAGMA optimize, y endurecimiento de migraciones. Se conserva en V1 el backup mínimo verificado exigido antes de una actualización o migración que pueda afectar datos.

**Yomira V1.3+ — hardening y madurez de producto.** Añade fault injection exhaustivo, fuzzing/property-based amplio, pruebas intensivas de memoria/soak/stress y benchmarks gigantes, threat model completo, SBOM, supply-chain hardening, signing, rollback sofisticado, actualización avanzada, escenarios extremos de corrupción y observabilidad de producción.

### 3. Fundamentos que no se posponen

En V1 se implementan y verifican los contratos y estructuras cuya sustitución posterior sería peligrosa: modelos relacionales e IDs, migraciones aditivas, rutas relativas y separación entre ejecutable/datos, API versionada y tipada, boundaries entre UI/servicios/repositorios/conectores, aislamiento de fuentes, máquina durable de DownloadJob, idempotencia, publicación atómica, ownership de temporales, verificación de hashes/archivos, contratos del reader, límites de recursos estáticos y semántica de errores/trazas.

El contrato de SecretVault y la prohibición de guardar secretos en claro son fundacionales. Si un conector V1 seleccionado requiere cookies o tokens persistentes, la implementación cifrada de K066 debe aprobar su gate antes de habilitar ese conector. No se permite fallback en texto plano. La implementación completa de un Vault para conectores que no se incluyan en V1 puede esperar; las referencias opacas, el aislamiento y el límite de exposición se mantienen desde el principio.

El manifiesto versionado y las primitivas de escritura/publicación seguras se conservan como compatibilidad arquitectónica. Una ausencia de infraestructura avanzada de recovery no autoriza reconstruir estado por timestamps, borrar sidecars desconocidos ni publicar un CBZ incompleto.

### 4. Resolución de requisitos y decisiones vigentes

| Artefacto o requisito | Resolución de alcance |
|---|---|
| SPEC 1.1, §2.1 y §113 | Se conservan como inventario de capacidades y cobertura completa deseada. La lectura de “incluido en V1” y su gate se reemplaza por la asignación versionada de esta ADR y el plan sucesor. No se editan ni se eliminan requisitos. |
| SPEC §§13, 57–64, 76–77, 84, 89–99, 101–102 y 106–111 | Se conserva su texto técnico; los límites por release se asignan en la matriz K. En V1 siguen los controles mínimos de error, logs acotados, preflight y respaldo pre-upgrade; restore general, recovery complejo, observabilidad durable y pruebas extremas se entregan en etapas posteriores. |
| ADR-0011 Recovery Manager | El principio de inspeccionar antes de limpiar y preservar ante ambigüedad aplica desde V1. La reconciliación completa y el scanner avanzado se entregan en V1.1. |
| ADR-0079 K094 | Se conservan sus invariantes de no inferir por timestamps, no sobrescribir candidatos inciertos, CAS, idempotencia y preservación. K094 tiene un subconjunto operativo obligatorio en V1: con mutex singleton adquirido, startup enumera activos en lotes acotados; cada fila activa se reconcilia con CAS individual y conserva interrupted_from_status, sin UPDATE masivo. Sólo reanuda si verifica punto de reinicio y archivos. Ambigüedad o cancelación pendiente no reversible queda INTERRUPTED/bloqueada y preservada. La reconciliación completa de generaciones, sidecars, checkpoints, replay y RecoveryCoordinator avanzado queda en V1.1; una cuarentena no se denomina recovery completado. Cualquier esquema o dato ya creado se conserva y migra hacia delante; esta ADR no autoriza downgrade destructivo. |
| ADR-0079 K096/K142/K157/K278/K284 | Se reemplaza para el release V1 el bloqueo universal de la primera ejecución de PROCESSING/PACKAGING hasta tener adaptadores de replay avanzados. V1 puede ejecutar su ruta normal sólo a través de validación de página, CBZ temporal, verificaciones completas y publicación atómica. Si un crash deja processing/packaging sin punto de reinicio verificable, no se replaya por heurística: K094 marca INTERRUPTED/bloqueado por CAS, conserva .part/.tmp/target y evidencia, y aísla ese job; la app puede servir biblioteca/lectura si DB/root siguen sanas. K096 gatea este comportamiento básico y las escrituras seguras. Reconciliación/replay durable de cada generación y etapa y adapters SAFE de K142 quedan V1.1; matriz exhaustiva de packaging K157 y fault matrix de K278 quedan V1.3+. V1 conserva pruebas representativas de crash/errores en K157 y falla si se publica o indexa un candidato parcial. K284 exige transparencia/preservación, no la resolución de toda cuarentena V1.1. |
| ADR-0080 diagnóstico durable | Se conserva el contrato aceptado para K095. Su snapshot durable y sus gates se implementan en V1.1. V1 conserva error_code, stage, trace ID y logs estructurados acotados necesarios para explicar fallos de las funciones entregadas. |
| ADR-0075 K090 backpressure | Se conservan los límites y umbrales como decisión de diseño para K090, pero el muestreador RAM/PDH, las pausas automáticas, la histéresis y las pruebas completas son V1.1. V1 aplica concurrencia y admisión estáticas acotadas, disk preflight y cancelación/pausa segura disponibles; nunca interpreta falta de una cota estática como recursos ilimitados. |
| ADR-0015 DiskBudget | El preflight y la reserva mínima que previenen iniciar descargas inseguras siguen en V1. Administración avanzada del presupuesto, reservas dinámicas y limpieza recovery-aware se entregan en V1.2. |
| Procesamiento SPEC §§31–33 y K130–K142 | V1 valida, conserva y empaqueta bytes de imagen sin aplicar recortes automáticos. La frontera del processor, el modo OFF y la ruta sin recomprimir se diseñan/implementan en V1. K131–K136 y K138–K142, incluidos golden/visual gates, se entregan en V1.1 antes de activar transformaciones. No se pierde la capacidad prevista ni se transforma contenido silenciosamente. |
| SPEC §§13, 61–64 y K227–K231 | Backup rutinario/retención, restauración general, deep verify y rebuild se entregan en V1.2. El backup verificado antes de una actualización o migración potencialmente destructiva permanece requisito de V1, así como conservar el directorio de datos durante instalación y desinstalación. |
| ADR-0020 Releases | V1 mantiene actualización manual y compatibilidad explícita. Se exige preservar datos y respaldo antes de upgrade soportado; nunca se permite downgrade de esquema destructivo. Rollback sofisticado y una suite completa de rollback quedan en V1.2. |
| ADR-0053 Secret Vault | Se mantiene cifrado DPAPI/Windows y cero fallback plaintext según su contrato. K066 es precondición V1 para todo conector que use credenciales persistentes; su aplicabilidad se decide al seleccionar connector A, no con una excepción de seguridad. |
| K047 SQLite maintenance | Se conserva VACUUM, PRAGMA optimize, preflight de espacio, trazas y pruebas. La ejecución programada/ciclo de mantenimiento avanzado es V1.2; V1 mantiene cierre correcto de sesiones y WAL y no lanza VACUUM si puede comprometer contenido o dejar la DB inutilizable. |
| K108 OS Lock Handling | Se queda en V1: retry con backoff exponencial acotado para sharing/locks transitorios de Windows en escritura, flush y publicación, distinguiéndolos de disk-full, permisos y rutas inválidas. Los artefactos anteriores permanecen intactos ante agotamiento. |
| K278–K280 | Pruebas de fallo críticas, integridad y migraciones necesarias al flujo principal permanecen en V1. La matriz exhaustiva combinatoria, benchmarks de release y gate general de restore pertenecen a V1.3+/V1.2 según la matriz; K048 y los gates de CBZ/descarga no se eliminan. |
| K284 Release | Se reemplaza el umbral global de “todo K001–K283 avanzado terminado” por el gate de V1 de la sección 10 del plan sucesor. Los incidentes P0/P1, corrupción conocida o artefactos parciales publicados continúan bloqueando V1. Una fila protegida/visiblemente bloqueada no se contará como recuperada, pero no fuerza construir V1.1 antes de poder probar el flujo base si sus bytes están preservados y el caso está expuesto como known issue. |

### 5. Migración de trabajo activo y gates

- El desarrollo actual continúa. El trabajo parcial K094 y cualquier otra tarea ya iniciada no se cancela, elimina ni rehace por esta ADR. El responsable conserva sus cambios, separa la implementación de las capacidades avanzadas y reutiliza lo que satisfaga los contratos de V1.
- El orden del plan sucesor sigue dependencias funcionales. Cada work package puede usar IDs no contiguos, pero cada ID tiene una sola clasificación en la matriz. Las dependencias deben pasar sus gates antes de habilitar el consumidor.
- La implementación V1 puede detener una etapa ante un resultado incierto. Readiness para nuevas descargas puede cerrarse; biblioteca, lectura y exportación local segura deben permanecer disponibles cuando sus datos estén sanos.
- Los gates diferidos no se pueden marcar PASS anticipadamente. Cada release posterior reabre los requisitos y ejecuta sus pruebas antes de activar las capacidades respectivas.
- Cualquier código que ya ofrezca recovery avanzado no se quita si hacerlo aumenta el riesgo o exige migración destructiva. Su presencia tampoco autoriza a relajar invariantes de la SPEC.

## Alternativas consideradas

- **Mantener todo el alcance original dentro de V1:** rechazado por la directriz explícita del usuario; pospone pruebas de producto por exigir endurecimiento amplio antes del flujo principal.
- **Eliminar K-tasks que no son imprescindibles para el primer release:** rechazado porque pierde trazabilidad y requisitos aprobados. Se difieren por versión conservando IDs y especificaciones.
- **Reescribir la SPEC o reemplazar el plan V1.1:** rechazado para no destruir la línea base aprobada; un overlay versionado hace visible el cambio y su autoridad.
- **Lanzar una versión mínima sin los fundamentos:** rechazado porque una DB, API, job model o publicación incorrectos encarecerían y podrían volver inseguras las versiones siguientes.
- **Llamar “recovery” a poner jobs en cuarentena:** rechazado. Bloquear y preservar es una contención honesta, pero no es una recuperación completa ni satisface el gate de V1.1.

## Consecuencias

### Positivas

- Hay un flujo usable y verificable antes de construir recuperación y diagnósticos exhaustivos.
- El contenido original permanece protegido y no se activa procesamiento incierto en V1.
- Los requisitos y decisiones no se pierden, y las fases futuras tienen una secuencia explícita.
- Los equipos pueden retener código parcial compatible sin convertirlo en una condición artificial para probar el producto.

### Negativas y riesgos aceptados

- Tras un crash ambiguo, V1 puede exigir intervención para un job individual en vez de repararlo automáticamente. Los bytes y evidencia se preservan y el caso debe mostrarse como no resuelto.
- Algunas garantías de diagnóstico y automatización llegan después; V1 mantiene el catálogo de errores y trazas mínimos.
- La implementación de un connector con sesión persistente puede requerir completar el Vault antes de conectar esa fuente.
- No se afirma que V1 soporte toda combinación de DB llena/corrupta, filesystem fallido y red perdida.

## Requisitos de verificación

- El plan sucesor enumera exactamente una vez todos los IDs K001–K284, conserva títulos/enlaces y asigna cada tarea a V1, V1.1, V1.2, V1.3+ o KEEP ARCHITECTURAL FOUNDATION.
- Cada requisito diferido aparece en una sección versionada; no se borra por conveniencia.
- El gate V1 verifica el flujo end-to-end, hashes, validación, atomicidad, interrupción/reapertura, Windows/Bigme, LAN opt-in e instalador limpio.
- Una prueba demuestra que las filas o archivos ambiguos de V1 no se limpian ni sobrescriben automáticamente. El caso se conserva y se reporta como pendiente, sin alegar que fue recuperado.
- Los gates V1.1/V1.2/V1.3+ se ejecutan antes de activar las capacidades correspondientes.






