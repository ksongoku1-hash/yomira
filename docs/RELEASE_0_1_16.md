# Validación de Yomira 0.1.16 — 25 de octubre de 2026

La entrega reúne el código local posterior a 0.1.13, las mejoras del lector y la interfaz, las recomendaciones públicas, 21 integraciones, el refresco de dos catálogos y el actualizador de Windows. Las notas para usuarios están en [releases/0.1.16.md](releases/0.1.16.md).

## Código y comprobaciones

| Comprobación | Resultado |
| --- | --- |
| Suite completa del servidor | 1972 aprobadas, 11 omitidas, 0 fallos; 792,59 s |
| Suite completa de interfaz | 487 aprobadas en 32 archivos, 0 fallos; 314,61 s |
| Pyright estricto | 333 archivos, 0 errores y 0 advertencias |
| Ruff, TypeScript, ESLint y dependencias Python | Correctos |
| Compilación de interfaz | Correcta; 28 archivos |
| PyInstaller e Inno Setup 7.1.0 | Ejecutable e instalador construidos correctamente |
| Integración de servidor, proxy y SPA | Raíz, biblioteca, trazas, disponibilidad y contrato API correctos |
| ZIP portátil | CRC e integridad de sus 1275 entradas correctos |
| Fuentes correspondientes de FMD | CRC y hashes de sus 2042 entradas correctos |

Las once omisiones del servidor requieren enlaces simbólicos o puntos de reanálisis que Windows no permite crear en este entorno. Se conserva un aviso de obsolescencia de Starlette sobre su cliente HTTP de pruebas; no se cambiaron dependencias por ese aviso.

Los dos intentos iniciales incompletos no se contaron como resultados: el primero no tenía aún la interfaz compilada y el segundo utilizaba una raíz temporal demasiado larga para las comprobaciones de rutas del producto. La ejecución definitiva utilizó el build terminado y raíces temporales cortas. Se mantuvieron las validaciones del producto y el runner habitual de Pytest. Las pruebas de interfaz comenzaron antes del cambio de metadatos 0.1.15 a 0.1.16; su lógica no cambió y la compilación final sí corresponde a 0.1.16.

La limpieza de tipos conserva las 224 funciones de los dieciséis archivos corregidos, sus casos y parámetros. Las diez excepciones puntuales de visibilidad privada de imports de prueba están explicadas en [TYPING_CLEANUP_20261024.md](TYPING_CLEANUP_20261024.md); la configuración estricta global se mantiene.

## Prueba del paquete real

Se extrajo el ZIP final en una carpeta de prueba con espacios y «ñ», con un perfil y una biblioteca temporales. El ejecutable empaquetado:

- Respondió como versión 0.1.16 y completó el arranque de producción.
- Sirvió las rutas raíz, biblioteca y ajustes con la interfaz incluida.
- Expuso el inventario de 21 fuentes públicas.
- Importó un CBZ sintético de tres imágenes y sirvió una página verificada.
- Guardó la página de índice 2, cerró de forma ordenada y conservó esa posición y la serie al volver a iniciar el mismo paquete.
- Cerró de forma ordenada después de la segunda comprobación.

Después se volvió a abrir la aplicación habitual de E como 0.1.16. Una copia consistente previa de SQLite y una comparación lógica posterior confirmaron la conservación de identidades de biblioteca y capítulos, posiciones y porcentajes de lectura, preferencias y estados de trabajos. Las muestras de la prueba no se incorporaron al perfil personal.

## Motor, catálogos y contenido distribuido

El motor incluido tiene SHA-256 `f288450da8018ae20e755d0b7985f5926d7de60cdcfce95fd858faddff2105ea`. La unidad de API corresponde a `552ac4afe9c478e3d512e4381901198628b3f1aa2d875d0f31a8e69045f0d7d2` y el upstream a `ad3a5b632375a5712065373027b0e8cc98a56b60`.

Se verificaron los diez archivos de destino del parche y los hashes de las unidades, módulos y plantillas actuales. Los insumos con hashes fijados están protegidos frente a la conversión de finales de línea de Git en Windows mediante `.gitattributes`.

El paquete conserva 1061 archivos del motor y sus licencias, con doce índices públicos: siete de la distribución previa, dos de la caché oficial comprobada de Weeb Central y ManhuaPlus y tres semillas públicas fijadas en el repositorio. Cada índice pasó `quick_check` y la comprobación del esquema público `masterlist`; su procedencia, tamaño, hash y número de filas están en `CATALOGOS_PUBLICOS.json`. La caché oficial reutilizada es del 22 de octubre y no certifica el directorio completo actual de cada proveedor.

Las fuentes de FMD incluyen dependencias de fuentes, recursos, instrucciones y procedencia, con 660 artefactos de compilación excluidos. El contenido publicable y los paquetes se revisaron para excluir biblioteca personal, historiales, configuración de ejecución, cookies, credenciales y registros.

La lectura online puede reutilizar imágenes desde memoria y una caché temporal local en disco. Esto no crea una descarga en la biblioteca. `LEEME.txt` y las notas describen ese comportamiento.

## Archivos finales

| Archivo | Bytes | SHA-256 |
| --- | ---: | --- |
| Yomira-Setup-0.1.16.exe | 53679971 | `537743ec32f15ad99e73f1f3d147fc04a8b92e63cbfd17cf7e827480265adaff` |
| Yomira-Windows-x64-0.1.16.zip | 72341277 | `58552dd627001ae8e71281bac6f4defff4581a001d886068f72cb414543768b3` |
| FMD-Yomira-codigo-fuente-2026.10.24.zip | 15188826 | `54c409d3a0601b3e8a5916573aa963728f59ce8edfed1807407b814a1a2ffe42` |

Se acompañan de `LEEME.txt`, `CATALOGOS_PUBLICOS.json` y `SHA256SUMS.txt`. El respaldo versionado definitivo conserva el instalador y el ZIP con esos mismos hashes; la primera compilación con instrucciones anteriores se conservó aparte y no es la distribución final.

## Actualizaciones y límites

El actualizador está integrado en el arranque, Ajustes y el aviso de la interfaz. Busca una versión estable superior, descarga y verifica el instalador, y lo aplica cuando la persona pulsa **Reiniciar y actualizar**. Sus 37 casos de prueba forman parte de la suite completa. Una instalación anterior que carezca de actualizador necesita instalar esta entrega manualmente una vez. Las copias portátiles y de desarrollo se actualizan manualmente.

La publicación debe completarse como borrador con todos los archivos subidos y sus digest comprobados antes de convertirla en la última versión estable. Los hashes anteriores identifican los archivos que deben distribuirse.

No se ejecutó Inno Setup contra la instalación personal ni se certificó un Windows limpio o un lector físico. Las pruebas del paquete verifican el ZIP final y sus procesos, no una sustitución real de archivos en otra instalación. El programa continúa sin firma de editor de Windows; SHA-256 verifica integridad y no sustituye esa firma. Las fuentes externas pueden cambiar o no responder; Cartel De Manhwas sigue sin un catálogo utilizable.
