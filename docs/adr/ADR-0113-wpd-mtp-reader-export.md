# ADR-0113: Exportar a lectores Windows WPD/MTP sin letra de unidad

## Estado

Aceptada para corregir la detección y exportación a lectores MTP del usuario. El envío físico completo aún requiere el gate en un lector compatible; la detección real de B6 Color y las pruebas de protocolo simuladas ya pasaron.

## Contexto

La exportación V1 enumeraba únicamente volúmenes Windows con tipo `DRIVE_REMOVABLE` y una ruta con letra. El lector conectado del usuario aparece como dispositivo portátil WPD/MTP y expone su almacenamiento por Windows Portable Devices, sin montar un volumen ni asignarle una letra. Por esa razón no aparecía como destino aunque Windows mostraba el lector conectado.

La interfaz WPD no ofrece las operaciones de filesystem ni la garantía de rename atómico de una unidad montada. Una copia directa al nombre final podría dejar contenido parcial visible o reemplazar contenido del lector ante un fallo. Los identificadores PnP y de objeto del dispositivo tampoco deben persistirse ni registrarse.

El 3 de octubre de 2026, la Alpha empaquetada terminó con Windows Application Error 1000, excepción `0xC0000005` en `_ctypes.pyd`, sin traceback Python. Una revisión anterior corrigió la liberación de proxies antes de `CoUninitialize`, pero las dos caídas registradas no tienen un minidump que identifique la llamada responsable. La auditoría posterior encontró además que `open_session` llamaba `device.Close()` antes de soltar los proxies COM hijos de sesión. La regresión comprueba ese orden de liberación; no reproduce por sí sola la violación de acceso.

Durante la recuperación, una solicitud de exportación falló en `source_path` con `FileNotFoundError` antes de escribir al lector. El conector emitió `LIB_PATH_UNAVAILABLE`, pero el modelo de historial solo admite códigos `EXP_` para trabajos fallidos; guardar el fallo lanzó `ValueError`, la API respondió 422 y el registro quedó en `EXPORTING`.

El CBZ afectado sí existía bajo `<data_root>/library/chapters/...`; su tamaño de 2 685 159 bytes y SHA-256 coincidían con el asset verificado. El ensamblado de lifecycle había pasado `<data_root>/library` como raíz, mientras que `asset.relative_path` ya empieza con `library/`, así que el exportador buscaba `library/library/chapters/...`.

## Decisión

1. Mantener la exportación existente a unidades removibles con letra y agregar detección de almacenamiento WPD/MTP mediante Windows Portable Devices. La ausencia o fallo de WPD no oculta los destinos de volumen.
2. Exponer un `target_id` opaco derivado mediante SHA-256 de la identidad de dispositivo/almacenamiento. Los identificadores PnP y de objeto se mantienen en memoria, se excluyen del `repr`, historial y logs, y se vuelven a resolver al iniciar una exportación.
3. Para WPD/MTP, validar el CBZ local, crear un objeto `.part` único dentro del almacenamiento elegido, copiar en bloques acotados, hacer commit, volver a leerlo y verificar tamaño y SHA-256. Solo después se cambia `WPD_OBJECT_NAME` al nombre final y se vuelve a leer/verificar. Se conserva `WPD_OBJECT_ORIGINAL_FILE_NAME` como metadato de importación; el nombre visible se obtiene de `WPD_OBJECT_NAME`.
4. Rechazar conflictos sin alterar el archivo existente. Un destino idéntico solo se considera `SKIPPED` tras lectura y verificación. Desconexiones y resultados ambiguos fallan cerrados y conservan cualquier objeto temporal; no se reintentan escrituras COM inciertas ni se borra evidencia de forma heurística.
5. Aplicar el máximo de exportación existente de 2 GiB por CBZ, bloques de 1 MiB y límites de enumeración de 64 dispositivos, 16 almacenamientos por dispositivo y 20 000 objetos por almacenamiento. El preflight de capacidad proviene de las propiedades del almacenamiento WPD y el resultado de escritura sigue siendo definitivo solo tras la verificación final.
6. Distribuir `comtypes` solo en Windows y empaquetar las interfaces WPD generadas como imports ocultos de PyInstaller.
7. Considerar la publicación MTP una secuencia verificada de staging y cambio del nombre visible, no un rename atómico del filesystem. Mantener explícito el gate de exportación física/desconexión en un lector compatible antes de declarar K206 completo.
8. Cerrar y liberar todos los proxies COM en el mismo hilo y antes de `CoUninitialize`, incluidos dispositivo, sesión y flujo de escritura. Cerrar el flujo en éxito y en error, sin borrar el objeto `.part` que pudiera haber quedado en el lector.
9. Al guardar un fallo de exportación, traducir `LIB_PATH_UNAVAILABLE` a `EXP_SOURCE_INVALID`; cualquier código no perteneciente a `EXP_` se normaliza a `EXP_COPY_FAILED`. Conservar etapa, reintentabilidad y tipo de causa para que todo intento alcance un estado terminal válido.
10. Resolver los `asset.relative_path` desde `<data_root>`, porque el prefijo `library/` forma parte de la ruta relativa persistida. El lifecycle no debe añadir ese prefijo una segunda vez.
11. Soltar `Properties`, `Content` y los demás proxies hijos de una sesión antes de llamar `IPortableDevice::Close`; liberar luego el dispositivo y finalmente ejecutar `CoUninitialize` en el mismo hilo.
12. Limitar la capacidad asignada para `IPortableDeviceManager::GetDevices` a 64 identificadores, aceptar `S_FALSE` como enumeración parcial dentro del límite y liberar cada salida no nula incluso si la llamada termina con un HRESULT de error.

## Consecuencias

- El lector MTP conectado sin letra aparece como destino y puede usar el servicio de exportación existente.
- La seguridad de publicación MTP depende del readback y de la verificación de hash; el temporal preservado puede requerir resolución manual después de una interrupción.
- La liberación explícita de proxies COM antes de desinicializar el apartamento es necesaria, pero la recurrencia nativa registrada abajo demuestra que todavía no basta para declarar estable el backend WPD.
- Soltar los proxies hijos antes de `IPortableDevice::Close` respeta el ciclo de vida del dispositivo WPD y evita retener objetos que ya no deben usarse; este ajuste reduce una superficie de riesgo, pero no atribuye por sí mismo las caídas anteriores.
- El tope de memoria para enumeración se aplica antes de reservar el array de identificadores, no solo después de recibirlo.
- Un archivo CBZ local ausente produce un fallo de exportación terminal y trazable; el historial no debe permanecer en `EXPORTING` por una discrepancia de prefijos de error.
- La raíz de exportación coincide con la raíz usada al publicar assets, por lo que la copia valida el archivo ya existente en lugar de buscar dentro de `library/library`.
- Las pruebas simuladas y la enumeración real validan el flujo y la detección, pero no sustituyen la prueba de escritura en el lector ni el gate de desconexión.
- No se cambia la exportación de volúmenes ni la biblioteca de origen.

## Verificación

- Enumeración de Windows muestra B6 Color y su almacenamiento sin exponer identificadores privados.
- Pruebas automáticas cubren staging, readback, hash, publicación, conflicto, destino idéntico y preservación del temporal en discrepancia.
- 57 pruebas enfocadas pasan, incluidas regresiones de ciclo de vida COM, raíz de asset y persistencia terminal para `LIB_PATH_UNAVAILABLE`; 25 enumeraciones WPD aisladas consecutivas conservaron detección del B6 Color.
- Ruff y Pyright pasan sin diagnósticos para los archivos corregidos.
- PyInstaller incluye `comtypes` y las interfaces WPD generadas.
- K206 permanece pendiente hasta ejecutar y registrar escritura, lectura, reenumeración y desconexión en un lector MTP compatible.

## Seguimiento de caída 2026-10-03

- La candidata empaquetada volvió a terminar con Windows Application Error 1000: PID 25100, excepción `0xC0000005` en `_ctypes.pyd`, desplazamiento `0x9201`, a las 21:25:27 hora local. La API dejó de aceptar conexiones y el lector mostró el error de conexión local.
- El historial de SQLite no contiene un trabajo de exportación nuevo para esa instancia; la única fila sigue siendo la exportación anterior, recuperada como `INTERRUPTED` desde el fallo de `source_path`. No hay evidencia en los registros de una nueva escritura al lector.
- Tras reiniciar como PID 26768, el capítulo local `85e63c3b-4eb3-4dae-bc6e-5d6aac55bb0b` respondió con HTTP 200 en metadatos, ajustes, progreso y sus 17 páginas; `/usb-exports` respondió HTTP 200 y 25 de 25 consultas de destinos detectaron B6 Color. Estas pruebas fueron de solo lectura.
- La causa exacta de la recurrencia `0xC0000005` sigue sin aislarse. El gate de escritura MTP continúa pendiente y esta ADR no declara resuelta la estabilidad nativa hasta registrar una reproducción y su corrección.

## Corrección de ciclo de vida y límites 2026-10-03

- Se añadió una prueba que primero falló al observar `device.Close()` antes de `properties_release` y `content_release`; ahora exige que ambos proxys hijos se liberen antes del cierre del dispositivo, y que el dispositivo se libere antes de `CoUninitialize`.
- Una prueba falsa de WPD reporta 100 000 dispositivos; la llamada reserva como máximo 64 slots, acepta el resultado parcial `S_FALSE` y libera los 64 identificadores devueltos. Otra prueba comprueba que se libera una salida parcial cuando el segundo HRESULT falla.
- Las pruebas enfocadas de WPD, historial de exportaciones, lifecycle y exportación por volumen pasan: 49 pruebas. Ruff y Pyright pasan sin diagnósticos.
- La Alpha empaquetada previa sigue activa como PID 26768 y responde readiness y metadatos del capítulo con HTTP 200. No es una verificación de la build con estas correcciones.
- El evento nativo histórico aún no tiene minidump/stack trace; la causa raíz de `0xC0000005` continúa sin demostrarse. La escritura física MTP sigue pendiente.

## Candidata WPD safety empaquetada 2026-10-03

- Se activó la build `Yomira-alpha-wpd-safety-20261003` como PID 31172 en `127.0.0.1:18082`, con la misma raíz `AlphaData`; antes del cambio no había trabajos de descarga ni exportación activos.
- Readiness y versión 0.1.6 responden correctamente. La ruta SPA del lector, la búsqueda, los metadatos del capítulo y la página local 1 respondieron HTTP 200.
- 25/25 llamadas empaquetadas y consecutivas a `/api/v1/usb-exports/targets` detectaron el almacenamiento compartido de B6 Color. El proceso siguió activo y no se registró otro Windows Application Error 1000 durante esa verificación. No se escribió al dispositivo.
- Los 169 archivos del paquete coinciden con sus tamaños y SHA-256 registrados. Los diagnósticos locales de backend, SQLite, biblioteca, espacio, imágenes y conectores están `ok`; `cbz_builder` queda `unknown` porque la verificación integral publica archivos y fue omitida.
- Las pruebas enfocadas pasan (49), Ruff pasa y Pyright devuelve cero diagnósticos. El historial conserva la exportación previa como `INTERRUPTED`; no se inicia una exportación física como parte de este smoke.
- Esta evidencia no identifica la llamada responsable de los dos access violations históricos de `_ctypes.pyd`. La causa raíz permanece abierta hasta obtener minidump/reproducción determinista. La estabilidad futura no se da por garantizada.

## Auditoría de memoria COM y llamadas nativas 2026-10-03

- La revisión de la API oficial WPD detectó salidas asignadas con `CoTaskMem` que no se liberaban: cadenas devueltas por `IPortableDeviceValues::GetStringValue`, IDs devueltos por `IEnumPortableDeviceObjectIDs::Next`, el ID del stream tras `Commit` y el cookie de `CreateObjectWithPropertiesAndData`. La fuga se acumulaba con enumeraciones y propiedades de almacenamiento; una llamada que devolvía error después de poblar parcialmente su salida también podía dejar memoria sin liberar.
- La enumeración de hijos, lectura de cadenas, consulta `GetDevices` y obtención del ID de commit usan ahora los wrappers tipados generados por comtypes. Se retiraron los dos `WINFUNCTYPE` con índices de vtable escritos a mano. Los buffers y cadenas parciales se liberan en `finally`; los errores de detección registran código clasificado, HRESULT/Win32 cuando están disponibles, etapa y traza, sin copiar el texto del error ni IDs privados al log.
- Se añadieron regresiones que inicialmente fallaron para cadenas normales y parciales, IDs de enumeración normales y parciales, IDs de commit con HRESULT parcial, liberación del cookie en éxito/error y clasificación segura de los errores de enumeración.
- Verificación de fuente: 58 pruebas pasan en `test_wpd_export.py`, `test_export_history.py`, `test_lifecycle.py` y `test_usb_export.py`; Ruff pasa y Pyright, usando el intérprete fijado del proyecto, reporta cero diagnósticos para los archivos revisados.
- Estas fugas y el uso de vtables manuales eran defectos confirmados y se corrigen aquí. Aun así, ninguna reproducción ha ligado una fuga o llamada específica con el `0xC0000005` histórico de `_ctypes.pyd`; la causa raíz nativa permanece **sin confirmar** hasta validar la candidata empaquetada y reunir un dump si reaparece.
