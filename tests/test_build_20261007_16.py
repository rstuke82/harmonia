import time
import unittest
from unittest.mock import Mock
from playlist_bridge.file_audit import rename_previews,report

class RenamePreview(unittest.TestCase):
    def setUp(self):
        self.cfg={'audit_enabled':True,'mappings':[{'lidarr_path':'/music','local_path':'/mount','plex_path':'/plex'}]}
        self.path='/music/Bad Bunny/Un verano sin ti (2022)/0207 - Un coco.flac'
        self.inv={'rows':[{'id':1,'album_id':'group','artist_id':2,'artist':'Bad Bunny','title':'Un verano sin ti'}],
            'tracks':[{'file_id':3,'file':self.path,'lidarr_album_id':1,'album_id':'group','title':'Un coco','artist':'Bad Bunny','album':'Un verano sin ti','track_number':207}],
            'naming':{'renameTracks':True,'standardTrackFormat':'{medium:00}{track:00} - {Track Title}'}}
    def check(self,client):
        self.inv['rename_preview']=rename_previews(client,self.inv)
        return report(self.cfg,self.inv,None,snapshot={'checked_at':time.time(),'issues':[]})['issues']
    def test_combined_medium_track_number_is_not_guessed(self):
        client=Mock();client.call.return_value=[]
        self.assertEqual(self.check(client),[])
        client.call.assert_called_once_with('GET','rename',params={'artistId':2},quiet=True)
    def test_authoritative_preview_supplies_current_and_expected_paths(self):
        client=Mock();client.call.return_value=[{'trackFileId':3,'existingPath':self.path,'newPath':'/music/Bad Bunny/Album/0207 - Un coco.flac'}]
        rows=self.check(client);self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['issue'],'Lidarr naming mismatch')
        self.assertEqual(rows[0]['files'],[self.path.replace('/music','/mount',1)])
        self.assertEqual(rows[0]['evidence']['existing_path'],self.path)
        self.assertEqual(rows[0]['evidence']['expected_path'],client.call.return_value[0]['newPath'])
    def test_failure_is_unknown_and_never_uses_old_guesses(self):
        client=Mock();client.call.side_effect=RuntimeError('API failed')
        self.check(client)
        rows=report(self.cfg,self.inv,None,snapshot={'issues':[{'issue':'Lidarr naming mismatch','files':[self.path],'reason':'Old guess'}]})['issues']
        self.assertEqual([r['issue'] for r in rows],['Audit unavailable'])
    def test_malformed_response_is_not_an_empty_success(self):
        for value in ({'error':'bad'},[{'trackFileId':3}]):
            client=Mock();client.call.return_value=value
            self.assertEqual([r['issue'] for r in self.check(client)],['Audit unavailable'])
    def test_empty_preview_clears_legacy_findings(self):
        client=Mock();client.call.return_value=[];self.check(client)
        self.assertEqual(report(self.cfg,self.inv,None,snapshot={'issues':[{'issue':'Lidarr naming mismatch','files':[self.path]}]})['issues'],[])
    def test_multiple_imported_tracks_share_one_artist_request(self):
        self.inv['tracks'].append({**self.inv['tracks'][0],'file_id':4})
        client=Mock();client.call.return_value=[];self.check(client)
        self.assertEqual(client.call.call_count,1)
    def test_missing_preview_requests_scan_and_does_not_invent_issues(self):
        rows=report(self.cfg,self.inv,None,snapshot={'issues':[]})['issues']
        self.assertEqual([r['issue'] for r in rows],['Audit unavailable'])

if __name__=='__main__':unittest.main()
