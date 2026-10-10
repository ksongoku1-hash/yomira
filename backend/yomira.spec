from pathlib import Path
import sys

from PyInstaller.building.datastruct import Tree


backend_root = Path(SPECPATH).resolve()
repository_root = backend_root.parent
sys.path.insert(0, str(backend_root))

from package_config import discover_package_inputs


data_inputs = discover_package_inputs(repository_root)
wpd_hidden_imports = []
if sys.platform == "win32":
    from app.services.wpd_export import _load_wpd_modules

    _load_wpd_modules()
    wpd_hidden_imports = [
        "comtypes.client",
        "comtypes.gen.PortableDeviceApiLib",
        "comtypes.gen.PortableDeviceTypesLib",
        "comtypes.gen.stdole",
        "comtypes.gen._00020430_0000_0000_C000_000000000046_0_2_0",
        "comtypes.gen._1F001332_1A57_4934_BE31_AFFC99F4EE0A_0_1_0",
        "comtypes.gen._2B00BA2F_E750_4BEB_9235_97142EDE1D3E_0_1_0",
    ]
file_datas = [
    (str(package_input.source), package_input.destination)
    for package_input in data_inputs
    if package_input.source.is_file()
]

a = Analysis(
    [str(backend_root / "app" / "main.py")],
    pathex=[str(backend_root)],
    binaries=[],
    datas=file_datas,
    hiddenimports=wpd_hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(backend_root / "packaging" / "set_production_mode.py")],
    excludes=[],
    noarchive=False,
    optimize=0,
)
for package_input in data_inputs:
    if package_input.source.is_dir():
        a.datas += Tree(str(package_input.source), prefix=package_input.destination)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Yomira",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Yomira",
)
