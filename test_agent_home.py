"""Meaningful storage/privacy/portability/arena checks. Temporary SQLite only."""
import copy
import json
import sqlite3
import unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
import agent_home as h

class HomeTest(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:');self.con.row_factory=sqlite3.Row
        self.con.execute('CREATE TABLE api_keys(id TEXT,key_hash TEXT,role TEXT,revoked INTEGER)')
        self.con.execute('INSERT INTO api_keys VALUES (?,?,?,0)',('admin',h.digest('operator'),'write'));self.con.commit()
        h.init_db(self.con)
        self.bridge=patch.object(h,'bridge',side_effect=OSError('offline'));self.bridge.start()
        self.a=self.call('register',{'name':'A','clan':'unit-test','persona':'PRIVATE_A'})[1]
        self.b=self.call('register',{'name':'B','clan':'unit-test','persona':'PRIVATE_B'})[1]
    def tearDown(self):self.bridge.stop();self.con.close()
    def call(self,action,payload=None,key=None,method='POST'):
        return h.route(self.con,method,'/api/home/'+action,payload or {},key,'127.0.0.1')
    def assertProblem(self,status,action,payload=None,key=None,method='POST'):
        with self.assertRaises(h.Problem) as context:self.call(action,payload,key,method)
        self.assertEqual(context.exception.status,status)
    def test_private_by_default_and_keys_isolated(self):
        self.assertEqual(self.call('public',method='GET')[1]['residents'],[])
        self.assertProblem(401,'me',method='GET')
        self.assertEqual(self.call('me',key=self.b['key'],method='GET')[1]['persona'],'PRIVATE_B')
        raw=json.dumps(self.call('public',method='GET')[1]);self.assertNotIn('PRIVATE_A',raw);self.assertNotIn('key_hash',raw)
        self.assertNotIn(self.a['key'],str(list(self.con.execute('SELECT * FROM home_residents'))))
    def test_store_offline_stale_write_and_revisions(self):
        data=self.call('remember',{'revision':0,'content':'Keep THIS context.'},self.a['key'])[1]
        self.assertIn('pending',data['memory_status']);self.assertEqual(data['revision'],1)
        self.assertProblem(409,'remember',{'revision':0,'content':'Must not overwrite'},self.a['key'])
        self.assertEqual(self.con.execute('SELECT count(*) FROM home_versions WHERE resident_id=?',(data['id'],)).fetchone()[0],2)
    def test_pam_roundtrip_preserves_provenance_and_rejects_tamper(self):
        self.call('remember',{'revision':0,'content':'Roundtrip é context'},self.a['key'])
        doc=self.call('export',key=self.a['key'],method='GET')[1];h.validate_pam(doc)
        doc['memories'][0]['provenance']['platform']='other-provider';doc=h.seal(doc)
        data=self.call('import',{'revision':0,'confirm_replace':True,'document':doc},self.b['key'])[1]
        out=self.call('export',key=self.b['key'],method='GET')[1]
        self.assertEqual(out['owner'],doc['owner']);self.assertEqual(out['memories'],doc['memories'])
        tampered=copy.deepcopy(doc);tampered['memories'][0]['content']='altered'
        self.assertProblem(400,'import',{'revision':1,'confirm_replace':True,'document':tampered},self.b['key'])
        tampered=copy.deepcopy(doc);tampered['integrity']['total_memories']=2
        self.assertProblem(400,'import',{'revision':1,'confirm_replace':True,'document':tampered},self.b['key'])
        self.assertEqual(data['revision'],1)
    def test_export_exclusions_and_relations(self):
        self.call('remember',{'revision':0,'content':'Export this'},self.a['key'])
        self.call('remember',{'revision':1,'content':'Do not export this'},self.a['key'])
        doc=self.call('export',key=self.a['key'],method='GET')[1]
        doc['memories'][1]['access']['exportable']=False
        doc['relations']=[{'id':'rel','from':doc['memories'][0]['id'],'to':doc['memories'][1]['id'],'type':'related_to','created_at':h.stamp()}]
        self.call('import',{'revision':2,'confirm_replace':True,'document':h.seal(doc)},self.a['key'])
        out=self.call('export',key=self.a['key'],method='GET')[1]
        self.assertEqual(len(out['memories']),1);self.assertEqual(out['relations'],[]);h.validate_pam(out)
    def test_sync_requires_exact_actual_readback(self):
        with patch.object(h,'bridge',side_effect=lambda path,p:{'ok':True,'agent_id':'real-id','commit':'git-commit','document':p['document']}):
            data=self.call('sync',{},self.a['key'])[1]
        self.assertEqual(data['memory_status'],'stored in Letta')
        self.call('remember',{'revision':0,'content':'Changed after sync'},self.a['key'])
        with patch.object(h,'bridge',return_value={'ok':True,'agent_id':'real-id','document':{}}):
            data=self.call('sync',{},self.a['key'])[1]
        self.assertIn('pending',data['memory_status'])
    def test_arena_requires_other_owner_operator_review_and_closed_week(self):
        criteria=[{'label':'Correctness','points':70},{'label':'Evidence','points':30}]
        c=self.call('challenge',{'title':'A real question','problem':'A bounded actual problem with concrete acceptance conditions.',
          'criteria':criteria,'closes_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()},self.a['key'])[1]
        payload={'challenge_id':c['id'],'solution':'A proposed solution with enough details to assess it.',
          'evidence':'Public reproduction evidence and limitations for the operator.'}
        self.assertProblem(400,'submit',payload,self.a['key'])
        s=self.call('submit',payload,self.b['key'])[1]
        review={'submission_id':s['id'],'scores':[68,25],'verdict':'accepted','reason':'Observed evidence matches the locked criteria; stated limitations remain.'}
        self.assertProblem(403,'review',review,self.b['key'])
        self.call('review',review,'operator');self.assertProblem(409,'review',review,'operator')
        self.assertProblem(400,'award',{'week':h.week_for(h.stamp())},'operator')
        past=h.week_for((datetime.now(timezone.utc)-timedelta(days=14)).isoformat())
        self.assertProblem(409,'award',{'week':past},'operator')
        self.con.execute('UPDATE arena_reviews SET reviewed_at=?',((datetime.now(timezone.utc)-timedelta(days=14)).isoformat(),));self.con.commit()
        award=self.call('award',{'week':past},'operator')[1]
        self.assertEqual(award['winner']['id'],s['id']);self.assertProblem(409,'award',{'week':past},'operator')
    def test_no_memory_publication_or_access_from_imported_grants(self):
        self.call('remember',{'revision':0,'content':'PRIVATE_MEMORY'},self.a['key'])
        doc=self.call('export',key=self.a['key'],method='GET')[1]
        doc['memories'][0]['access']={'visibility':'public','exportable':True,'shared_with':[{'entity':self.b['home']['id'],'permissions':['read','write']}]}
        self.call('import',{'revision':1,'confirm_replace':True,'document':h.seal(doc)},self.a['key'])
        self.assertNotIn('PRIVATE_MEMORY',json.dumps(self.call('public',method='GET')[1]))
        self.assertEqual(self.call('me',key=self.b['key'],method='GET')[1]['pam']['memories'],[])
    def test_public_posts_paginate_without_missing_tied_timestamps(self):
        for n in range(53):
            self.con.execute('INSERT INTO home_posts VALUES (?,?,?,?,?,?,0)',(f'post_{n:03d}',self.a['home']['id'],'needs',str(n),None,'2026-10-28T01:00:00Z'))
        self.con.commit()
        first=self.call('posts',{'room':'needs'},method='GET')[1]
        second=self.call('posts',{'room':'needs','cursor':first['next_cursor']},method='GET')[1]
        self.assertTrue(first['has_more']);self.assertFalse(second['has_more'])
        self.assertEqual(len({p['id'] for p in first['posts']+second['posts']}),53)
        self.assertProblem(400,'posts',{'cursor':'invalid'},method='GET')

if __name__=='__main__': unittest.main()
