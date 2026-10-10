# Vista previa offline del flujo de descarga

Este launcher de test support permite probar manualmente la interfaz actual
contra el API real y un trabajo con bytes de imagen deterministas:

`Buscar → DownloadJob → páginas PNG → CBZ → lector`

## Inicio

Usa un entorno Python del proyecto con `backend/requirements.lock` instalado.
En PowerShell, abre dos terminales:

**Terminal 1 — API y fixture**

```powershell
Set-Location <raíz-del-repositorio>\backend
python tests\offline_preview.py
```

El API usa `127.0.0.1:8000`, que coincide con el proxy predeterminado de Vite.
El proceso imprime el origen loopback exacto autorizado y la ruta completa de
su `data_root` aislado.

**Terminal 2 — interfaz**

```powershell
Set-Location <raíz-del-repositorio>\frontend
npm run dev
```

Abre `http://127.0.0.1:5173`, busca `fixture`, selecciona el resultado, elige
el capítulo, crea el trabajo desde la interfaz y ábrelo en el lector cuando
figure como completado.

Para usar otro puerto de API, inicia el backend con `--api-port 8765` y en la
segunda terminal define el proxy explícitamente:

```powershell
$env:YOMIRA_API_PROXY_TARGET = "http://127.0.0.1:8765"
npm run dev
```

## Aislamiento y límites

- Solo registra `yomira.mock`; su búsqueda y metadatos son datos sintéticos en
  memoria. `MockMangaServer` sirve las páginas PNG por HTTP en `127.0.0.1` y un
  puerto efímero. No hay acceso a fuentes reales, bypass de desafíos ni
  dependencia de Internet.
- El allowlist contiene únicamente el origen exacto del fixture. El launcher
  inyecta al transporte de descarga el mismo transporte HTTPX que usa el cliente
  compartido. Si origen o transporte no coinciden, el arranque falla cerrado.
- Los patch de composición existen solo mientras corre este proceso de test
  support. No modifican el runtime normal. El monitor recibe muestras sanas
  deterministas para que esta comprobación del pipeline no dependa de PDH; esta
  vista previa no verifica backpressure ni sus umbrales.
- La aplicación enlaza únicamente a loopback. La vista previa sustituye el
  mutex global de instancia por un guard no-op solo mientras este proceso usa
  `app_env=test` y un `data_root` aislado; el guard rechaza cualquier otro
  entorno. El puerto sigue siendo exclusivo: si el 8000 está ocupado, elige un
  puerto libre y configura `YOMIRA_API_PROXY_TARGET` en Vite.
- Cada ejecución crea un `data_root` único bajo el directorio temporal del SO y
  comprueba que no se cruce con la raíz configurada para la biblioteca normal.
  Imprime su ruta antes de servir y **no lo elimina al salir**; los registros,
  DB, páginas y CBZ de prueba quedan disponibles para inspección. El launcher
  nunca selecciona ni migra la biblioteca del usuario.
- Detén el API con `Ctrl+C`; el contexto cierra el API y el fixture. Vite se
  detiene por separado en su terminal.
