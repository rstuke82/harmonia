import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from harmonia.storage import Repository
from harmonia.lidarr import state_put
from harmonia import playlist_overlays
from harmonia.source_observations import validate
from harmonia.file_audit import inspect,upgrade_reason,match_review_reason
from harmonia.file_tools import Settings
from harmonia.matching import Matcher
from harmonia.api import _has_pending_playlist_changes
from harmonia.metadata_sources import spotify_relations

class Beta10(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.repo=Repository(Path(self.tmp.name)/'data')
 def test_overlay_keeps_duplicates_manual_tracks_and_removals(self):
  source=[{'source_id':'1','title':'One','artist':'A','album':'X'},{'source_id':'1','title':'One','artist':'A','album':'X'},{'source_id':'2','title':'Two','artist':'B','album':'Y'}]
  rows=playlist_overlays.entries(source)
  manual={'editor_id':'bridge:m','origin':'bridge','title':'Uploaded','artist':'A','album':'','source_id':''}
  state_put(self.repo,'playlist_overlays','apple:p',{'revision':1,'removed':[rows[2]['editor_id']],'added':[manual],'order':[rows[1]['editor_id'],'bridge:m',rows[0]['editor_id']]})
  merged=playlist_overlays.merged(self.repo,'apple:p',source+[{'source_id':'3','title':'New','artist':'C','album':'Z'}])
  self.assertEqual([r['title'] for r in merged],['One','Uploaded','One','New'])
  self.assertEqual(sum(r['title']=='One' for r in merged),2)
 def test_ready_to_sync_only_reflects_pending_durable_work(self):
  self.assertFalse(_has_pending_playlist_changes({},'apple:p',[]))
  self.assertTrue(_has_pending_playlist_changes({'ready_to_sync':True},'apple:p',[]))
  self.assertTrue(_has_pending_playlist_changes({},'apple:p',[{'playlist_key':'apple:p'}]))
  self.assertFalse(_has_pending_playlist_changes({},'apple:p',[{'playlist_key':'apple:other'}]))
 def test_source_completeness_protects_existing_playlist(self):
  previous=[{'title':str(i)} for i in range(5)]
  with self.assertRaisesRegex(ValueError,'could not confirm completeness'):validate(self.repo,'k',previous[:3],{},previous)
  self.assertTrue(validate(self.repo,'k',[],{'complete':True,'expected_track_count':0},previous))
  with self.assertRaisesRegex(ValueError,'of 5 expected'):validate(self.repo,'k',previous[:4],{'expected_track_count':5},previous)
 def test_upgrade_rules_and_naming(self):
  self.assertTrue(upgrade_reason({'codec':'MP3','bitrate':320000}))
  self.assertFalse(upgrade_reason({'codec':'AAC','bitrate':256000}))
  self.assertFalse(upgrade_reason({'codec':'ALAC','bitrate':800000}))
  self.assertTrue(upgrade_reason({'codec':'AAC','bitrate':192000}))
  self.assertFalse(upgrade_reason({'codec':'AAC','bitrate':326000}))
  self.assertFalse(match_review_reason({'codec':'AAC','bitrate':326000}))
 def test_upgrade_candidate_survives_missing_file(self):
  root=(Path(self.tmp.name)/'music');root.mkdir();root=root.resolve();path=root/'01 Song.mp3';path.write_bytes(b'x')
  cfg=Settings(audit_enabled=True,mappings=[{'lidarr_path':'/lidarr','local_path':str(root)}]).model_dump()
  inv={'identity':'x','checked_at':time.time(),'tracks':[{'id':'7','recording_id':'mbid','title':'Song','file':'/lidarr/01 Song.mp3'}]}
  with patch('harmonia.local_library.read_tags',return_value={'title':'Song','recording_id':'mbid','codec':'MP3','format':'MP3','bitrate':320000}):first=inspect(cfg,inv,{},None)
  path.unlink();second=inspect(cfg,inv,{},first)
  waiting=[r for r in second['issues'] if r['issue']=='Upgrade candidate']
  self.assertEqual(waiting[0]['evidence']['status'],'Missing — awaiting upgraded file')
 def test_high_bitrate_aac_is_neither_match_review_nor_quality_issue(self):
  root=(Path(self.tmp.name)/'aac');root.mkdir();root=root.resolve();path=root/'01 Song.m4a';path.write_bytes(b'x')
  cfg=Settings(audit_enabled=True,mappings=[{'lidarr_path':'/lidarr','local_path':str(root)}]).model_dump()
  inv={'identity':'x','checked_at':time.time(),'tracks':[{'id':'7','recording_id':'mbid','title':'Song','file':'/lidarr/01 Song.m4a'}]}
  with patch('harmonia.local_library.read_tags',return_value={'title':'Song','recording_id':'mbid','codec':'AAC','format':'AAC','bitrate':326000}):result=inspect(cfg,inv,{},None)
  self.assertFalse(any(r['issue']=='Upgrade candidate' for r in result['issues']))
  self.assertFalse(any(r['issue']=='iTunes Match review' for r in result['issues']))
 def test_structured_contributor_can_match_when_album_agrees(self):
  source={'title':'The Middle','artist':'Zedd, Maren Morris & Grey','artists':[{'name':'Zedd'},{'name':'Maren Morris'},{'name':'Grey'}],'album':'The Middle - Single'}
  candidate={'plex_id':'1','title':'The Middle','artist':'Zedd','album':'The Middle'}
  self.assertEqual(Matcher._match_catalog_track(source,[candidate]),'1')
 def test_spotify_artist_relations_are_normalized_and_deduplicated(self):
  relations=[{'url':{'resource':'https://open.spotify.com/artist/abc123/'}},{'url':{'resource':'https://www.open.spotify.com/artist/abc123'}},{'url':{'resource':'https://open.spotify.com/album/nope'}},{'url':{'resource':'https://example.com/artist/other'}}]
  self.assertEqual(spotify_relations(relations),[('abc123','https://open.spotify.com/artist/abc123')])

if __name__=='__main__':unittest.main()
