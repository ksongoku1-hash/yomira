#!/usr/bin/env bash
set -euo pipefail
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  echo 'Esta versión de prueba requiere Linux x86_64 (Arch / SteamOS).'; exit 1
fi
if [[ $EUID == 0 ]]; then echo 'Ejecuta Yomira con tu usuario, sin sudo.'; exit 1; fi
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if systemctl --user is-active --quiet yomira.service; then
  echo 'Cierra Yomira desde Cerrar Yomira antes de reinstalar.'; exit 1
fi
if [[ -f "$root/SHA256SUMS" ]]; then (cd -- "$root" && sha256sum --quiet -c SHA256SUMS); fi
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
if [[ $data_home != /* ]]; then data_home=$HOME/.local/share; fi
target=$data_home/yomira-app/0.1.16-linux-preview
if [[ $root != "$target" ]]; then
  mkdir -p -- "$target"
  cp -a -- "$root/." "$target/"
  root=$target
fi
echo 'Preparando Python y las dependencias de Yomira en esta carpeta…'
echo 'La primera instalación necesita Internet. No se modificará el sistema base.'
"$root/tools/uv" venv --python 3.14.7 "$root/.venv" --allow-existing
"$root/tools/uv" pip install --python "$root/.venv/bin/python" -r "$root/backend/requirements.lock"
"$root/.venv/bin/python" "$root/scripts/install-linux-desktop.py"
echo 'Listo. Abre Yomira desde el menú de aplicaciones. Usa Cerrar Yomira para detenerla.'
