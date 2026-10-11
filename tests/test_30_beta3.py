from playlist_bridge.config import Config
"""Focused beta checks; external media services are never mutated."""
import asyncio,json
from unittest.mock import patch
from test_30 import MultiUser
from playlist_bridge import accounts, api
from playlist_bridge.lidarr import state_put

class Beta3(MultiUser):
    def call(self,path,method='GET',user=None,body=None):
        status,raw=asyncio.run(self.http(path,method=method,user=user or self.a,body=body))
        return status,json.loads(raw)
    def test_promotion_keeps_personal_library(self):
        with accounts.as_user(self.a):
            c=Config();c.config['playlists']=[{'source':'spotify','source_id':'mine'}];c.save();original=accounts.personal_repository().directory
        status,data=self.call('/api/users/11','PUT',self.admin,{'admin':True,'disabled':False,'can_request':True})
        self.assertEqual(status,200,data)
        with accounts.as_user(accounts.users()['11']):
            self.assertEqual(accounts.personal_repository().directory,original)
            self.assertEqual(Config().config['plex']['token'],'ALICE_SECRET')
            self.assertEqual(Config().config['playlists'][0]['source_id'],'mine')
        self.assertIn('11',[u['id'] for u,_ in accounts.member_stores()])
        status,_=self.call('/api/users/1','PUT',self.admin,{'admin':False,'disabled':False,'can_request':True})
        self.assertEqual(status,409)
    def test_bulk_schedule_updates_only_selected(self):
        with accounts.as_user(self.a):
            c=Config();c.config['playlists']=[{'source':'spotify','source_id':v} for v in ('one','two')];c.save()
        status,data=self.call('/api/playlists/sync-mode','PUT',body={'playlist_keys':['spotify:one'],'mode':'disabled'})
        self.assertEqual(status,200,data)
        with accounts.as_user(self.a):
            modes=accounts.personal_repository().load('playlist_schedules');self.assertEqual(modes['spotify:one']['mode'],'disabled');self.assertNotIn('spotify:two',modes)
        status,_=self.call('/api/playlists/sync-mode','PUT',self.b,{'playlist_keys':['spotify:one'],'mode':'inherit'});self.assertEqual(status,404)
    def test_shared_listing_and_preview_permissions(self):
        scoped=patch('playlist_bridge.shared_playlists.root_repository',return_value=self.repo);scoped.start();self.addCleanup(scoped.stop)
        source={'url':'https://music.apple.com/us/playlist/test/pl.test','name':'Rock Workout on Apple Music','custom_name':False}
        state_put(self.repo,'shared_playlists','example',source)
        status,rows=self.call('/api/shared-playlists');self.assertEqual(status,200);self.assertEqual(rows[0]['name'],'Rock Workout');self.assertFalse(rows[0]['subscribed'])
        with accounts.as_user(self.a):
            c=Config();c.config['playlists']=[{'source':'applemusic','source_id':'pl.test'}];c.save()
        _,rows=self.call('/api/shared-playlists');self.assertTrue(rows[0]['subscribed'])
        _,rows=self.call('/api/shared-playlists',user=self.b);self.assertFalse(rows[0]['subscribed'])
        status,_=self.call('/api/shared-playlists','POST',body=source);self.assertEqual(status,403)
    def test_names(self):
        from playlist_bridge.source_names import clean
        self.assertEqual(clean('â\x80\x9990s Workout on Apple Music'),'’90s Workout')
        self.assertEqual(clean('Rock Workout - Playlist - Apple Music'),'Rock Workout')
    def test_download_permissions_and_approval(self):
        status,_=self.call('/api/requests-user/downloads');self.assertEqual(status,403)
        status,_=self.call('/api/requests-user/album/download-action','POST',body={'action':'search'});self.assertEqual(status,403)
        self.a['auto_approve']=False;accounts.put('accounts','11',self.a)
        status,data=self.call('/api/requests-user/add','POST',body={'album_id':'11111111-1111-1111-1111-111111111111','title':'Song','artist':'Artist'})
        self.assertEqual(status,202,data);self.assertFalse(data['queued']);self.assertTrue(self.repo.load('pending_requests'))
    def test_manual_album_link_requires_admin_and_existing_identity(self):
        album='11111111-1111-1111-1111-111111111111'
        body={'artist':'Artist','album':'Album','album_id':album}
        status,_=self.call('/api/discover/album-link','PUT',body=body);self.assertEqual(status,403)
        status,_=self.call('/api/discover/album-link','PUT',self.admin,body);self.assertEqual(status,409)
        state_put(self.repo,'inventory','lidarr',{'rows':[{'album_id':album}]})
        status,_=self.call('/api/discover/album-link','PUT',self.admin,body);self.assertEqual(status,200)
    def test_request_removal_preserves_album_inventory(self):
        state_put(self.repo,'inventory','lidarr',{'rows':[{'album_id':'album'}]})
        state_put(self.repo,'lidarr_requests','album',{'status':'added'})
        state_put(self.repo,'user_requests','11:album',{'user_id':'11','album_id':'album'})
        status,data=self.call('/api/lidarr/requests/album','DELETE',self.admin)
        self.assertEqual(status,200,data);self.assertFalse(self.repo.load('lidarr_requests'));self.assertTrue(self.repo.load('inventory')['lidarr']['rows'])
