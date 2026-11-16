"""Restore recorded Git object storage without copying a recorded solution.

Git may encode the same objects into different pack bytes. A tool can observe
that representation. Require the recorded object set to equal the untouched base
repository exactly, then restore only .git/objects into a fresh replay checkout.
Never relax tool-output matching or import the recorded working tree/index.
"""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile


def git(repo,*args):
    return subprocess.check_output(['git','--no-optional-locks','-C',str(repo),*args],text=True).strip()


def logical_objects(repo):
    rows=git(repo,'cat-file','--batch-all-objects','--batch-check=%(objectname) %(objecttype) %(objectsize)').splitlines()
    return sorted(set(rows))


def storage_manifest(objects):
    result={}
    for path in sorted(objects.rglob('*')):
        if path.is_symlink():raise ValueError('Object storage must not contain symlinks')
        if path.is_file():
            if path.name in ('alternates','http-alternates'):
                raise ValueError('External object storage is not supported')
            result[str(path.relative_to(objects))]=dict(bytes=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    if not result:raise ValueError('Empty object storage')
    return result


def restore_object_storage(recorded_repo,base_repo,fresh_repo):
    recorded_repo,base_repo,fresh_repo=[Path(p).resolve() for p in (recorded_repo,base_repo,fresh_repo)]
    if len({recorded_repo,base_repo,fresh_repo})!=3:
        raise ValueError('Recorded evidence and base must be distinct from the fresh checkout')
    expected=git(base_repo,'rev-parse','HEAD')
    if any(git(p,'rev-parse','HEAD')!=expected for p in (recorded_repo,fresh_repo)):
        raise ValueError('Recorded and replay HEAD must equal the untouched base')
    objects=logical_objects(base_repo)
    if not objects or any(logical_objects(p)!=objects for p in (recorded_repo,fresh_repo)):
        raise ValueError('Recorded storage contains a different logical object set')
    source=recorded_repo/'.git/objects';target=fresh_repo/'.git/objects'
    if source.is_symlink() or target.is_symlink():raise ValueError('Object root must be a local directory')
    manifest=storage_manifest(source)
    # Complete copying and validation before touching even this fresh checkout.
    with tempfile.TemporaryDirectory(prefix='replay-object-storage-',dir=fresh_repo.parent) as tmp:
        stage=Path(tmp)/'objects';backup=Path(tmp)/'original'
        shutil.copytree(source,stage)
        if storage_manifest(stage)!=manifest:raise ValueError('Object copy changed bytes')
        target.rename(backup)
        try:stage.rename(target)
        except BaseException:
            backup.rename(target);raise
        if logical_objects(fresh_repo)!=objects or git(fresh_repo,'rev-parse','HEAD')!=expected:
            shutil.rmtree(target);backup.rename(target)
            raise ValueError('Restored repository differs from base')
    return dict(base_commit=expected,logical_objects=len(objects),
        logical_manifest_sha256=hashlib.sha256('\n'.join(objects).encode()).hexdigest(),
        recorded_object_storage=str(source),files=manifest,
        working_tree_copied=False,index_copied=False,tool_output_normalization=False,
        purpose='Reproduce initial Git object representation; no resource-demand or answer hints')
