"""Resolve reviewed SWE environment paths relative to the specification file."""
import json
import os
from pathlib import Path


def load_spec(path):
    path = Path(path).expanduser().resolve()
    spec = json.loads(path.read_text())
    for key in ('task_file', 'source', 'python'):
        value = Path(spec[key]).expanduser()
        spec[key] = os.path.abspath(value if value.is_absolute() else path.parent / value)
    if 'conda_executable' in spec and '/' in spec['conda_executable']:
        value = Path(spec['conda_executable']).expanduser()
        spec['conda_executable'] = str(value if value.is_absolute() else path.parent / value)
    return spec
