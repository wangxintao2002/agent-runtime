"""Download the same pinned ONNX embedding model used by the original recorder."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'common'))
from paths import DATA

MODEL_REPO = 'Qdrant/bge-small-en-v1.5-onnx-Q'
MODEL_REVISION = 'aa8f8b060edb00e03bfdd08813a2949946c8ba55'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=DATA / 'model-cache/pinned-bge-small')
    args = parser.parse_args()
    from huggingface_hub import snapshot_download
    path = Path(snapshot_download(repo_id=MODEL_REPO, revision=MODEL_REVISION,
                                  local_dir=str(args.out)))
    manifest = dict(repo=MODEL_REPO, revision=MODEL_REVISION, files={
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob('*')) if p.is_file() and '.cache' not in p.parts
        and p.name != 'agentos-model-source.json'})
    (path / 'agentos-model-source.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(path.resolve())


if __name__ == '__main__':
    main()
