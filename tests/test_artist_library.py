import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playlist_bridge import local_library as local,artist_library as artists
from playlist_bridge.storage import Repository
A='11111111-1111-1111-1111-111111111111'
B='22222222-2222-2222-2222-222222222222'

class Artists(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.repo=Repository(Path(temp.name))
        p=patch.object(local,'repo',return_value=self.repo);p.start();self.addCleanup(p.stop)
    def album(self,name,aid,album):
        return {'artist':name,'artist_id':aid,'album_id':album,'local_count':10,'status':'Needs Attention'}
    def test_artist_directory_identity_and_reviewed_alias(self):
        rows=[self.album('Same name',A,'one'),self.album('Same name',B,'two')]
        self.assertEqual(len(artists.directory(rows)),2)
        source=artists.artist_key('Local alias')
        local.put('local_artist_links',source,{'artist_id':A})
        local.put('local_artists',A,{'name':'Canonical artist'})
        merged=artists.directory(rows+[self.album('Local alias','','three')])
        row=next(r for r in merged if r['artist_id']==A)
        self.assertEqual(row['name'],'Canonical artist');self.assertEqual(row['local_albums'],2)
    def test_bad_years_are_not_shown(self):
        for value in ('0000','0001','N/A',''):
            self.assertEqual(local.valid_year(value),'')
        self.assertEqual(local.valid_year('1994-05-10'),'1994')
    def test_itunes_ambiguity_does_not_link(self):
        results={'results':[{'artistId':1,'artistName':'Example'},{'artistId':2,'artistName':'Example'}]}
        with patch.object(artists,'itunes_get',return_value=results):artists.enrich({'id':A,'name':'Example'},['Album'])
        value=self.repo.load('local_artist_enrichment')[A]
        self.assertEqual(value['status'],'Review iTunes artist');self.assertNotIn('itunes_id',value)
    def test_itunes_album_corroboration_and_url_safety(self):
        person={'wrapperType':'artist','artistId':1,'artistName':'Example','artistLinkUrl':'https://music.apple.com/artist/1','primaryGenreName':'Rock'}
        album={'wrapperType':'collection','artistId':1,'collectionName':'Album','releaseDate':'1994-05-10','artworkUrl100':'https://is1-ssl.mzstatic.com/art.jpg','collectionViewUrl':'https://music.apple.com/album/1'}
        with patch.object(artists,'itunes_get',side_effect=[{'results':[person]},{'results':[person,album]}]):artists.enrich({'id':A,'name':'Example'},['Album'])
        value=self.repo.load('local_artist_enrichment')[A]
        self.assertEqual(value['status'],'Loaded');self.assertEqual(value['albums'][0]['year'],'1994')
        self.assertEqual(artists.apple_url('javascript:alert(1)'),'')
        self.assertEqual(artists.image_url('https://attacker.example/art'), '')

if __name__=='__main__':unittest.main()
