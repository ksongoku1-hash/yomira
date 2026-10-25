# Construir la distribución de Windows

Los usuarios descargan el instalador desde Releases. Para construirlo se necesitan los runtimes de runtime-versions.json, las dependencias fijadas en backend/requirements.lock y backend/requirements-package.lock, frontend/package-lock.json e Inno Setup 7.1.0.

La versión proviene de backend/app/version.py y debe coincidir con frontend/package.json y frontend/package-lock.json. Los parámetros Python y NpmCmd permiten indicar las rutas de los runtimes privados.

## Motor y fuentes correspondientes

La referencia vigente está en third_party/fmd2/upstream.json. Para 0.1.16, el SHA-256 de fmd.exe es f288450da8018ae20e755d0b7985f5926d7de60cdcfce95fd858faddff2105ea y la unidad de API es 552ac4afe9c478e3d512e4381901198628b3f1aa2d875d0f31a8e69045f0d7d2.

Usa una distribución limpia con Motor y Licencias, ese ejecutable, sus DLL, los módulos Lua, las licencias y únicamente catálogos públicos. El ZIP portátil de la Release 0.1.16 contiene esa distribución. No uses una carpeta de ejecución diaria ni reutilices el motor o el archivo de fuentes de las Releases de octubre anteriores: corresponden a otros binarios.

El empaquetado valida el ejecutable y los módulos, plantillas y tres catálogos iniciales fijados en el repositorio. Los catálogos públicos adicionales incluidos en esta distribución se inventarían con sus SHA-256. Ninguna biblioteca personal, trabajo de descarga, cookie, token o configuración de usuario forma parte de los insumos.

FMD-Yomira-codigo-fuente-2026.10.24.zip contiene las fuentes correspondientes, dependencias de fuentes, modificaciones e instrucciones de compilación. Para recuperar el upstream fijado y aplicar los cambios del repositorio también puedes ejecutar:

```powershell
.\scripts\restore-fmd-source.ps1 -Destination 'D:/Build/FMD-Yomira-source'
```

El destino debe ser nuevo y su carpeta superior debe existir. El script comprueba los hashes antes de descargar y aplica el parche, las unidades de Yomira, los módulos, las plantillas y los catálogos iniciales.

## Construcción y verificación

Desde la raíz del repositorio, con una salida nueva fuera del checkout:

```powershell
.\scripts\quality-gate.ps1
.\scripts\package-release.ps1 `
    -OutputDirectory 'D:/Build/Yomira-0.1.16' `
    -MotorDistribution 'D:/Build/Distribucion-limpia/Yomira' `
    -ISCCPath 'D:/Tools/Inno/ISCC.exe'
```

El resultado incluye el bundle, installer/Yomira-Setup-0.1.16.exe y Yomira-Windows-x64-0.1.16.zip. Después, scripts/backup-release.ps1 guarda únicamente ZIP e instalador, metadatos y un manifiesto SHA-256 en D:/Yomira-Backup/releases/<versión>, con retención de cinco versiones. No respalda biblioteca personal ni runtimes de desarrollo.

Comprueba el ejecutable empaquetado con raíces de datos y biblioteca temporales, un puerto libre, la interfaz incluida y el motor distribuido. Examina el contenido completo del ZIP. Una prueba en el equipo del desarrollador no certifica otro Windows ni un dispositivo físico.

## Publicación y actualizador

1. Publica el código correspondiente y crea la Release como **borrador** sobre ese commit.
2. Adjunta el instalador, el ZIP portátil, las fuentes correspondientes de FMD, LEEME.txt y SHA256SUMS.txt.
3. Comprueba que todos están en estado uploaded, con tamaño correcto y digest SHA-256 en GitHub igual al archivo local.
4. Publica con etiqueta v0.1.16, como versión estable y última versión, después de esas comprobaciones.

El actualizador consulta la última Release estable y exige el nombre exacto Yomira-Setup-X.Y.Z.exe y su digest. Una etiqueta o un commit no distribuyen una actualización. Publicar una Release incompleta puede causar un error recuperable en los clientes. Consulta [AUTO_UPDATES.md](AUTO_UPDATES.md).

La instalación por usuario en %LOCALAPPDATA%/Programs/Yomira conserva el AppId de Inno Setup y las raíces externas de datos y biblioteca. El programa sigue sin firma de editor de Windows; SHA-256 comprueba integridad y no sustituye esa firma.
