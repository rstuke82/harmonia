import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from harmonia.album_identity import Albums
from harmonia.album_catalog import catalog_rows,secondary_types
from harmonia.file_audit import inspect,report
from harmonia.file_tools import Settings
from harmonia import library,accounts
import test_build_20261007_14 as previous
from test_build_20261007_14 import ALBUM,OTHER,ARTIST

class AlbumEvidence(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()
        self.cfg=Settings(audit_enabled=True,mappings=[{'lidarr_path':'/music','local_path':str(self.root),'plex_path':'/plex'}]).model_dump()
        self.lidarr={'checked_at':time.time(),'rows':[{'id':1,'album_id':ALBUM,'artist':'Various Artists','title':'Tribute'}],
                     'rename_preview':{'rows':[],'errors':[],'checked_at':time.time()},'tracks':[{'id':'1','album_id':ALBUM,'artist':'Various Artists','album':'Tribute','file':'/music/one.flac','recording_id':'r'}]}
        self.tags={'title':'Song','artist':'Performer','album_artist':'Various Artists','album':'Tribute','recording_id':'r','codec':'FLAC'}
    def scan(self,previous=None,verify=True):
        with patch('harmonia.local_library.read_tags',return_value=self.tags):return inspect(self.cfg,self.lidarr,{},previous,verify)
    def old(self,album=True):
        source={'issue':'Identical files','files':[str(self.root/'one.flac'),str(self.root/'two.flac')],'evidence':{'sha256':'verified'},'reason':'Hashes match.'}
        if album:source['album_id']=ALBUM
        return {'checked_at':time.time(),'issues':[source], 'files':{source['files'][0]:{'tags':{'artist':'First Performer','album':'Tribute'}},source['files'][1]:{'tags':{'artist':'Second Performer','album':'Tribute'}}}}
    def test_tracked_unmanaged_copy_same_album_is_verified(self):
        for name in ['one.flac','two.flac']:(self.root/name).write_bytes(b'same audio')
        result=self.scan();rows=[r for r in result['issues'] if r['issue']=='Identical files']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['album_id'],ALBUM)
        self.assertEqual(rows[0]['artist'],'Various Artists')
        self.assertEqual(result['files'][str(self.root/'two.flac')]['album_identity']['album_id'],ALBUM)
        self.assertEqual(len([r for r in self.scan(result,False)['issues'] if r['issue']=='Identical files']),1)
    def test_identical_audio_on_two_known_albums_is_not_duplicate(self):
        for name in ['one.flac','two.flac']:(self.root/name).write_bytes(b'same audio')
        self.lidarr['rows'].append({'id':2,'album_id':OTHER,'artist':'Various Artists','title':'Another album'})
        self.lidarr['tracks'].append({'id':'2','album_id':OTHER,'album':'Another album','file':'/music/two.flac','recording_id':'r'})
        result=self.scan();self.assertFalse(any(r['issue']=='Identical files' for r in result['issues']))
    def test_historical_compilation_hashes_use_paths_and_saved_album(self):
        self.lidarr['tracks'].append({'id':'2','album_id':ALBUM,'file':'/music/two.flac'})
        rows=report(self.cfg,self.lidarr,None,snapshot=self.old())['issues']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['issue'],'Identical files')
        self.assertEqual(rows[0]['artist'],'Various Artists');self.assertEqual(rows[0]['evidence']['sha256'],'verified')
    def test_unresolved_legacy_evidence_requires_review_instead_of_disappearing(self):
        self.lidarr['tracks']=[]
        rows=report(self.cfg,self.lidarr,None,snapshot=self.old(False))['issues']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['issue'],'Album identity needs review')
        self.assertTrue(rows[0]['identity_review']);self.assertIn('Scan Local Files',rows[0]['reason'])
    def test_conflicting_current_album_associations_override_saved_hint(self):
        self.lidarr['rows'].append({'id':2,'album_id':OTHER,'artist':'Other','title':'Another album'})
        self.lidarr['tracks'].append({'id':'2','album_id':OTHER,'file':'/music/two.flac'})
        self.assertEqual(report(self.cfg,self.lidarr,None,snapshot=self.old())['issues'],[])
    def test_ambiguous_same_name_does_not_choose_a_release_group(self):
        self.lidarr['rows'].append({**self.lidarr['rows'][0],'id':2,'album_id':OTHER})
        result=Albums(self.lidarr).resolve(self.tags)
        self.assertEqual(result['identity_status'],'review');self.assertNotIn('album_id',result)
    def test_mapping_and_album_artist_resolve_compilation_performer(self):
        plex={'rows':[{'plex_id':'p','plex_album_id':'pa','artist':'Performer','album_artist':'Various Artists','album':'Tribute','files':['/plex/one.flac']}]}
        result=Albums(self.lidarr,plex,self.cfg['mappings']).finding({**plex['rows'][0],'issue':'Plex-only / unlinked'})
        self.assertEqual(result['album_id'],ALBUM);self.assertEqual(result['review_album_id'],ALBUM)
        self.assertEqual(result['plex_album_id'],'pa');self.assertEqual(result['artist'],'Various Artists')
    def test_legacy_tags_are_reloaded_on_scan(self):
        for name in ['one.flac','two.flac']:(self.root/name).write_bytes(b'same audio')
        before=self.scan()
        for row in before['files'].values():row['tags'].pop('album_artist')
        with patch('harmonia.local_library.read_tags',return_value=self.tags) as tags:
            inspect(self.cfg,self.lidarr,{},before,False)
        self.assertEqual(tags.call_count,2)

class SecondaryTypes(unittest.TestCase):
    def test_managed_group_enriched_by_id_even_when_edition_title_differs(self):
        artists=[{'id':1,'mbid':ARTIST,'name':'Artist'}]
        rows=[{'id':1,'album_id':ALBUM,'artist_id':1,'title':'Live Album (Deluxe)','artist':'Artist'}]
        catalogs={ARTIST:{'rows':[{'id':ALBUM,'title':'Live Album','secondary_types':['Live','Compilation'],'official':True}]}}
        result=catalog_rows(rows,artists,catalogs,{})
        self.assertEqual(result[0]['secondary_types'],['Live','Compilation']);self.assertEqual(len(result),1)
    def test_raw_lidarr_representations_are_preserved(self):
        self.assertEqual(secondary_types({'secondaryTypes':[{'name':'live'},'Compilation']}),['Live','Compilation'])
        self.assertEqual(secondary_types({'albumType':'Remix'}),['Remix'])
    def test_ambiguous_title_does_not_inherit_types(self):
        rows=[{'id':1,'artist_id':1,'title':'Same','artist':'Artist'}]
        catalogs={ARTIST:{'rows':[{'id':ALBUM,'title':'Same','secondary_types':['Live'],'official':True},{'id':OTHER,'title':'Same','secondary_types':['Remix'],'official':True}]}}
        self.assertEqual(catalog_rows(rows,[{'id':1,'mbid':ARTIST}],catalogs,{})[0]['secondary_types'],[])

class IssuesNavigation(previous.AlbumReads):
    def endpoints(self):
        app=FastAPI();library.register(app)
        return {r.path:r.endpoint for r in app.routes if r.path.startswith('/api/library')}
    def test_plex_only_route_has_same_issues_and_saved_tracks(self):
        self.plex['rows'].append({'plex_id':'unlinked','plex_album_id':'only','title':'Other Song','artist':'Performer','album_artist':'Various Artists','album':'Compilation','files':['/music/other.flac']})
        routes=self.endpoints()
        with accounts.as_user(self.user):
            issues=self.prepare(routes['/api/library/issues'])
            detail=self.prepare(routes['/api/library/albums/{album_id}'],'plex:only')
        finding=next(e for e in issues['errors'] if e.get('plex_album_id')=='only')
        self.assertEqual(finding['review_album_id'],'plex:only')
        self.assertEqual(detail['issues'],[finding]);self.assertEqual(detail['artist'],'Various Artists')
        self.assertEqual(detail['tracks'][0]['plex_id'],'unlinked');self.assertTrue(detail['plex_only'])
    def test_linked_plex_findings_route_to_lidarr_album(self):
        self.plex['rows'].append({'plex_id':'unlinked','plex_album_id':'pal','title':'Unmatched recording','artist':'Performer','album_artist':'Artist','album':'Partial','files':[]})
        routes=self.endpoints()
        with accounts.as_user(self.user):
            issues=self.prepare(routes['/api/library/issues'])
            detail=self.prepare(routes['/api/library/albums/{album_id}'],ALBUM)
        finding=next(e for e in issues['errors'] if e.get('plex_id')=='unlinked')
        self.assertEqual(finding['album_id'],ALBUM);self.assertEqual(finding['review_album_id'],ALBUM)
        self.assertIn(finding,detail['issues'])
    def test_reverse_link_is_persisted_and_no_search_is_started(self):
        self.plex['rows'].append({'plex_id':'unlinked','plex_album_id':'only','title':'Song','artist':'Performer','album_artist':'Various Artists','album':'Compilation','files':[]})
        routes=self.endpoints();endpoint=routes['/api/library/plex-albums/{plex_album_id}/lidarr-link']
        import inspect as reflection
        body_type=reflection.signature(endpoint).parameters['body'].annotation
        with accounts.as_user(self.user),patch('harmonia.lidarr.Client.call',side_effect=AssertionError('No Lidarr API mutations during association')):
            result=endpoint('only',body_type(album_id=ALBUM))
            detail=self.prepare(routes['/api/library/albums/{album_id}'],ALBUM)
            issues=self.prepare(routes['/api/library/issues'])
        self.assertTrue(result['saved']);self.assertEqual(detail['plex_album_id'],'only')
        self.assertTrue(any(link['plex_album_id']=='only' for links in self.repo.load('plex_album_links').values() for link in links.values()))
        self.assertTrue(all(e.get('review_album_id')==ALBUM for e in issues['errors'] if e.get('plex_album_id')=='only'))
    def test_member_cannot_reverse_link(self):
        routes=self.endpoints();endpoint=routes['/api/library/plex-albums/{plex_album_id}/lidarr-link']
        import inspect as reflection
        from fastapi import HTTPException
        body=reflection.signature(endpoint).parameters['body'].annotation(album_id=ALBUM)
        with accounts.as_user({'id':'2','admin':False}):
            with self.assertRaises(HTTPException) as error:endpoint('pal',body)
        self.assertEqual(error.exception.status_code,403)

class InternalPreparation(unittest.TestCase):
    def test_internal_jobs_hidden_from_activity_and_history_but_diagnostic_lane_remains(self):
        from harmonia import api,jobs,admin_activity
        from harmonia.storage import Repository
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);repo=Repository(Path(tmp.name));store=jobs.Store(repo)
        internal=store.enqueue('prepare_views',{'key':'fixture'});visible=store.enqueue('health',{'scope':'all'})
        with patch('harmonia.api.job_store',return_value=store),accounts.as_user({'id':'2','admin':False}):
            self.assertEqual([j['id'] for j in api.list_jobs()],[visible])
        with patch('harmonia.admin_activity.stores',return_value=[({'id':'1','name':'Owner'},store)]):
            result=admin_activity.history();diagnostic=admin_activity.history(worker='views')
        self.assertEqual(result['total'],1);self.assertEqual(result['rows'][0]['id'],visible)
        self.assertEqual(diagnostic['total'],1);self.assertEqual(diagnostic['rows'][0]['id'],internal)
    def test_download_status_and_playlist_changes_do_not_invalidate_library_pages(self):
        from harmonia import saved_views
        from harmonia.storage import Repository
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);repo=Repository(Path(tmp.name))
        with patch('harmonia.saved_views.root_repository',return_value=repo):
            before=saved_views.revision(repo,saved_views.LIBRARY)
            repo.save({'lidarr_requests':{'a':{'download_checked_at':time.time()}},'health_history':{'check':{}},'mapping':{'playlist':{}}})
            self.assertEqual(before,saved_views.revision(repo,saved_views.LIBRARY))
            repo.save({'inventory':{'lidarr':{'rows':[]}}})
            self.assertNotEqual(before,saved_views.revision(repo,saved_views.LIBRARY))
    def test_existing_review_survives_canonical_identity_enrichment(self):
        from harmonia.file_audit import decorate
        row={'issue':'Identical files','files':['a','b'],'evidence':{'sha256':'same'}}
        class Repo:
            reviews={}
            def load(self,key):return self.reviews
        repo=Repo();old=decorate([row],repo)[0];repo.reviews[old['issue_id']]={'intentional':True}
        identities=Albums({'rows':[{'album_id':ALBUM,'artist':'Artist','title':'Album'}],'tracks':[{'album_id':ALBUM,'file':'a'},{'album_id':ALBUM,'file':'b'}]})
        enriched=decorate([identities.finding(row)],repo)[0]
        self.assertTrue(enriched['intentional']);self.assertEqual(enriched['issue_id'],old['issue_id'])
        self.assertNotIn('_legacy_issue_id',enriched)
