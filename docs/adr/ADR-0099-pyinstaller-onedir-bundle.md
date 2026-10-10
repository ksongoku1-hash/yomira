# ADR-0099 — PyInstaller onedir bundle

**Estado:** Aceptado para K258.  
**Fecha:** 2026-09-27.

## Contexto

K257 hace que FastAPI sirva el frontend compilado desde `app/web` dentro del bundle PyInstaller. El inicio también ejecuta migraciones Alembic, que requieren `alembic.ini`, `env.py` y los archivos de revisión disponibles como archivos físicos. La aplicación debe mantener su base de datos, biblioteca, logs y secretos en el `data_root` de usuario, fuera del directorio del programa.

## Decisión

K258 genera un bundle Windows **onedir** mediante un archivo `.spec` versionado. PyInstaller coloca el ejecutable y sus dependencias en un árbol `Yomira/`; los recursos web se copian a `app/web`, y la configuración y revisiones Alembic se incluyen en las rutas que consume `MigrationRunner`. La configuración de build no incorpora ni resuelve datos de usuario.

El build usa CPython fijado por `runtime-versions.json`, un pin de PyInstaller separado de las dependencias de aplicación, y un script que rechaza sobrescribir una salida existente. Los ficheros temporales de build se escriben en una ruta nueva y única; ante fallo se conservan para inspección. Un runtime hook define `YOMIRA_APP_ENV=prod` solo cuando el proceso empaquetado no trae un valor explícito.

## Consecuencias

- El directorio empaquetado es visible y puede instalarse después con Inno Setup; K258 no define instalación, actualización ni desinstalación.
- No se expande un ejecutable onefile en un directorio temporal en cada inicio.
- La versión fijada de PyInstaller y la plataforma del bootloader forman parte del entorno de build.
- Un bundle onedir no demuestra instalación limpia ni funcionamiento sin runtimes o dependencias externas; esos son gates posteriores.
- `Yomira.exe` conserva consola para que el prototipo registre errores de arranque de forma visible. La experiencia de lanzamiento pertenece a K259.

## Verificación

Los tests de K258 validan el plan de recursos y el contenido del `.spec`. El build de Windows y el smoke del ejecutable se registran en `docs/reports/K258_PYINSTALLER_2026-09-27.md`; eso no sustituye los gates K259–K269.
