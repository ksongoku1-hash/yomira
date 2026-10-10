# Preparación de la primera copia en GitHub

Destino previsto: repositorio privado `ksongoku1-hash/yomira`.

Se conserva una instantánea del código actual, incluidas las mejoras que aún no estaban registradas en Git. La primera copia utiliza un historial nuevo: el historial de trabajo original permanece en el equipo y no se publica. Esto evita arrastrar informes o archivos retirados de versiones anteriores.

El código de la integración con FMD2 se conserva como parche y unidad de API sobre una revisión fija. El ejecutable y las herramientas locales quedan fuera del repositorio. La licencia pública del código propio, la revisión completa de dependencias y las atribuciones de marca quedan pendientes antes de una distribución pública.

## Comprobaciones del 9 de octubre de 2026

- Dependencias Python y JavaScript restauradas en carpetas nuevas a partir de sus lockfiles.
- Compilación de la interfaz terminada correctamente, incluida la comprobación de tipos TypeScript.
- Servidor: 1.625 pruebas aprobadas, 11 omitidas y un aviso de deprecación de Starlette TestClient; ninguna prueba fallida. Duración: 676,48 segundos.
- Interfaz: 257 pruebas aprobadas en 13 archivos.
- Ruff para todo `app` y `tests`, y ESLint para toda la interfaz: sin errores.
- Arranque real desde `scripts/start-yomira.ps1` en PowerShell de Windows, con datos y biblioteca nuevos, en el puerto 5189: servidor disponible, interfaz servida y biblioteca vacía. No se utilizó la biblioteca personal.
- Restauración de FMD2 desde su revisión exacta mediante el script: los nueve archivos adaptados coinciden por SHA-256 con los usados en la instalación de referencia. Su ejecutable de referencia coincide con el motor local activo.
- Escaneo por patrones de claves reconocibles de los archivos preparados y de 737 blobs históricos: sin hallazgos pendientes. Dos URL con credenciales ficticias en pruebas fueron revisadas y conservadas como fixtures; este escaneo no garantiza detectar cualquier secreto arbitrario.
- Se excluyeron los informes, capturas y evidencias de ejecución locales de `docs/reports`. Las rutas personales que permanecían en la documentación preparada se sustituyeron por ejemplos genéricos.
- Las exclusiones de Git se comprobaron para datos y dependencias, y también para evitar que ocultaran el código de biblioteca y descargas.

## Pendiente antes de una distribución pública

Las pruebas en esta copia limpia del mismo equipo no certifican una instalación en otro Windows. No se ha construido ni probado aquí un instalador nuevo, una compilación de FMD2 con herramientas recién instaladas ni una descarga nueva desde cada fuente externa. Los paquetes adicionales de Lazarus todavía necesitan una preparación reproducible.

La licencia del código propio de Yomira sigue pendiente de decisión. Hay un inventario de licencias declaradas de 39 dependencias Python y 227 entradas JavaScript; faltan los metadatos de seis herramientas de empaquetado Python. También están pendientes la revisión completa de redistribución y las atribuciones de los recursos de marca. Esta primera copia es privada y no constituye una publicación abierta ni una versión de instalador certificada.

No se volvió a ejecutar Pyright estricto para todo el repositorio. Los controles de estilo, tipos TypeScript y pruebas indicados arriba son las comprobaciones efectivamente realizadas.
