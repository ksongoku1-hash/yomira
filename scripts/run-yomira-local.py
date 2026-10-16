"""Run the local Yomira task with paths resolved from this project tree."""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'backend'))
os.chdir(PROJECT_ROOT / 'backend')
os.environ.update({
    'YOMIRA_DATA_ROOT': str(PROJECT_ROOT / 'user-data/AppData/Yomira/YomiraData'),
    'YOMIRA_LIBRARY_ROOT': str(PROJECT_ROOT / 'user-data/libraries/Mangas'),
    'YOMIRA_PYTHON_EXE': str(PROJECT_ROOT / 'runtime/python-3.14.7/python.exe'),
    'YOMIRA_NODE_EXE': str(PROJECT_ROOT / 'runtime/node-24.21.0/node.exe'),
    'YOMIRA_NPM_CMD': str(PROJECT_ROOT / 'runtime/node-24.21.0/npm.cmd'),
    'YOMIRA_FMD2_EXECUTABLE': str(PROJECT_ROOT / 'user-data/fmd2-runtime/fmd.exe'),
    'YOMIRA_API_HOST': '127.0.0.1',
    'YOMIRA_API_PORT': '5173',
    'YOMIRA_APP_ENV': 'prod',
})
from app.main import main
main()