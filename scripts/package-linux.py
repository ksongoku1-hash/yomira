"""Create a Linux preview containing application code, never user data."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "Yomira-0.1.16-Linux-x86_64-preview"


def package() -> Path:
    files: dict[str, bytes] = {}
    for directory in ("backend/app", "frontend/dist", "tools"):
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in ("backend/requirements.lock", "README-LINUX.md", "THIRD_PARTY_NOTICES.md",
                 "Instalar-Yomira.sh", "Abrir-Yomira.sh", "Cerrar-Yomira.sh",
                 "scripts/run-yomira-linux.py", "scripts/open-yomira-linux.py",
                 "scripts/install-linux-desktop.py"):
        data = (ROOT / name).read_bytes()
        files[name] = data.replace(b"\r\n", b"\n") if name.endswith(".sh") else data
    assert files["tools/uv"].startswith(b"\x7fELF"), "A Linux executable is required"
    source = json.loads(files["tools/uv-source.json"])
    assert hashlib.sha256(files["tools/uv"]).hexdigest() == source["binary_sha256"]
    files["SHA256SUMS"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                                 for name, data in sorted(files.items())).encode()
    output = ROOT / "dist-linux" / f"{NAME}.tar.gz"
    output.parent.mkdir(exist_ok=True)
    with output.open("xb") as destination, tarfile.open(fileobj=destination, mode="w:gz") as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(f"{NAME}/{name}")
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") or name == "tools/uv" else 0o644
            archive.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{digest}  {output.name}\n", encoding="ascii",
    )
    print(json.dumps({"artifact": str(output), "bytes": output.stat().st_size,
                      "files": len(files), "sha256": digest}))
    return output


if __name__ == "__main__":
    package()
