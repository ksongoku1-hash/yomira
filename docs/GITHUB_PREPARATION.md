# Preparación de la primera copia en GitHub

Destino: repositorio `ksongoku1-hash/yomira`, con publicación pública autorizada por su propietario.

Se conserva una instantánea del código actual, incluidas las mejoras que aún no estaban registradas en Git. La primera copia utiliza un historial nuevo: el historial de trabajo original permanece en el equipo y no se publica. Esto evita arrastrar informes o archivos retirados de versiones anteriores.

El código de la integración con FMD2 se conserva como parche y unidad de API sobre una revisión fija. Los ejecutables se distribuyen en Releases; las herramientas locales y los datos personales quedan fuera del repositorio y del paquete.

## Comprobaciones del 9 de octubre de 2026

- Dependencias Python y JavaScript restauradas en carpetas nuevas a partir de sus lockfiles.
- Compilación de la interfaz terminada correctamente, incluida la comprobación de tipos TypeScript.
- Servidor: 1.625 pruebas aprobadas, 11 omitidas y un aviso de deprecación de Starlette TestClient; ninguna prueba fallida. Duración: 676,48 segundos.
- Interfaz: 257 pruebas aprobadas en 13 archivos.
- Ruff para todo `app` y `tests`, y ESLint para toda la interfaz: sin errores.
- Arranque real desde `scripts/start-yomira.ps1` en PowerShell de Windows, con datos y biblioteca nuevos, en el puerto 5189: servidor disponible, interfaz servida y biblioteca vacía. No se utilizó la biblioteca personal.
- Restauración inicial de FMD2 desde su revisión exacta mediante el script: los nueve archivos adaptados coincidieron con la instalación local. Después se corrigió la carga de Lua en rutas Unicode y se recompiló el motor para el paquete portable; el nuevo parche superó la comprobación inversa de aplicación. Los hashes nuevos están en `third_party/fmd2/upstream.json`. El motor de la instalación personal no se sustituyó.
- Escaneo por patrones de claves reconocibles de los archivos preparados y de 737 blobs históricos: sin hallazgos pendientes. Dos URL con credenciales ficticias en pruebas fueron revisadas y conservadas como fixtures; este escaneo no garantiza detectar cualquier secreto arbitrario.
- Se excluyeron los informes, capturas y evidencias de ejecución locales de `docs/reports`. Las rutas personales que permanecían en la documentación preparada se sustituyeron por ejemplos genéricos.
- Las exclusiones de Git se comprobaron para datos y dependencias, y también para evitar que ocultaran el código de biblioteca y descargas.

## Paquete portable de prueba

- ZIP Windows x64 construido con Python 3.14.7, SQLite 3.50.4 y PyInstaller 6.22.3. Incluye interfaz, motor adaptado, avisos de dependencias y catálogos públicos de títulos.
- Diez pruebas de empaquetado aprobadas y análisis de sintaxis del lanzador en PowerShell de Windows aprobado.
- Prueba real del ZIP extraído en una ruta con espacios y `ñ`: arranque, interfaz, búsqueda de Naruto con 50 resultados, importación de imágenes y lectura aprobadas con datos nuevos.
- Prueba del motor en una ruta con `ñ` y caracteres japoneses: nueve fuentes registradas; un módulo con BOM y primera línea de intérprete también cargó correctamente.
- Se verificó la integridad de los ZIP y se generaron `MANIFIESTO.json` y `SHA256SUMS.txt`.
- Se incluye un ZIP separado del código correspondiente del motor, con sus paquetes adicionales y los cambios de Yomira.
- La instalación personal se reinició tras la prueba y conservó su biblioteca, cola y registros de exportación.

## Límites de esta versión de prueba

Las pruebas en esta copia limpia del mismo equipo no certifican una instalación en otro Windows. El ZIP portable no está firmado. No se verificó la apertura automática del navegador, una compilación de FMD2 con herramientas recién instaladas ni una descarga nueva desde cada fuente externa. Los paquetes adicionales de Lazarus todavía necesitan una preparación reproducible.

La licencia del código propio de Yomira sigue pendiente de decisión. Hay un inventario de licencias declaradas de 39 dependencias Python y 227 entradas JavaScript, incluidos los metadatos de las seis herramientas de empaquetado. El paquete conserva los avisos encontrados y el código correspondiente de FMD2. La revisión completa de redistribución y las atribuciones de los recursos de marca siguen pendientes; publicar el repositorio no equivale a asignar una licencia abierta al código propio.

No se volvió a ejecutar Pyright estricto para todo el repositorio. Los controles de estilo, tipos TypeScript y pruebas indicados arriba son las comprobaciones efectivamente realizadas.
