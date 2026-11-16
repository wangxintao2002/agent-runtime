"""Shared BM25/FAISS service, with real query embeddings and request telemetry.

Uses the prepared independent Wikipedia subset with --shared-index. No cached query results. One worker
is an explicitly fixed calibration setting, not a proposed scheduling policy.
"""
import argparse
from contextlib import nullcontext
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import pickle
from pathlib import Path
import re
import resource
import threading
import time


def tokens(text):
    return re.findall(r'\w+', text.lower())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--port', type=int, default=18761)
    p.add_argument('--shared-index', action='store_true', help='Use the independent prepared corpus for every question')
    args = p.parse_args()
    import faiss
    import numpy as np
    from fastembed import TextEmbedding
    from rank_bm25 import BM25Okapi
    args.out.mkdir(exist_ok=False, parents=True)
    faiss.omp_set_num_threads(2)
    docs = {d['id']: d for d in json.loads((args.data/'documents.json').read_text())}
    tasks = json.loads((args.data/'tasks.json').read_text())
    model = TextEmbedding(model_name='BAAI/bge-small-en-v1.5', threads=2,
                          cache_dir=str(args.data.parent/'model-cache'), local_files_only=True,
                          specific_model_path=str(args.data.parent/'model-cache/pinned-bge-small'))
    identifiers = list(docs)
    indexes = {}
    if args.shared_index:
        manifest=json.loads((args.data/'manifest.json').read_text())
        for name in ['documents.json','faiss.index','bm25.pickle']:
            if hashlib.sha256((args.data/name).read_bytes()).hexdigest()!=manifest['files'][name]:
                raise ValueError('Prepared corpus/index hash mismatch')
        # Only load the locally created, hash-verified pickle, never arbitrary remote pickle.
        with (args.data/'bm25.pickle').open('rb') as f:bm25=pickle.load(f)
        index=faiss.read_index(str(args.data/'faiss.index'))
        shared=(identifiers,bm25,index)
        indexes={task['id']:shared for task in tasks}
    else:
        texts = [docs[i]['title'] + '\n' + ''.join(docs[i]['sentences']) for i in identifiers]
        vectors = np.asarray(list(model.passage_embed(texts)), dtype='float32')
        faiss.normalize_L2(vectors)
        embeddings = dict(zip(identifiers, vectors))
        for task in tasks:
            ids = task['document_ids']
            corpus = [docs[i]['title'] + '\n' + ''.join(docs[i]['sentences']) for i in ids]
            index = faiss.IndexFlatIP(vectors.shape[1])
            index.add(np.asarray([embeddings[i] for i in ids], dtype='float32'))
            indexes[task['id']] = (ids, BM25Okapi([tokens(s) for s in corpus]), index)
    gate, log_lock, stop = threading.Lock(), threading.Lock(), threading.Event()
    def log(name, data):
        with log_lock:
            with (args.out/name).open('a') as f:
                f.write(json.dumps(data, ensure_ascii=False)+'\n')
    def observe():
        while not stop.is_set():
            usage = resource.getrusage(resource.RUSAGE_SELF)
            stat = Path('/proc/self/statm').read_text().split()
            log('resources.jsonl', dict(monotonic_s=time.monotonic(),
                cpu_s=usage.ru_utime+usage.ru_stime, rss_bytes=int(stat[1])*4096,
                maxrss_kib=usage.ru_maxrss))
            stop.wait(.2)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            arrived = time.monotonic()
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if self.path != '/search' or not 0 < size <= 16384:
                    raise ValueError('Invalid request')
                request = json.loads(self.rfile.read(size))
                task_id, query = request['task_id'], request['query']
                if task_id not in indexes or not isinstance(query, str) or not query.strip():
                    raise ValueError('Invalid task/query')
                with (nullcontext() if args.shared_index else gate):
                    started, cpu = time.monotonic(), time.process_time()
                    ids, bm25, index = indexes[task_id]
                    stages={};stage_start=time.monotonic()
                    sparse = bm25.get_scores(tokens(query))
                    sparse_order = (list(np.lexsort((np.arange(len(ids)), -sparse))[:100]) if args.shared_index
                        else sorted(range(len(ids)), key=lambda i: (-float(sparse[i]), ids[i])))
                    now=time.monotonic();stages['sparse_s']=now-stage_start;stage_start=now
                    vector = np.asarray(list(model.query_embed([query])), dtype='float32')
                    faiss.normalize_L2(vector)
                    now=time.monotonic();stages['query_embedding_s']=now-stage_start;stage_start=now
                    faiss.omp_set_num_threads(2)
                    distances, positions = index.search(vector, min(100,len(ids)) if args.shared_index else len(ids))
                    now=time.monotonic();stages['dense_search_s']=now-stage_start;stage_start=now
                    dense_scores = {int(i):float(s) for i,s in zip(positions[0],distances[0])}
                    dense_order = sorted(dense_scores, key=lambda i: (-dense_scores[i], ids[i]))
                    ranks = [{i:rank+1 for rank,i in enumerate(order)} for order in (sparse_order,dense_order)]
                    candidates=set(sparse_order)|set(dense_order)
                    scores = {i:sum(1/(60+r[i]) for r in ranks if i in r) for i in candidates}
                    selected = sorted(scores, key=lambda i: (-scores[i],ids[i]))[:5]
                    result = {'results': [{'title': docs[ids[i]]['title'],
                        'sentences': [[j,s] for j,s in enumerate(docs[ids[i]]['sentences'])]} for i in selected]}
                    body = json.dumps(result, ensure_ascii=False, sort_keys=True).encode()
                    stages['fusion_serialization_s']=time.monotonic()-stage_start
                    log('requests.jsonl', dict(task_id=task_id, run_id=request.get('run_id'),
                        query=query, arrived_s=arrived, started_s=started, ended_s=time.monotonic(),
                        stages=stages,
                        process_cpu_delta_s_not_additive=time.process_time()-cpu, queue_s=started-arrived,
                        response_sha256=hashlib.sha256(body).hexdigest(), document_ids=[ids[i] for i in selected]))
                self.send_response(200)
            except Exception as exc:
                body = json.dumps({'error':type(exc).__name__}).encode()
                log('errors.jsonl',dict(monotonic_s=time.monotonic(), error=type(exc).__name__))
                self.send_response(400)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    class Service(ThreadingHTTPServer):
        # Make the listener capacity explicit; a five-connection default queue
        # must not masquerade as retrieval compute saturation in burst tests.
        request_queue_size=128
    server = Service(('127.0.0.1', args.port), Handler)
    threading.Thread(target=observe, daemon=True).start()
    cache = args.data.parent/'model-cache'
    (args.out/'ready.json').write_text(json.dumps(dict(url=f'http://127.0.0.1:{args.port}',
        corpus_scope='independent Wikipedia subset; shared index for all questions' if args.shared_index else 'official distractor, ten candidates per task; calibration only',
        tasks=len(tasks), documents=len(docs), model='BAAI/bge-small-en-v1.5',
        embedding_threads=2, faiss_threads=2, service_workers='concurrent requests, no admission gate' if args.shared_index else 1,
        socket_listen_backlog=128,
        fusion='reciprocal rank, k=60, top5; top100 from each retriever' if args.shared_index else 'reciprocal rank, k=60, top5; all candidate ranks',
        model_files={str(f.relative_to(cache)):hashlib.sha256(f.read_bytes()).hexdigest()
                     for f in cache.rglob('*') if f.is_file()},
        data_files={f.name:hashlib.sha256(f.read_bytes()).hexdigest()
                    for f in args.data.glob('*.json') if 'gold' not in f.name}),indent=2)+'\n')
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
