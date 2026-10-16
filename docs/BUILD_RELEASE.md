# Construir la distribución de Windows

Los usuarios descargan el instalador desde Releases y no necesitan estas
herramientas. El mantenedor necesita los runtimes de runtime-versions.json,
las dependencias de backend/requirements.lock y
backend/requirements-package.lock, el frontend restaurado con npm ci, el
motor FMD2 redistribuible y el compilador Inno Setup 7.1.0.

El empaquetado usa los runtimes privados del proyecto de forma predeterminada. La versión proviene de backend/app/version.py.

El motor fijado para esta versión es el de la Release
v0.1.6-preview.20261009. Descarga
Yomira-Windows-x64-2026.10.09.zip desde esa Release y extrae su contenido.
Usa como MotorDistribution la carpeta que contiene Motor y Licencias.
El script comprueba el SHA-256 de FMD contra
third_party/fmd2/upstream.json. No uses una carpeta de FMD de uso diario:
puede contener datos privados.

Desde la raíz del repositorio, con una carpeta de salida nueva fuera del
repositorio:

    .\scripts\package-release.ps1 -OutputDirectory 'D:/Build/Yomira-0.1.7' -MotorDistribution 'D:/Build/Distribucion-anterior/Yomira' -ISCCPath 'D:/Tools/Inno/ISCC.exe'

El resultado incluye el bundle con el motor y licencias, el instalador en
español y el ZIP portátil. Publica también installer/LEEME.txt, sumas
SHA-256 y el código fuente correspondiente de FMD de la Release anterior: el
motor no cambia. Antes de publicar, prueba una instalación aislada y el
arranque con raíces de datos temporales; nunca incluyas bases de datos,
cookies, logs o mangas.

Después de crear correctamente el instalador y el ZIP, el script llama a scripts/backup-release.ps1. La copia versionada en D:\Yomira-Backup\releases\<versión> contiene solo el ZIP portátil, el instalador, metadatos y el manifiesto SHA-256; no incluye biblioteca, base de datos, descargas, runtimes ni registros. Se prepara en una carpeta temporal, se verifica y se renombra de forma atómica. Se conservan las cinco versiones más recientes. Si falla la creación de artefactos, no se inicia el respaldo de release.

El ejecutable configura automáticamente el FMD que tiene junto a él, espera
readiness y abre /library en el navegador. La instalación es por usuario en
%LOCALAPPDATA%/Programs/Yomira. No requiere elevación y crea accesos
directos. El desinstalador no elimina la biblioteca ni los datos situados
fuera del programa.
