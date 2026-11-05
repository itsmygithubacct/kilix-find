import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from kilix_find import cli as c
from kilix_find import sources as s


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.specs = []
        self.add('optics', 'Aurora camera calibration', 'Calibration failed. The optical shutter is stuck.\nCafé sensors await validation.\n', 'optics')
        self.add('audio', 'Copper audio cancellation', 'The cancel_request handler acknowledged shutdown.\nAudio cancellation passed the deadline check.\n', 'audio')
        self.add('other', 'Planning note', 'A proposed cancellation design exists; no implementation was tested.\n', 'audio')
        self.snapshot = s.index_sources({'schema': 'kilix.find.sources/v1', 'sources': self.specs})
        self.path = self.root / 'content.sqlite'
        c.build(self.snapshot, self.path)
        self.index = c.Index(self.path)

    def add(self, key, title, text, group):
        path = self.root / (key + '.txt')
        path.write_text(text)
        os.utime(path, (1767225600, 1767225600))
        self.specs.append({'key': key, 'path': str(path), 'kind': 'text', 'title': title, 'group': group, 'split': 'development'})

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def keys(self, query, **kw):
        return [r['key'] for r in self.index.search(query, **kw)['matches']]

    def test_find_negative_evidence_with_stemming_and_exact_excerpts(self):
        result = self.index.search('Aurora calibrations failing')
        row = result['matches'][0]
        self.assertEqual(row['key'], 'optics')
        self.assertIn('Calibration failed.', row['excerpt'])
        self.assertEqual(row['excerpt'], Path(row['source_path']).read_text()[row['start']:row['end']])
        self.assertEqual(row['evidence_class'], 'unattributed_text')
        self.assertTrue(s.revalidate(row)['same_generation'])

    def test_code_identifiers_accents_and_phrases(self):
        self.assertEqual(self.keys('cancelRequest')[0], 'audio')
        self.assertEqual(self.keys('cancelRequest', match='all'), ['audio'])
        self.assertEqual(self.keys('cafe'), ['optics'])
        self.assertEqual(self.keys('"cancel request"'), ['audio'])
        self.assertEqual(self.keys('"request cancel"'), [])
        self.assertEqual(self.keys('"optical shutter" audio'), ['optics'])

    def test_unknown_terms_count_toward_coverage_and_any_is_explicit(self):
        result = self.index.search('audio xenocrystal orbital nebula')
        self.assertEqual(result['matches'], [])
        self.assertEqual(result['query_rules']['required_terms'], 2)
        self.assertEqual(len(result['query_rules']['terms_absent_from_index']), 3)
        self.assertTrue(self.keys('audio xenocrystal orbital nebula', match='any'))
        self.assertEqual(self.keys('audio xenocrystal', match='all'), [])
        self.assertEqual(self.keys('xenocrystal'), [])

    def test_sources_and_groups_are_exact_filters(self):
        self.assertEqual(self.keys('cancellation', sources=['other']), ['other'])
        self.assertEqual(self.keys('cancellation', groups=['optics']), [])
        self.assertEqual(self.keys('audio', kinds=['claude']), [])
        with self.assertRaisesRegex(ValueError, 'unknown source_key'):
            self.index.search('audio', sources=['typo'])

    def test_dates_use_recorded_basis_and_exclusive_upper_bound(self):
        self.assertTrue(self.keys('audio', since='2026-01-01', until='2026-01-01'))
        self.assertEqual(self.keys('audio', since='2026-01-02'), [])
        self.assertEqual(self.keys('audio', until='2026-01-01T00:00:00Z'), [])
        result = self.index.search('audio', since='2026-01-01')
        self.assertIn('filesystem_mtime', result['warnings'][0])
        with self.assertRaisesRegex(ValueError, 'timezone-aware'):
            self.index.search('audio', since='2026-01-01T00:00:00')

    def test_deterministic_results_independent_of_insertion_order(self):
        original = [r['id'] for r in self.index.search('cancellation')['matches']]
        changed = copy.deepcopy(self.snapshot)
        changed['records'].reverse()
        changed['index_sha256'] = s.hash_value({k:v for k,v in changed.items() if k != 'index_sha256'})
        path = self.root/'reordered.sqlite'
        c.build(changed, path)
        with c.Index(path) as other:
            self.assertEqual(original, [r['id'] for r in other.search('cancellation')['matches']])

    def test_source_limit_and_preservation_of_alternative_sources(self):
        self.add('long', 'Copper status', ('Cancellation state remains pending. ' * 25 + '\n') * 5, 'audio')
        self.add('copy', 'Copper status', 'Cancellation state remains pending.\n', 'audio')
        snapshot = s.index_sources({'schema':'kilix.find.sources/v1','sources':self.specs})
        path = self.root/'long.sqlite'
        c.build(snapshot,path)
        with c.Index(path) as index:
            result = index.search('cancellation',limit=20,per_source=1)
            keys = [r['key'] for r in result['matches']]
            self.assertEqual(len(keys),len(set(keys)))
            self.assertIn('long',keys)
            self.assertIn('copy',keys)
            self.assertGreater(result['suppressed_duplicates'] + result['suppressed_by_source_limit'],0)

    def test_index_is_read_only_and_cannot_overwrite(self):
        before = self.path.read_bytes()
        self.index.search('audio')
        self.index.verify()
        self.assertEqual(before,self.path.read_bytes())
        with self.assertRaises(sqlite3.OperationalError):
            self.index.db.execute('DELETE FROM documents')
        with self.assertRaisesRegex(ValueError,'already exists'):
            c.build(self.snapshot,self.path)
        self.assertEqual(before,self.path.read_bytes())

    def test_changed_source_requires_reindex(self):
        row = self.index.search('shutter')['matches'][0]
        Path(row['source_path']).write_text('changed')
        with self.assertRaisesRegex(ValueError,'generation changed'):
            s.revalidate(row)
        self.assertIn('stuck',self.index.search('shutter')['matches'][0]['excerpt'])

    def test_corrupt_snapshot_and_stored_excerpt_are_rejected(self):
        changed = copy.deepcopy(self.snapshot)
        changed['records'][0]['text'] = 'forged'
        with self.assertRaisesRegex(ValueError,'digest mismatch'):
            c.build(changed,self.root/'forged.sqlite')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE documents SET payload='{}' WHERE source_key='optics'")
        with self.assertRaisesRegex(ValueError,'digest mismatch'):
            self.index.search('shutter')

    def test_malformed_query_bounds_and_literal_operators(self):
        for query in ['', 'x'*2001, '"unclosed']:
            with self.assertRaises(ValueError): self.index.search(query)
        for options in [{'limit':0},{'per_source':0},{'min_coverage':float('nan')},{'min_coverage':1.1}]:
            with self.assertRaises(ValueError): self.index.search('audio',**options)
        self.assertEqual(self.keys('the and of'), [])
        self.index.search("audio'); DROP TABLE documents; --",match='any')
        self.assertTrue(self.index.verify()['ok'])

    def test_json_cli_default_has_no_model_dependency(self):
        env = os.environ.copy()
        src_path = str(Path(c.__file__).resolve().parents[1])
        env["PYTHONPATH"] = src_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        result = subprocess.run([sys.executable,'-m','kilix_find','--index',str(self.path),'--json','optical shutter'],capture_output=True,text=True,env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['backend'],'bm25')
        self.assertEqual(value['matches'][0]['key'],'optics')
        self.assertNotIn('inference',value)


class SourceTests(unittest.TestCase):
    def test_rollouts_preserve_provenance_and_exclude_non_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kind,rows,pointer,role in [
                ('claude',[{'type':'assistant','sessionId':'session','timestamp':'2026-01-02T03:04:05Z','message':{'role':'assistant','content':[{'type':'thinking','thinking':'private analysis'},{'type':'text','text':'Café shutter calibration failed.'}]}}],'/message/content/1/text','assistant_claim'),
                ('codex',[{'type':'response_item','payload':{'type':'message','role':'assistant','channel':'analysis','content':[{'type':'output_text','text':'private analysis'}]}},{'type':'response_item','timestamp':'2026-01-02T03:04:05Z','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'Find the shutter calibration.'}]}}],'/payload/content/0/text','user_statement')]:
                path=Path(tmp)/(kind+'.jsonl')
                path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
                config={'schema':'kilix.find.sources/v1','sources':[{'key':kind,'path':str(path),'kind':kind,'group':'shutter','split':'development','title':'Shutter','session_id':'session'}]}
                snapshot=s.index_sources(config)
                self.assertEqual(len(snapshot['records']),1)
                row=snapshot['records'][0]
                self.assertEqual(row['json_pointer'],pointer)
                self.assertEqual(row['evidence_class'],role)
                self.assertTrue(s.revalidate(row)['same_generation'])
                db=Path(tmp)/(kind+'.sqlite')
                c.build(snapshot,db)
                with c.Index(db) as index:
                    self.assertEqual(len(index.search('shutter',session='session')['matches']),1)
                    self.assertEqual(index.search('shutter',session='absent')['matches'],[])

    def test_partial_sources_and_terminal_controls_remain_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            note=Path(tmp)/'secret.txt'
            note.write_text('token ' + ('gh' + 'p_') + ('Z' * 32))
            spec={'key':'secret','path':str(note),'kind':'text','group':'test','split':'development','title':'test'}
            snapshot=s.index_sources({'schema':'kilix.find.sources/v1','sources':[spec,{**spec,'key':'missing','path':str(note.parent/'missing')}]})
            self.assertFalse(snapshot['complete_within_explicit_scope'])
            self.assertEqual(snapshot['records'],[])
            c.build(snapshot,Path(tmp)/'empty.sqlite')
            with c.Index(Path(tmp)/'empty.sqlite') as index:
                self.assertTrue(index.search('token')['warnings'])
                self.assertEqual(index.search('token')['matches'],[])
        escaped=s.safe_text('\x1b]52;bad\x07\u202etext')
        self.assertNotIn('\x1b',escaped)
        self.assertNotIn('\u202e',escaped)


if __name__ == '__main__':
    unittest.main()
