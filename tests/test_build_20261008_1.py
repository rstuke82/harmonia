"""H12/H16: real authorization boundaries and saved Plex-only browsing."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch
import test_30 as auth_tests
import test_build_20261007_14 as library_tests
from harmonia import accounts,api,library,saved_views
from harmonia.jobs import Store

class MemberAccess(auth_tests.MultiUser):
    def call(self,path,method='GET',user=None,body=None):
        status,raw=asyncio.run(self.http(path,method,user or self.a,body=body))
        return status,json.loads(raw)

    def test_issues_permission_is_checked_before_preparation_and_revocation(self):
        for path in ['/api/library/issues','/api/library/scan-status']:
            status,_=self.call(path);self.assertEqual(status,403)
        # Re-register the saved provider against this installation, avoiding
        # fixture closures installed by earlier model tests.
        library.register(__import__('fastapi').FastAPI())
        self.a['can_library_issues']=True;accounts.put('accounts',self.a['id'],self.a)
        status,data=self.call('/api/library/issues');self.assertEqual(status,202)
        with accounts.as_user(self.a):repo=accounts.personal_repository()
        job=Store(repo).get(data['job_id'])
        with patch('harmonia.jobs.current',return_value=SimpleNamespace(store=Store(repo),id=job['id'],action='prepare_views')),patch('harmonia.jobs.progress'):
            saved_views.execute(job['payload'])
        status,_=self.call('/api/library/issues');self.assertEqual(status,200)
        self.a['can_library_issues']=False;accounts.put('accounts',self.a['id'],self.a)
        status,_=self.call('/api/library/issues');self.assertEqual(status,403)
        status,_=self.call('/api/library/issues/x/review','PUT',body={'intentional':True});self.assertEqual(status,403)

    def test_download_read_and_management_are_own_request_only(self):
        self.a.update(can_downloads=True,can_manage_downloads=True)
        accounts.put('accounts',self.a['id'],self.a)
        self.repo.save({'user_requests':{'11:mine':{'user_id':'11','album_id':'mine'}}})
        records=[{'key':key,'requesters':[{'user_id':'11','name':'Alice'},{'user_id':'12','name':'Bob'}],'downloads':[{'queue_id':1,'title':'Private download'}]} for key in ['mine','other']]
        endpoint=next(r for r in api.app.routes if getattr(r,'path','')=='/api/lidarr/requests')
        own=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/requests-user/downloads')
        with patch.object(endpoint,'endpoint',SimpleNamespace(__wrapped__=lambda **kwargs:records)),accounts.as_user(self.a):
            rows=own.__wrapped__()
        self.assertEqual([r['key'] for r in rows],['mine'])
        self.assertEqual(rows[0]['requesters'],[{'user_id':'11','name':'Alice'}])
        self.assertEqual(rows[0]['sources'],[])
        with patch('harmonia.lidarr_downloads.perform',return_value={'ok':True}) as perform:
            status,_=self.call('/api/requests-user/other/download-action','POST',body={'action':'search'})
            self.assertEqual(status,403);perform.assert_not_called()
            status,_=self.call('/api/requests-user/mine/download-action','POST',body={'action':'search'})
            self.assertEqual(status,200);perform.assert_called_once()
        with patch.object(endpoint,'endpoint',SimpleNamespace(__wrapped__=lambda **kwargs:records)),accounts.as_user(self.admin):
            self.assertEqual(len(own.__wrapped__()),2)

    def test_member_request_records_actor_and_returns_owned_progress_link(self):
        album=library_tests.ALBUM
        preview=next(r for r in api.app.routes if getattr(r,'path','')=='/api/lidarr/preview')
        add=next(r for r in api.app.routes if getattr(r,'path','')=='/api/lidarr/add')
        def queue(body):
            self.repo.save({'lidarr_requests':{album:{'job_id':'server-job','requesters':[{'user_id':'12','name':'Bob'}]}}})
            return {'id':'server-job'}
        with patch.object(preview,'endpoint',return_value={'preview_id':library_tests.OTHER}),patch.object(add,'endpoint',side_effect=queue):
            status,data=self.call('/api/requests-user/add','POST',body={'album_id':album,'title':'Album','artist':'Artist'})
        self.assertEqual(status,202);self.assertEqual(data['request_id'],album)
        people=self.repo.load('lidarr_requests')[album]['requesters']
        self.assertEqual([p['user_id'] for p in people],['12','11'])
        self.assertEqual(people[1]['name'],'Alice')

    def test_plex_only_account_has_no_requests_even_with_request_permission(self):
        self.repo.save({'lidarr':{'settings':{'enabled':False}}})
        self.assertFalse(accounts.public(self.a)['lidarr_enabled'])
        self.assertFalse(accounts.public(self.a)['can_library_issues'])
        self.assertTrue(accounts.public(self.admin)['can_library_issues'])
        for path,body in [('/api/requests-user/search',{'artist':'Artist','album':'Album'}),('/api/requests-user/add',{'album_id':library_tests.ALBUM})]:
            status,_=self.call(path,'POST',body=body);self.assertEqual(status,409)
        endpoint=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/requests-user')
        with accounts.as_user(self.a):self.assertEqual(endpoint.__wrapped__(),[])

    def test_permission_update_preserves_omitted_issues_field(self):
        self.a['can_library_issues']=True;accounts.put('accounts',self.a['id'],self.a)
        status,data=self.call('/api/users/11','PUT',self.admin,{'disabled':False,'can_request':True})
        self.assertEqual(status,200);self.assertTrue(data['can_library_issues'])
        status,data=self.call('/api/users/11','PUT',self.admin,{'disabled':False,'can_request':True,'can_library_issues':False})
        self.assertEqual(status,200);self.assertFalse(data['can_library_issues'])

class LibraryAccess(library_tests.AlbumReads):
    def test_plex_only_directory_artist_and_album_are_coherent_saved_reads(self):
        directory_app=__import__('fastapi').FastAPI();library.register(directory_app)
        directory=next(r.endpoint for r in directory_app.routes if r.path=='/api/library')
        # The disabled integration must ignore its older saved catalog.
        with patch('harmonia.lidarr.config',return_value={'enabled':False,'url':'http://lidarr'}),accounts.as_user(self.user):
            data=self.prepare(directory)
            self.assertEqual(data['rows'][0]['album_id'],'plex:pal')
            self.assertEqual(data['artists'][0]['name'],'Artist');self.assertEqual(len(data['rows']),1)
            artist=self.prepare(self.artist,'plex:pa')
            album=self.prepare(self.album,'plex:pal')
        self.assertEqual(artist['rows'][0]['album_id'],album['album_id'])
        self.assertEqual(album['tracks'][0]['title'],'Song');self.assertEqual(album['issues'],[])

    def test_member_with_issues_permission_sees_findings_without_file_paths(self):
        user={'id':'2','admin':False,'can_library_issues':True};self.repo.save({'accounts':{'2':user}})
        self.plex['rows']=[]
        with accounts.as_user(user):
            album=self.prepare(self.album,library_tests.ALBUM)
        self.assertTrue(album['issues'])
        self.assertEqual(album['issues'][0]['issue'],'Link needs review')
        self.assertNotIn('/music/song.flac',json.dumps(album))
        self.assertNotIn('files',album['issues'][0])
        self.assertNotIn('file',album['tracks'][0])
