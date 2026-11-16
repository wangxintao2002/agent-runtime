"""Keep workspace-path lengths stable across recording and real-tool replay."""
import hashlib
from pathlib import Path


def replay_run_name(preferred, source, runs_root):
    # CLI tools truncate long outputs before the replay comparator sees them.
    # Equal-length paths prevent a different run name shifting that boundary.
    length = len(str(Path(source)/'workspace')) - len(str(Path(runs_root)/'workspace')) - 1
    if not 16 <= length <= 200:
        raise ValueError('Cannot preserve workspace path length with a safe run name')
    digest = hashlib.sha256((preferred+'\0'+str(source)).encode()).hexdigest()[:12]
    prefix = preferred[:length-13]
    return (prefix+'-'+digest).ljust(length, '_')
