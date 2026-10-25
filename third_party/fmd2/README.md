# Motor FMD2 adaptado para Yomira

El ejecutable usado por Yomira incorpora una API local autenticada y correcciones de adquisición que no existen en un FMD2 oficial sin modificar. `upstream.json` fija el repositorio, la revisión y los hashes de referencia. `yomira.patch` conserva los cambios de archivos existentes; `baseunits/uYomiraApi.pas` conserva la unidad nueva. El paquete portable de prueba y un ZIP del código correspondiente están en [Releases](https://github.com/ksongoku1-hash/yomira/releases).

El motor del paquete incorpora carga de módulos Lua mediante rutas Unicode. Se comprobó su arranque desde carpetas con espacios, `ñ` y caracteres japoneses. El cargador conserva el manejo de errores y admite BOM UTF-8 y primera línea de intérprete.

## Recuperar el código fuente

Ejecuta desde la raíz de Yomira, indicando una carpeta nueva fuera del repositorio:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/restore-fmd-source.ps1 -Destination 'D:/Compilacion/FMD-Yomira'
```

El script recupera la revisión exacta, sus submódulos, aplica el parche y copia la unidad de la API. No copia bibliotecas, cuentas, tareas, tokens o preferencias del usuario.

## Compilar y preparar el motor

La compilación local de referencia utilizó Lazarus 4.6, modo `Win64`. Instala los paquetes que pide el README de FMD2 y registra sus archivos `.lpk` en un perfil de Lazarus. Los paquetes adicionales y sus versiones aún no están automatizados ni fijados por esta primera preparación.

```powershell
lazbuild --pcp='D:/Compilacion/PerfilLazarus' --build-mode=Win64 'D:/Compilacion/FMD-Yomira/mangadownloader/md.lpi'
```

El resultado se encuentra en `bin/x86_64-win64/fmd.exe`. Necesita también las DLL de FMD2, SQLite, SSL, Lua y los módulos Lua de la revisión preparada. No sustituyas el motor adaptado por el ejecutable oficial: perderías la API local de Yomira.

En un directorio de motor nuevo puedes crear `userdata/settings.json` con:

```json
{"general":{"MinimizeOnStart":true}}
```

Si ese archivo ya existe, cambia únicamente `general.MinimizeOnStart` a `true` con FMD cerrado. Es la opción propia de FMD para iniciar sin ventana. Yomira configura el token aleatorio y el puerto de su API al lanzar el proceso; no guardes ni publiques ese token.

Finalmente, usa `scripts/start-yomira.ps1 -FmdExecutable` para indicar el ejecutable. El ZIP portable ya contiene el motor compilado y un lanzador; no necesita instalar Lazarus, Python ni Node para probarlo. La compilación desde fuentes en un Windows sin herramientas previas aún no está certificada.

## Módulo NovelCool del lector

`lua/templates/NovelCool.lua` conserva los identificadores estables y el prefijo de ruta requerido por la API online. Su SHA-256 está fijado en `upstream.json`. Tanto la recuperación de fuentes como el empaquetado validan y copian este mismo archivo; no se toma del perfil personal.
