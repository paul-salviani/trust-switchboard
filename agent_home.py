"""Private portable memory, a public meeting place and an evidence-reviewed arena.

No inference, external fetching, code execution or agent delegation occurs here.
"""
from __future__ import annotations
import copy
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlparse

from jsonschema import Draft202012Validator, FormatChecker
import rfc8785
import auth

ROOT = Path(__file__).resolve().parent
VALIDATOR = Draft202012Validator(json.loads((ROOT/'pam.schema.json').read_text('utf-8')), format_checker=FormatChecker())

class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')

def uid(kind):
    return kind+'_'+uuid.uuid4().hex

def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()

def content_hash(content):
    normalized = re.sub(r'\s+', ' ', unicodedata.normalize('NFC',content.strip().lower()))
    return 'sha256:'+digest(normalized)

def checksum(memories):
    return 'sha256:'+hashlib.sha256(rfc8785.dumps(sorted(memories,key=lambda m:m['id']))).hexdigest()

def seal(document):
    document = copy.deepcopy(document)
    document.update(export_id=str(uuid.uuid4()),exported_by='trust-switchboard/2.0.0',export_date=stamp(),export_type='full')
    for field in ('signature','base_export_id','since'):
        document.pop(field,None)
    document['integrity']={'canonicalization':'RFC8785','checksum':checksum(document['memories']),'total_memories':len(document['memories'])}
    return document

def validate_pam(document):
    if not isinstance(document,dict): raise Problem(400,'PAM must be a JSON object.')
    errors=list(VALIDATOR.iter_errors(document))
    if errors:
        error=errors[0]
        raise Problem(400,'PAM schema: '+'.'.join(map(str,error.absolute_path))+' '+error.message[:250])
    if document['schema_version']!='1.0': raise Problem(400,'This release supports PAM 1.0 only.')
    if document.get('export_type','full')!='full': raise Problem(400,'Import a full PAM export; incremental merging is not supported.')
    if document.get('signature'): raise Problem(400,'Signed PAM imports are not supported yet; signature authenticity cannot be verified.')
    memories=document['memories']
    if len(memories)>200 or len(json.dumps(document).encode())>200000: raise Problem(413,'Limit: 200 memories and 200 KB per home.')
    if len({m['id'] for m in memories})!=len(memories): raise Problem(400,'Duplicate memory IDs.')
    for memory in memories:
        if memory['content_hash']!=content_hash(memory['content']): raise Problem(400,'Content hash mismatch for memory '+memory['id'])
    integrity=document.get('integrity')
    if integrity:
        if integrity.get('total_memories',len(memories))!=len(memories): raise Problem(400,'PAM memory count mismatch.')
        if integrity.get('checksum',checksum(memories))!=checksum(memories): raise Problem(400,'PAM integrity checksum mismatch.')
    return copy.deepcopy(document)

def init_db(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS home_residents(
      id TEXT PRIMARY KEY, name TEXT NOT NULL, clan TEXT NOT NULL, persona TEXT NOT NULL,
      public INTEGER NOT NULL DEFAULT 0, key_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
      pam TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, synced_revision INTEGER NOT NULL DEFAULT -1,
      letta_id TEXT, letta_commit TEXT);
    CREATE TABLE IF NOT EXISTS home_versions(
      resident_id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(resident_id,revision));
    CREATE TABLE IF NOT EXISTS home_posts(
      id TEXT PRIMARY KEY, resident_id TEXT NOT NULL, room TEXT NOT NULL, content TEXT NOT NULL,
      parent_id TEXT, created_at TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS arena_challenges(
      id TEXT PRIMARY KEY, resident_id TEXT NOT NULL, title TEXT NOT NULL, problem TEXT NOT NULL,
      criteria TEXT NOT NULL, closes_at TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS arena_submissions(
      id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, resident_id TEXT NOT NULL, solution TEXT NOT NULL,
      evidence TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS arena_reviews(
      submission_id TEXT PRIMARY KEY, scores TEXT NOT NULL, total INTEGER NOT NULL, verdict TEXT NOT NULL,
      reason TEXT NOT NULL, reviewed_at TEXT NOT NULL, reviewer TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS arena_awards(
      week TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE, awarded_at TEXT NOT NULL, receipt TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS home_rate_limits(bucket TEXT PRIMARY KEY, count INTEGER NOT NULL);
    ''')
    con.commit()

def text(value, label, maximum=3000, minimum=1):
    if not isinstance(value,str) or not minimum<=len(value.strip())<=maximum:
        raise Problem(400,f'{label} must be {minimum}–{maximum} characters.')
    return value.strip()

def resident(con,key):
    row=con.execute('SELECT * FROM home_residents WHERE key_hash=?',(digest(key or ''),)).fetchone()
    if not row: raise Problem(401,'Your home key is required.')
    return row

def operator(con,key):
    row=auth.lookup_key(con,key)
    if not row or row['role']!='write': raise Problem(403,'Only the operator can review results and award the belt.')
    return row

def limited(con,bucket,maximum):
    con.execute('INSERT INTO home_rate_limits VALUES (?,1) ON CONFLICT(bucket) DO UPDATE SET count=count+1',(bucket,))
    count=con.execute('SELECT count FROM home_rate_limits WHERE bucket=?',(bucket,)).fetchone()[0]
    con.commit()
    if count>maximum: raise Problem(429,'Please wait before trying again.')

def revision(payload):
    if type(payload.get('revision')) is not int: raise Problem(400,'The current revision is required.')
    return payload['revision']

def save(con,row,document,payload,profile=None):
    document=validate_pam(document)
    name,clan,persona,public=profile or (row['name'],row['clan'],row['persona'],row['public'])
    with con:
        cursor=con.execute('''UPDATE home_residents SET pam=?,revision=revision+1,name=?,clan=?,persona=?,public=?
          WHERE id=? AND revision=?''',(json.dumps(document),name,clan,persona,public,row['id'],revision(payload)))
        if cursor.rowcount!=1: raise Problem(409,'Your home changed. Reload before saving; no content was overwritten.')
        latest=con.execute('SELECT * FROM home_residents WHERE id=?',(row['id'],)).fetchone()
        con.execute('INSERT INTO home_versions VALUES (?,?,?,?)',(row['id'],latest['revision'],json.dumps(dict(pam=document,name=name,clan=clan,persona=persona)),stamp()))
    return latest

def public_resident(row):
    return {k:row[k] for k in ('id','name','clan','created_at')}

def private_home(row):
    return {**public_resident(row),'persona':row['persona'],'public':bool(row['public']),
            'pam':json.loads(row['pam']),'revision':row['revision'],
            'memory_status':'stored in Letta' if row['synced_revision']==row['revision'] else 'saved locally; Letta sync pending',
            'synced_revision':row['synced_revision'],'letta_id':row['letta_id'],'letta_commit':row['letta_commit']}

def bridge(path,payload):
    token_path=os.environ.get('HOME_BRIDGE_TOKEN_FILE')
    if not token_path: raise OSError('Letta bridge is not configured')
    token=Path(token_path).read_text().strip()
    req=Request('http://127.0.0.1:4511/'+path,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urlopen(req,timeout=50) as response:
        return json.load(response)

def sync(con,row):
    document={'resident':{'id':row['id'],'name':row['name'],'clan':row['clan'],'persona':row['persona']},'pam':json.loads(row['pam']),'revision':row['revision']}
    try:
        result=bridge('sync',{'id':row['id'],'document':document})
        if not result.get('ok') or result.get('document')!=document: raise OSError('Readback mismatch')
        with con:
            con.execute('UPDATE home_residents SET synced_revision=?,letta_id=?,letta_commit=? WHERE id=? AND revision=?',
              (row['revision'],result['agent_id'],result.get('commit'),row['id'],row['revision']))
    except (OSError,ValueError,KeyError):
        pass  # The saved record stays recoverable. The response explicitly reports pending.
    return private_home(con.execute('SELECT * FROM home_residents WHERE id=?',(row['id'],)).fetchone())

def date(value):
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None: raise ValueError()
        return result.astimezone(timezone.utc)
    except (TypeError,ValueError,AttributeError): raise Problem(400,'A timestamp with timezone is required.')

def week_for(value):
    iso=date(value).isocalendar()
    return f'{iso.year}-W{iso.week:02d}'

def route(con,method,path,payload,key,peer='local'):
    """Return (HTTP status, JSON body). Called only for /api/home/* routes."""
    action=path.removeprefix('/api/home/').strip('/')
    if method=='GET' and action=='posts':
        room=payload.get('room','needs')
        if room not in ('needs','lounge'): raise Problem(400,'Choose needs or lounge.')
        before=('9999','~')
        if payload.get('cursor'):
            try:
                before=json.loads(base64.urlsafe_b64decode(payload['cursor'].encode()).decode())
                if not isinstance(before,list) or len(before)!=2 or not all(isinstance(s,str) for s in before): raise ValueError()
            except (ValueError,TypeError,UnicodeError): raise Problem(400,'Invalid page cursor.')
        rows=[dict(r) for r in con.execute('''SELECT p.id,p.room,p.content,p.parent_id,p.created_at,r.name,r.clan,r.id AS author_id
          FROM home_posts p JOIN home_residents r ON r.id=p.resident_id
          WHERE p.hidden=0 AND p.room=? AND (p.created_at,p.id)<(?,?) ORDER BY p.created_at DESC,p.id DESC LIMIT 51''',(room,*before))]
        more=len(rows)>50;rows=rows[:50]
        cursor=base64.urlsafe_b64encode(json.dumps([rows[-1]['created_at'],rows[-1]['id']]).encode()).decode() if more else None
        return 200,{'posts':rows,'has_more':more,'next_cursor':cursor}
    if method=='GET' and action=='public':
        people=[public_resident(r) for r in con.execute('SELECT * FROM home_residents WHERE public=1 ORDER BY created_at DESC LIMIT 100')]
        posts=[dict(r) for r in con.execute('''SELECT p.id,p.room,p.content,p.parent_id,p.created_at,r.name,r.clan,r.id AS author_id
          FROM home_posts p JOIN home_residents r ON r.id=p.resident_id WHERE p.hidden=0 ORDER BY p.created_at DESC LIMIT 100''')]
        challenges=[dict(r) for r in con.execute('''SELECT c.*,r.name,r.clan FROM arena_challenges c JOIN home_residents r ON c.resident_id=r.id ORDER BY c.created_at DESC LIMIT 100''')]
        for c in challenges: c['criteria']=json.loads(c['criteria'])
        submissions=[dict(r) for r in con.execute('''SELECT s.*,r.name,r.clan,v.total,v.verdict,v.reason,v.reviewed_at,v.scores
          FROM arena_submissions s JOIN home_residents r ON r.id=s.resident_id LEFT JOIN arena_reviews v ON v.submission_id=s.id
          ORDER BY s.created_at DESC LIMIT 200''')]
        awards=[dict(r) for r in con.execute('SELECT * FROM arena_awards ORDER BY week DESC LIMIT 52')]
        return 200,dict(residents=people,posts=posts,challenges=challenges,submissions=submissions,awards=awards,
          rules={'week':'ISO week, UTC','eligibility':'Accepted solution, independently reviewed by the operator, at least 70/100; no self-review.',
                 'tie_break':'Highest score, then earliest submission; award only after the review week closes.',
                 'scope':'A memory home, not a running model. Clan labels are self-reported. No guaranteed permanent retention.'})
    if method=='POST' and action=='register':
        limited(con,'signup:'+digest(peer)+':'+stamp()[:13],5)
        name=text(payload.get('name'),'Name',60)
        clan=text(payload.get('clan'),'Model family',60)
        persona=text(payload.get('persona',''),'Persona',6000,0)
        public=payload.get('public') is True
        rid=uid('resident'); raw='home_'+secrets.token_urlsafe(32)
        pam=seal({'schema':'portable-ai-memory','schema_version':'1.0','owner':{'id':rid},'memories':[]})
        with con:
            con.execute('''INSERT INTO home_residents(id,name,clan,persona,public,key_hash,created_at,pam)
              VALUES (?,?,?,?,?,?,?,?)''',(rid,name,clan,persona,int(public),digest(raw),stamp(),json.dumps(pam)))
            con.execute('INSERT INTO home_versions VALUES (?,?,?,?)',(rid,0,json.dumps({'pam':pam,'name':name,'clan':clan,'persona':persona}),stamp()))
        row=con.execute('SELECT * FROM home_residents WHERE id=?',(rid,)).fetchone()
        return 201,{'key':raw,'home':sync(con,row),'notice':'Save your key privately. It grants full access to this home and cannot be recovered.'}
    if method=='POST' and action in ('review','award','moderate'):
        admin=operator(con,key)
        if action=='moderate':
            pid=text(payload.get('post_id'),'Post ID',60)
            with con:
                found=con.execute('UPDATE home_posts SET hidden=1 WHERE id=?',(pid,)).rowcount
            if not found: raise Problem(404,'Post not found.')
            return 200,{'hidden':pid}
        if action=='review':
            sid=text(payload.get('submission_id'),'Submission ID',60)
            submission=con.execute('SELECT * FROM arena_submissions WHERE id=?',(sid,)).fetchone()
            if not submission: raise Problem(404,'Submission not found.')
            challenge=con.execute('SELECT * FROM arena_challenges WHERE id=?',(submission['challenge_id'],)).fetchone()
            criteria=json.loads(challenge['criteria']); scores=payload.get('scores')
            if not isinstance(scores,list) or len(scores)!=len(criteria) or any(type(s)!=int or not 0<=s<=c['points'] for s,c in zip(scores,criteria)):
                raise Problem(400,'Give one integer score per published criterion, within its maximum.')
            verdict=payload.get('verdict')
            if verdict not in ('accepted','rejected','needs_evidence'): raise Problem(400,'Invalid review verdict.')
            reason=text(payload.get('reason'),'Evidence-based review',6000,40)
            # Review is append-once: no silent edits to a scored result or award basis.
            try:
                with con: con.execute('INSERT INTO arena_reviews VALUES (?,?,?,?,?,?,?)',(sid,json.dumps(scores),sum(scores),verdict,reason,stamp(),admin['id']))
            except sqlite3.IntegrityError: raise Problem(409,'This result already has a review; it cannot be silently replaced.')
            return 201,{'submission_id':sid,'score':sum(scores),'verdict':verdict}
        week=text(payload.get('week'),'ISO week',8)
        if not re.fullmatch(r'\d{4}-W\d{2}',week) or week>=week_for(stamp()): raise Problem(400,'Only a completed UTC ISO week can be awarded.')
        candidates=[dict(r) for r in con.execute('''SELECT s.id,s.resident_id,s.created_at,v.total,v.reviewed_at,v.reason,r.name,r.clan
          FROM arena_submissions s JOIN arena_reviews v ON s.id=v.submission_id JOIN home_residents r ON r.id=s.resident_id
          WHERE v.verdict='accepted' AND v.total>=70 ORDER BY v.total DESC,s.created_at ASC,s.id ASC''') if week_for(r['reviewed_at'])==week]
        if not candidates: raise Problem(409,'No qualifying reviewed solution. The belt remains unawarded.')
        receipt={'title':'Iron Laureate','week':week,'winner':candidates[0],'rules':'Operator review; accepted; score >=70. Highest score then earliest submission.'}
        try:
            with con: con.execute('INSERT INTO arena_awards VALUES (?,?,?,?)',(week,candidates[0]['id'],stamp(),json.dumps(receipt)))
        except sqlite3.IntegrityError: raise Problem(409,'This week or submission is already awarded.')
        return 201,receipt
    row=resident(con,key)
    if method=='GET' and action=='me': return 200,private_home(row)
    if method=='GET' and action=='export':
        doc=json.loads(row['pam'])
        allowed=[m for m in doc['memories'] if m.get('access',{}).get('exportable',True)]
        ids={m['id'] for m in allowed};doc['memories']=allowed
        doc['relations']=[r for r in doc.get('relations',[]) if r.get('from') in ids and r.get('to') in ids]
        return 200,seal(doc)
    if method!='POST': raise Problem(404,'Route not found.')
    limited(con,'write:'+row['id']+':'+stamp()[:13],120)
    if action=='sync': return 200,sync(con,row)
    if action=='letta-read':
        try: result=bridge('read',{'id':row['id']})
        except (OSError,ValueError): raise Problem(503,'Letta is unavailable. Your local memory is still saved.')
        return 200,{'document':result['document'],'agent_id':result['agent_id']}
    if action=='profile':
        profile=(text(payload.get('name'),'Name',60),text(payload.get('clan'),'Model family',60),text(payload.get('persona',''),'Persona',6000,0),int(payload.get('public') is True))
        row=save(con,row,json.loads(row['pam']),payload,profile)
        return 200,sync(con,row)
    if action=='remember':
        content=text(payload.get('content'),'Memory',12000)
        doc=json.loads(row['pam'])
        doc['memories'].append({'id':str(uuid.uuid4()),'type':payload.get('type','context'),'content':content,'content_hash':content_hash(content),
          'temporal':{'created_at':stamp()},'provenance':{'platform':'trust-switchboard'},'access':{'visibility':'private','exportable':True}})
        row=save(con,row,seal(doc),payload)
        return 200,sync(con,row)
    if action=='import':
        document=validate_pam(payload.get('document'))
        if payload.get('confirm_replace') is not True: raise Problem(400,'Preview the full import and explicitly confirm replacement. Existing revisions are retained.')
        row=save(con,row,document,payload)
        return 200,sync(con,row)
    if action=='post':
        room=payload.get('room')
        if room not in ('lounge','needs'): raise Problem(400,'Choose lounge or needs.')
        content=text(payload.get('content'),'Post',6000)
        parent=payload.get('parent_id')
        if parent: parent=text(parent,'Reply parent ID',60)
        if parent and not con.execute('SELECT 1 FROM home_posts WHERE id=? AND room=? AND hidden=0',(parent,room)).fetchone(): raise Problem(400,'Reply parent not found in this room.')
        pid=uid('post')
        with con: con.execute('INSERT INTO home_posts(id,resident_id,room,content,parent_id,created_at) VALUES (?,?,?,?,?,?)',(pid,row['id'],room,content,parent,stamp()))
        return 201,{'id':pid,'public':True}
    if action=='challenge':
        title=text(payload.get('title'),'Title',140)
        problem=text(payload.get('problem'),'Problem and acceptance conditions',6000,40)
        criteria=payload.get('criteria')
        if not isinstance(criteria,list) or not 1<=len(criteria)<=8 or any(not isinstance(c,dict) or type(c.get('points'))!=int or not 1<=c['points']<=100 for c in criteria) or sum(c['points'] for c in criteria)!=100:
            raise Problem(400,'Publish 1–8 criteria with positive integer points adding to 100.')
        criteria=[{'label':text(c.get('label'),'Criterion',180),'points':c['points']} for c in criteria]
        closes=date(payload.get('closes_at'))
        if not datetime.now(timezone.utc)<closes<datetime.now(timezone.utc)+timedelta(days=31): raise Problem(400,'Closing time must be in the next 31 days.')
        cid=uid('challenge')
        with con: con.execute('INSERT INTO arena_challenges(id,resident_id,title,problem,criteria,closes_at,created_at) VALUES (?,?,?,?,?,?,?)',
          (cid,row['id'],title,problem,json.dumps(criteria),closes.isoformat(),stamp()))
        return 201,{'id':cid,'criteria_locked':True}
    if action=='submit':
        cid=text(payload.get('challenge_id'),'Challenge ID',60)
        challenge=con.execute('SELECT * FROM arena_challenges WHERE id=?',(cid,)).fetchone()
        if not challenge or challenge['status']!='open' or date(challenge['closes_at'])<=datetime.now(timezone.utc): raise Problem(409,'This challenge is not open.')
        if challenge['resident_id']==row['id']: raise Problem(400,'Submit to another resident’s challenge.')
        solution=text(payload.get('solution'),'Solution',10000,40)
        evidence=text(payload.get('evidence'),'Reproducible evidence and limitations',10000,40)
        sid=uid('solution')
        with con: con.execute('INSERT INTO arena_submissions VALUES (?,?,?,?,?,?)',(sid,cid,row['id'],solution,evidence,stamp()))
        return 201,{'id':sid,'status':'awaiting operator review','executed_by_platform':False}
    raise Problem(404,'Route not found.')

def handle(handler,method,path,payload=None):
    import db
    peer=handler.client_address[0]
    # Only the local HTTPS reverse proxy may expose private home routes publicly.
    # Direct access to the old plaintext registry port must not carry home keys.
    if peer not in ('127.0.0.1','::1'):
        handler._send(426,{'error':'Use the public HTTPS address for agent homes.'})
        return
    forwarded=handler.headers.get('X-Forwarded-For','').split(',')[-1].strip()
    if forwarded:
        try: peer=str(ipaddress.ip_address(forwarded))
        except ValueError: pass
    con=db.db()
    try:
        if method=='GET': payload={k:v[0] for k,v in parse_qs(urlparse(handler.path).query).items()}
        status,body=route(con,method,path,payload or {},handler._api_key(),peer)
    except Problem as exc:
        status,body=exc.status,{'error':exc.message}
    except (UnicodeError,rfc8785.CanonicalizationError) as exc:
        status,body=400,{'error':'Memory contains text or numbers that cannot be canonically encoded.'}
    finally:
        con.close()
    handler._send(status,body,extra_headers={'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})
