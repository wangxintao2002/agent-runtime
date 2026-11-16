"""Agent-visible client; retrieval is real, shared, and local."""
import json
import os
from pathlib import Path
import sys
import urllib.request

config = json.loads(Path('connection.json').read_text())
body = json.dumps({'task_id': config['task_id'], 'query': ' '.join(sys.argv[1:]),
                   'run_id': os.environ.get('QA_RUN_ID', 'unspecified')}).encode()
request = urllib.request.Request(config['url'] + '/search', data=body,
                                 headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=120) as response:
    print(json.dumps(json.load(response), ensure_ascii=False, sort_keys=True))
