"""Private bounded diagnostic checkpoint; never a data source or trading proof.

Only physical candle keys and SHA256 digests are retained, not OHLCV prices.
A partial read cannot replace the last complete checkpoint. The market DB is
always query-only; this disposable cache uses a separate SQLite file.
"""
import hashlib,json,math,sqlite3,threading,time,zlib
from datetime import datetime,timezone
from pathlib import Path

SCHEMA=1
MAX_BLOB=24*1024*1024
MAX_RAW=128*1024*1024
MAX_STORE=512*1024*1024
MAX_METADATA_RAW=64*1024*1024
MAX_METADATA_BLOB=32*1024*1024

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()

def invalid(values):
    try:
        o,h,l,c,v=map(float,values)
        return not all(map(math.isfinite,(o,h,l,c,v))) or min(o,h,l,c)<=0 or v<0 or h<max(o,l,c) or l>min(o,h,c)
    except (ValueError,TypeError,OverflowError):return True

def capture(session,isin,seconds=3.0):
    from cp_bridge_comparison import rows
    deadline=time.monotonic()+seconds
    manifest={};bad=set();ambiguous=set();by_tf={};examples=[]
    for n,row in enumerate(rows(session.db,session.tables,isin,None)):
        if n%1024==0 and time.monotonic()>deadline:raise TimeoutError('MANIFEST_READ_BUDGET')
        key=json.dumps(row[:6],separators=(',',':'),ensure_ascii=True)
        # SQLite INTEGER/REAL values with equal numeric value compare equally.
        values=tuple(float(x) if isinstance(x,(int,float)) else x for x in row[6:])
        value=hashlib.sha256(repr(values).encode()).hexdigest()
        previous=manifest.get(key)
        if previous is not None and previous!=value:ambiguous.add(key)
        manifest.setdefault(key,value)
        if invalid(row[6:]):
            bad.add(key)
            if len(examples)<8:examples.append(dict(identity=list(row[:4]),timeframe=row[4],at=row[5],reason='IMPOSSIBLE_OHLCV'))
    for key in bad:
        tf=json.loads(key)[4];by_tf[tf]=by_tf.get(tf,0)+1
    raw=encoded(dict(rows=manifest,ambiguous=sorted(ambiguous)))
    if len(raw)>MAX_RAW:raise ValueError('MANIFEST_SIZE_LIMIT')
    blob=zlib.compress(raw,1)
    if len(blob)>MAX_BLOB:raise ValueError('MANIFEST_SIZE_LIMIT')
    integrity=dict(status='EXACT',confirmed_structural_errors=len(bad),ambiguous_physical_keys=len(ambiguous),by_tf=by_tf,examples=examples,
        unit='Unique physical keys containing impossible numeric/OHLC/volume values; zero volume is valid.',
        source_accuracy='NOT_CERTIFIED_BY_STRUCTURAL_CHECK')
    return blob,integrity

def decode(blob):
    if not isinstance(blob,bytes) or len(blob)>MAX_BLOB:raise ValueError('MANIFEST_SIZE_LIMIT')
    loader=zlib.decompressobj();raw=loader.decompress(blob,MAX_RAW+1)
    if len(raw)>MAX_RAW or not loader.eof or loader.unused_data:raise ValueError('INVALID_MANIFEST')
    value=json.loads(raw)
    if not isinstance(value,dict) or not isinstance(value.get('rows'),dict) or not isinstance(value.get('ambiguous'),list):raise ValueError('INVALID_MANIFEST')
    return value

class Checkpoint:
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock();self.problem=None
        self.db=sqlite3.connect(self.path,timeout=.05,check_same_thread=False)
        try:self.db.executescript('CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY,payload BLOB NOT NULL); CREATE TABLE IF NOT EXISTS entries(revision TEXT,isin TEXT,hash TEXT NOT NULL,integrity TEXT NOT NULL,PRIMARY KEY(revision,isin)); CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,hash TEXT NOT NULL);')
        except Exception:
            self.db.close();self.db=None;raise
    @staticmethod
    def binding(path):return hashlib.sha256(str(Path(path).resolve()).casefold().encode()).hexdigest()
    def get(self,revision,isin):
        with self.lock:
            row=self.db.execute('SELECT b.payload,e.integrity,e.hash FROM entries e JOIN blobs b ON b.hash=e.hash WHERE revision=? AND isin=?',(revision,isin)).fetchone()
        if row is None:return None
        blob=bytes(row[0])
        if hashlib.sha256(blob).hexdigest()!=row[2]:raise ValueError('MANIFEST_CHECKSUM_MISMATCH')
        return blob,json.loads(row[1])
    def put(self,revision,isin,blob,integrity):
        if len(blob)>MAX_BLOB:raise ValueError('MANIFEST_SIZE_LIMIT')
        with self.lock,self.db:
            page=self.db.execute('PRAGMA page_size').fetchone()[0]
            used=self.db.execute('PRAGMA page_count').fetchone()[0]-self.db.execute('PRAGMA freelist_count').fetchone()[0]
            if page*used+len(blob)>MAX_STORE:raise ValueError('CHECKPOINT_STORAGE_LIMIT')
            key=hashlib.sha256(blob).hexdigest()
            self.db.execute('INSERT OR REPLACE INTO blobs VALUES (?,?)',(key,blob))
            self.db.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?)',(revision,isin,key,json.dumps(integrity)))
    def metadata(self):
        with self.lock:row=self.db.execute('SELECT payload,hash FROM checkpoint WHERE id=1').fetchone()
        if row is None:return None
        payload=row[0]
        if isinstance(payload,str):
            raw=payload.encode()
            if len(raw)>MAX_METADATA_RAW:raise ValueError('CHECKPOINT_SIZE_LIMIT')
            if hashlib.sha256(raw).hexdigest()!=row[1]:raise ValueError('CHECKPOINT_CHECKSUM_MISMATCH')
        else:
            blob=bytes(payload)
            if len(blob)>MAX_METADATA_BLOB:raise ValueError('CHECKPOINT_SIZE_LIMIT')
            if hashlib.sha256(blob).hexdigest()!=row[1]:raise ValueError('CHECKPOINT_CHECKSUM_MISMATCH')
            try:
                loader=zlib.decompressobj();raw=loader.decompress(blob,MAX_METADATA_RAW+1)
            except zlib.error as error:raise ValueError('INVALID_CHECKPOINT') from error
            if len(raw)>MAX_METADATA_RAW or not loader.eof or loader.unused_data:raise ValueError('CHECKPOINT_SIZE_LIMIT')
        saved=json.loads(raw)
        if not isinstance(saved,dict):raise ValueError('INVALID_CHECKPOINT')
        return saved
    def load(self,path):
        saved=self.metadata()
        if saved is None:return None
        if saved.get('schema')!=SCHEMA or saved.get('binding')!=self.binding(path) or saved.get('status')!='COMPLETE':raise ValueError('CHECKPOINT_BINDING_MISMATCH')
        baseline=saved['baseline']
        if not isinstance(baseline,dict) or not isinstance(baseline.get('scope'),list) or not isinstance(baseline.get('entities'),dict):raise ValueError('INVALID_CHECKPOINT')
        at=datetime.fromisoformat(baseline['generated_at'])
        if at.tzinfo is None or at>datetime.now(timezone.utc):raise ValueError('CHECKPOINT_FROM_FUTURE')
        baseline['unverified']={bytes.fromhex(v) for v in baseline['unverified']}
        baseline.update(view=None,checkpoint=self,loaded_from_disk=True)
        with self.lock:
            got=self.db.execute('SELECT count(*) FROM entries WHERE revision=?',(baseline['revision'],)).fetchone()[0]
        if got!=len(baseline['scope']):raise ValueError('CHECKPOINT_INCOMPLETE')
        return baseline
    def publish(self,path,baseline):
        value={k:v for k,v in baseline.items() if k not in ('view','checkpoint','loaded_from_disk')}
        value['unverified']=sorted(v.hex() for v in value['unverified'])
        raw=json.dumps(dict(schema=SCHEMA,binding=self.binding(path),status='COMPLETE',baseline=value),sort_keys=True,separators=(',',':'))
        raw=raw.encode()
        if len(raw)>MAX_METADATA_RAW:raise ValueError('CHECKPOINT_SIZE_LIMIT')
        payload=zlib.compress(raw,1)
        if len(payload)>MAX_METADATA_BLOB:raise ValueError('CHECKPOINT_SIZE_LIMIT')
        with self.lock,self.db:
            page=self.db.execute('PRAGMA page_size').fetchone()[0]
            used=self.db.execute('PRAGMA page_count').fetchone()[0]-self.db.execute('PRAGMA freelist_count').fetchone()[0]
            if page*used+len(payload)>MAX_STORE:raise ValueError('CHECKPOINT_STORAGE_LIMIT')
            count=self.db.execute('SELECT count(*) FROM entries WHERE revision=?',(value['revision'],)).fetchone()[0]
            if count!=len(value['scope']):raise ValueError('CHECKPOINT_INCOMPLETE')
            self.db.execute('INSERT OR REPLACE INTO checkpoint VALUES (1,?,?)',(payload,hashlib.sha256(payload).hexdigest()))
    def prune(self,keep):
        with self.lock,self.db:
            try:
                saved=self.metadata()
                keep=set(keep)|({saved['baseline']['revision']} if saved else set())
            except (ValueError,KeyError,TypeError):keep=set(keep)
            for revision, in self.db.execute('SELECT DISTINCT revision FROM entries').fetchall():
                if revision not in keep:self.db.execute('DELETE FROM entries WHERE revision=?',(revision,))
            self.db.execute('DELETE FROM blobs WHERE NOT EXISTS(SELECT 1 FROM entries e WHERE e.hash=blobs.hash)')
    def close(self):
        with self.lock:
            if self.db is not None:self.db.close();self.db=None

def compare(current,previous,isin):
    unknown=dict(status='UNKNOWN',new_unique_physical_ohlcv=None,corrected_unique_physical_ohlcv=None,removed_unique_physical_ohlcv=None)
    if not previous:return dict(unknown,reason='NO_PREVIOUS_COMPLETE_SNAPSHOT')
    expected=sorted((i['isin'],i.get('kind','stock')) for i in current.infos)
    if sorted(map(tuple,previous['scope']))!=expected:return dict(unknown,reason='CATALOG_SCOPE_CHANGED')
    cache=current.checkpoint
    try:
        a=cache.get(previous['revision'],isin);b=cache.get(current.revision,isin)
        if a is None or b is None:return dict(unknown,reason='MANIFEST_NOT_AVAILABLE')
        if a[0]==b[0] and current.manifest_integrities.get(isin,{}).get('ambiguous_physical_keys')==0:
            return dict(status='EXACT',from_revision=previous['revision'],to_revision=current.revision,new_unique_physical_ohlcv=0,corrected_unique_physical_ohlcv=0,removed_unique_physical_ohlcv=0,ambiguous_physical_keys=0,by_tf={},basis='Identical independently captured physical-key/SHA256 manifests.')
        aa=decode(a[0]);bb=decode(b[0]);old=aa['rows'];new=bb['rows'];ambiguous=set(aa['ambiguous'])|set(bb['ambiguous'])
        groups={};added=corrected=removed=0;deadline=time.monotonic()+3
        for n,key in enumerate(old.keys()|new.keys()):
            if n%1024==0 and time.monotonic()>deadline:raise TimeoutError('MANIFEST_READ_BUDGET')
            tf=json.loads(key)[4];r=groups.setdefault(tf,dict(new=0,corrected=0,removed=0,ambiguous=0))
            if key in ambiguous:r['ambiguous']+=1;continue
            if key not in old:r['new']+=1;added+=1
            elif key not in new:r['removed']+=1;removed+=1
            elif old[key]!=new[key]:r['corrected']+=1;corrected+=1
        return dict(status='EXACT_WITH_AMBIGUOUS_KEYS' if ambiguous else 'EXACT',from_revision=previous['revision'],to_revision=current.revision,
            new_unique_physical_ohlcv=added,corrected_unique_physical_ohlcv=corrected,removed_unique_physical_ohlcv=removed,
            ambiguous_physical_keys=len(ambiguous),by_tf=groups,basis='Persisted full physical-key/SHA256 manifests; no DQ, journal or row-count estimates.')
    except TimeoutError:return dict(unknown,reason='MANIFEST_READ_BUDGET')
    except (ValueError,TypeError,KeyError,zlib.error,sqlite3.Error):return dict(unknown,reason='MANIFEST_UNAVAILABLE')
