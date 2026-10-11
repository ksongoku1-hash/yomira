# Construir la distribución de Windows

Los usuarios descargan el instalador desde Releases y no necesitan estas herramientas.
El mantenedor necesita los runtimes de `runtime-versions.json`, las dependencias
de `backend/requirements.lock` y `backend/requirements-package.lock`, el frontend
restaurado con `npm ci`, y el compilador Inno Setup 7.1.0.

El motor de esta versión es el de la Release `v0.1.6-preview.20261009`. Descarga
`Yomira-Windows-x64-2026.10.09.zip` desde esa Release y extrae su contenido.
Usa como MotorDistribution la carpeta que contiene `Motor` y `Licencias`.
El script comprueba el SHA-256 de FMD contra `third_party/fmd2/upstream.json`.
No uses una carpeta de FMD de uso diario: puede contener datos privados.

Desde la raíz del repositorio, con una carpeta de salida nueva fuera del repositorio:

```powershell
./scripts/package-release.ps1 -OutputDirectory 'D:/Build/Yomira-0.1.7' `
  -MotorDistribution 'D:/Build/Distribucion-anterior/Yomira' `
  -ISCCPath 'D:/Tools/Inno/ISCC.exe' `
  -Python './backend/.venv/Scripts/python.exe'
```

La salida incluye el bundle con el motor y licencias, el instalador en español
y el ZIP portátil. Publica también `installer/LEEME.txt`, sumas SHA-256 y el
código fuente correspondiente de FMD de la Release anterior: el motor no cambia.
El código de Yomira se identifica por el commit etiquetado en la nueva Release.
Antes de publicar, prueba una instalación aislada y el arranque con raíces de
datos temporales; nunca incluyas bases de datos, cookies, logs o mangas.

El ejecutable configura automáticamente el FMD que tiene junto a él, espera
readiness y abre `/library` en el navegador. La instalación es por usuario en
`%LOCALAPPDATA%/Programs/Yomira`. No requiere elevación y crea accesos directos.
El desinstalador no elimina la biblioteca ni los datos situados fuera del programa.
