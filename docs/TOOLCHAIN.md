# Toolchain de desarrollo fijado

**Fecha de fijación:** 2026-09-25.  
**Plataforma:** Windows x64.  
**Distribuciones verificadas localmente:** runtime.

| Elemento | Versión exacta | Distribución | SHA-256 |
|---|---|---|---|
| CPython | 3.14.7 | python-3.14.7-embed-amd64.zip | d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15 |
| SQLite | 3.50.4 | Incluido con CPython 3.14.7 | Parte del runtime Python |
| Node.js | 24.21.0 LTS | node-v24.21.0-win-x64.zip | 158f7685b44de51f6c0df1d153526cbcd3e1bc739a8dfc607721cef75de9e541 |
| npm | 11.19.0 | Incluido con Node.js | Lockfile versión 3 |

Fuentes oficiales:

- [Python 3.14.7 y sumas publicadas](https://www.python.org/downloads/release/python-3147/)
- [Distribuciones Python para Windows](https://docs.python.org/3/using/windows.html)
- [Node 24.21.0 LTS y checksums firmados](https://nodejs.org/en/blog/release/v24.21.0)
- [Versiones LTS de Node.js](https://nodejs.org/en/about/previous-releases)
- [Formato npm package-lock](https://docs.npmjs.com/cli/v11/configuring-npm/package-lock-json/)

El runtime activo está dentro de runtime. Python usa la
distribución embebible con python314._pth habilitado y dependencias fijadas
instaladas en Lib\site-packages; npm y Node también son locales al proyecto.
Los paquetes fuente verificados se conservaron en archive/legacy-development.

runtime-versions.json, .python-version, .node-version y .nvmrc fijan
las versiones. backend/requirements.lock y frontend/package-lock.json
fijan las dependencias. Antes de cambiar cualquier pin, revisa soporte,
compatibilidad y sumas SHA-256.
