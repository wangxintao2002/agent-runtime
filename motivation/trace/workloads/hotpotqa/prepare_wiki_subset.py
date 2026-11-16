"""Prepare an independent Wikipedia subset for real retrieval tools.

Corpus selection never reads questions/answers. Fixed first 100k abstracts;
archive order bias and subset scope are explicit, not fullwiki reproduction.
"""
import argparse
import bz2
import hashlib
import io
import json
from pathlib import Path
import pickle
import re
import tarfile
import time
import sys
import urllib.request

HERE=Path(__file__).resolve().parent
COMMON = HERE.parents[1] / 'common'
sys.path.insert(0, str(COMMON))
from paths import DATA, run_root
RUNS = run_root('hotpotqa')
WIKI_URL='https://nlp.stanford.edu/projects/hotpotqa/enwiki-20171001-pages-meta-current-withlinks-abstracts.tar.bz2'
EXPECTED_BYTES=1553565403
EXPECTED_MD5='01edf64cd120ecc03a2745352779514c'


def save(path,value):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def download(url,path,expected_bytes=None,expected_md5=None):
    digest=hashlib.sha256();md5=hashlib.md5();size=0
    if path.exists():
        source=path.open('rb');target=None
    else:
        source=urllib.request.urlopen(url,timeout=120);target=path.with_suffix(path.suffix+'.partial').open('wb')
    try:
        while chunk:=source.read(2**20):
            size+=len(chunk);digest.update(chunk);md5.update(chunk)
            if target:target.write(chunk)
    finally:
        source.close()
        if target:target.close()
    if expected_bytes is not None and size!=expected_bytes:raise ValueError('Archive size mismatch')
    if expected_md5 is not None and md5.hexdigest()!=expected_md5:raise ValueError('Archive MD5 mismatch')
    if target:path.with_suffix(path.suffix+'.partial').rename(path)
    return dict(url=url,bytes=size,sha256=digest.hexdigest(),md5=md5.hexdigest())


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--name',default='wiki100k-hotpot128')
    p.add_argument('--documents',type=int,default=100000)
    p.add_argument('--questions',type=int,default=128)
    p.add_argument('--resume-before-documents',action='store_true',help='Retry download/extraction only; refuse to overwrite prepared documents/indexes')
    args=p.parse_args()
    if args.documents<1 or args.questions<1 or not args.name.replace('-','').isalnum():p.error('Invalid name/count')
    out=DATA/args.name
    if args.resume_before_documents:
        if not out.is_dir() or (out/'documents.json').exists():raise ValueError('Resume only an existing preparation before documents are written')
    else:out.mkdir(parents=True, exist_ok=False)
    def status(phase,**extra):save(out/'status.json',dict(phase=phase,updated_s=time.time(),**extra))
    status('download')
    archive=DATA / "wiki-intro-2017.tar.bz2"
    evidence=download(WIKI_URL,archive,EXPECTED_BYTES,EXPECTED_MD5)
    save(out/'archive-source.json',evidence)
    status('extract_independent_subset')
    docs=[];seen=set()
    with tarfile.open(archive,'r|bz2') as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith('.bz2'):continue
            # Python's streaming tar ExFileObject does not provide seekable();
            # individual compressed wiki shards are small, so buffer one shard.
            with bz2.open(io.BytesIO(tar.extractfile(member).read()),'rt',encoding='utf-8') as rows:
                for line in rows:
                    row=json.loads(line);text=row['text']
                    if text and isinstance(text[0],list):text=[s for paragraph in text for s in paragraph]
                    if not text or not all(isinstance(s,str) for s in text):continue
                    identity=str(row['id'])
                    if identity in seen:continue
                    seen.add(identity);docs.append(dict(id=identity,title=row['title'],sentences=text))
                    if len(docs)>=args.documents:break
            if len(docs)>=args.documents:break
    if len(docs)!=args.documents:raise ValueError('Insufficient documents')
    save(out/'documents.json',docs)
    # Freeze questions independently, after the corpus is already fixed.
    import pyarrow.parquet as pq
    url='https://huggingface.co/datasets/hotpotqa/hotpot_qa/resolve/1908d6afbbead072334abe2965f91bd2709910ab/fullwiki/validation-00000-of-00001.parquet'
    questions=DATA / "hotpot_fullwiki_validation.parquet"
    question_source=download(url,questions)
    rows=pq.read_table(questions).to_pylist()
    if args.questions>len(rows):raise ValueError('Question count exceeds dataset')
    selected=sorted(rows,key=lambda r:hashlib.sha256(('shared-wiki-20261019:'+r['id']).encode()).hexdigest())[:args.questions]
    save(out/'tasks.json',[dict(id=r['id'],question=r['question']) for r in selected])
    # No gold is required by the current collection protocol.
    status('build_embeddings',documents=len(docs),completed=0)
    import faiss
    import numpy as np
    from fastembed import TextEmbedding
    from rank_bm25 import BM25Okapi
    model=TextEmbedding(model_name='BAAI/bge-small-en-v1.5',threads=8,local_files_only=True,
        specific_model_path=str(DATA / "model-cache/pinned-bge-small"),cache_dir=str(DATA / "model-cache"))
    matrix=np.lib.format.open_memmap(out/'embeddings.npy',mode='w+',dtype='float32',shape=(len(docs),384))
    texts=[d['title']+'\n'+''.join(d['sentences']) for d in docs]
    started=time.monotonic()
    for offset in range(0,len(docs),256):
        vectors=np.asarray(list(model.passage_embed(texts[offset:offset+256],batch_size=64)),dtype='float32')
        faiss.normalize_L2(vectors);matrix[offset:offset+len(vectors)]=vectors
        if offset%2048==0:
            matrix.flush();status('build_embeddings',documents=len(docs),completed=offset+len(vectors),elapsed_s=time.monotonic()-started)
    matrix.flush()
    status('build_indexes',documents=len(docs))
    index=faiss.IndexFlatIP(384);index.add(np.asarray(matrix));faiss.write_index(index,str(out/'faiss.index'))
    bm25=BM25Okapi([re.findall(r'\w+',text.lower()) for text in texts])
    with (out/'bm25.pickle').open('wb') as f:pickle.dump(bm25,f)
    save(out/'manifest.json',dict(corpus='official HotpotQA Wikipedia introductory paragraphs, 2017-10-01',
        corpus_selection=f'first {len(docs)} distinct documents in official archive order, independent of queries/answers',
        corpus_scope='independent subset; NOT complete fullwiki; archive order bias remains',
        document_count=len(docs),question_count=len(selected),question_source=question_source,
        task_selection=f'first{args.questions} SHA256(shared-wiki-20261019:+id); no answer/resource filtering',
        model='BAAI/bge-small-en-v1.5',model_revision='aa8f8b060edb00e03bfdd08813a2949946c8ba55',
        build_threads=8,dimension=384,
        files={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in out.iterdir() if f.is_file() and f.name!='status.json'}))
    status('complete',documents=len(docs),questions=len(selected))


if __name__=='__main__':main()
