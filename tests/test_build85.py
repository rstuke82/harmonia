import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from harmonia import accounts,jobs,network
from harmonia.storage import Repository
from harmonia.plex_client import PlexAPI

class Build85(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.repo=Repository(Path(tmp.name))
        for name in ('personal_repository','root_repository'):
            p=patch.object(accounts,name,return_value=self.repo);p.start();self.addCleanup(p.stop)
        self.client=object.__new__(PlexAPI);self.client.base_url='http://plex';self.client.headers={'X-Plex-Token':'secret-token-value'};self.client.strict_errors=False
        self.client._library_uri=lambda value:'server://machine/library/metadata/'+value
        self.contents=['old'];self.clears=0
        self.client.get_playlist_items=lambda _:[{'plex_id':v,'playlist_item_id':str(i)} for i,v in enumerate(self.contents)]
        def clear(_):self.clears+=1;self.contents.clear();return True
        self.client.clear_playlist=clear
        self.client.add_to_playlist=lambda _,value:self.contents.append(value) or True
    def response(self,ids,queue='queue',total=None,status=200):
        return Mock(status_code=status,json=Mock(return_value={'MediaContainer':{'playQueueID':queue,'playQueueTotalCount':len(ids) if total is None else total,'Metadata':[{'ratingKey':v} for v in ids]}}),raise_for_status=Mock())
    def transfer(self,*args,**kwargs):
        self.assertIn('/playlists/p/items',args[0]);self.assertEqual(kwargs['params'],{'playQueueID':'queue'});self.contents[:]=self.expected;return self.response([])
    def test_windowed_creation_fetches_full_queue_and_preserves_86_occurrences(self):
        self.expected=[str(i) for i in range(84)]+['1','2']
        with patch.object(network,'post',return_value=self.response(self.expected[:50],total=86)) as post,patch.object(network,'get',return_value=self.response(self.expected)) as get,patch.object(network,'put',side_effect=self.transfer):
            self.assertTrue(self.client.replace_playlist_tracks('p',self.expected))
        self.assertEqual(self.contents,self.expected);self.assertEqual(post.call_args.kwargs['params']['window'],86)
        self.assertEqual(get.call_args.kwargs['params']['window'],86);self.assertEqual(self.repo.load('playlist_write_recovery'),{})
    def test_full_creation_does_not_need_another_queue_read(self):
        self.expected=['a','b','a']
        with patch.object(network,'post',return_value=self.response(self.expected)),patch.object(network,'get') as get,patch.object(network,'put',side_effect=self.transfer):
            self.assertTrue(self.client.replace_playlist_tracks('p',self.expected))
        get.assert_not_called()
    def test_collapsed_bulk_queue_rebuilt_with_each_occurrence(self):
        self.expected=['a','b','a'];queue=['a']
        def put(url,**kwargs):
            if '/playlists/' in url:return self.transfer(url,**kwargs)
            queue.append(kwargs['params']['uri'].rsplit('/',1)[-1]);return self.response(queue.copy())
        with patch.object(network,'post',side_effect=[self.response(['a','b']),self.response(['a'])]),patch.object(network,'get',return_value=self.response(['a','b'])),patch.object(network,'put',side_effect=put) as put:
            self.assertTrue(self.client.replace_playlist_tracks('p',self.expected))
        self.assertEqual(queue,self.expected);self.assertEqual(self.contents,self.expected);self.assertEqual(put.call_count,3)
    def test_queue_still_collapses_repeats_destination_is_never_cleared(self):
        self.expected=['a','b','a']
        with patch.object(network,'post',side_effect=[self.response(['a','b']),self.response(['a'])]),patch.object(network,'get',return_value=self.response(['a','b'])),patch.object(network,'put',return_value=self.response(['a','b'])),patch.object(jobs,'output') as output:
            self.assertFalse(self.client.replace_playlist_tracks('p',self.expected))
        self.assertEqual(self.clears,0);self.assertEqual(self.contents,['old'])
        messages=' '.join(str(c.args[0]) for c in output.call_args_list)
        self.assertIn('missing library IDs: a x1',messages);self.assertNotIn('secret-token-value',messages)
    def test_individual_queue_append_failure_preserves_previous_contents(self):
        with patch.object(network,'post',side_effect=[self.response(['a','b']),self.response(['a'])]),patch.object(network,'get',return_value=self.response(['a','b'])),patch.object(network,'put',side_effect=network.exceptions.Timeout('secret-token-value')):
            self.assertFalse(self.client.replace_playlist_tracks('p',['a','b','a']))
        self.assertEqual(self.clears,0);self.assertEqual(self.contents,['old'])
    def test_incomplete_full_window_is_not_accepted(self):
        self.expected=['a','b','a']
        with patch.object(network,'post',side_effect=[self.response(['a'],total=3),self.response(['a'])]),patch.object(network,'get',return_value=self.response(['a','b'],total=3)),patch.object(network,'put',return_value=self.response(['a','b'],total=3)):
            self.assertFalse(self.client.replace_playlist_tracks('p',self.expected))
        self.assertEqual(self.contents,['old']);self.assertEqual(self.clears,0)
    def test_verified_queue_transfer_drops_repeat_completes(self):
        def transfer(*args,**kwargs):self.contents[:]=['a','b'];return self.response([])
        with patch.object(network,'post',return_value=self.response(['a','b','a'])),patch.object(network,'put',side_effect=transfer):
            self.assertTrue(self.client.replace_playlist_tracks('p',['a','b','a']))
        self.assertEqual(self.contents,['a','b']);self.assertEqual(self.repo.load('playlist_write_recovery'),{})
    def test_queue_transfer_failure_restores_previous(self):
        with patch.object(network,'post',return_value=self.response(['a','a'])),patch.object(network,'put',return_value=self.response([],status=500)):
            self.assertFalse(self.client.replace_playlist_tracks('p',['a','a']))
        self.assertEqual(self.contents,['old']);self.assertEqual(self.repo.load('playlist_write_recovery'),{})
    def test_other_queue_returned_during_append_is_rejected(self):
        with patch.object(network,'post',side_effect=[self.response(['a']),self.response(['a'])]),patch.object(network,'get',return_value=self.response(['a'])),patch.object(network,'put',return_value=self.response(['a','a'],queue='other')):
            self.assertFalse(self.client.replace_playlist_tracks('p',['a','a']))
        self.assertEqual(self.clears,0);self.assertEqual(self.contents,['old'])
    def test_missing_queue_id_leaves_playlist_untouched(self):
        response=Mock(json=Mock(return_value={'MediaContainer':{'Metadata':[]}}))
        with patch.object(network,'post',return_value=response),patch.object(network,'put') as put:
            self.assertFalse(self.client.replace_playlist_tracks('p',['a','a']))
        put.assert_not_called();self.assertEqual(self.clears,0);self.assertEqual(self.contents,['old'])

    def test_failed_rollback_retains_durable_snapshot(self):
        def transfer(*args,**kwargs):self.contents[:]=['a'];return self.response([])
        self.client.add_to_playlist=lambda *_:False
        with patch.object(network,'post',return_value=self.response(['a','b','a'])),patch.object(network,'put',side_effect=transfer):
            self.assertFalse(self.client.replace_playlist_tracks('p',['a','b','a']))
        self.assertEqual(next(iter(self.repo.load('playlist_write_recovery').values()))['previous'],['old'])

    def test_distinct_recording_missing_is_not_duplicate_collapse(self):
        self.contents=['recording1']
        self.assertFalse(self.client._verify_playlist_tracks('p',['recording1','recording2'],allow_duplicate_collapse=True))

    def test_reordered_tracks_are_not_accepted(self):
        self.contents=['b','a','c']
        self.assertFalse(self.client._verify_playlist_tracks('p',['a','b','c','a'],allow_duplicate_collapse=True))

    def test_recovery_verification_requires_every_occurrence(self):
        self.contents=['a','b']
        self.assertFalse(self.client._verify_playlist_tracks('p',['a','b','a']))

    def test_entire_track_missing_restores_previous(self):
        def transfer(*args,**kwargs):self.contents[:]=['a'];return self.response([])
        with patch.object(network,'post',return_value=self.response(['a','b','a'])),patch.object(network,'put',side_effect=transfer):
            self.assertFalse(self.client.replace_playlist_tracks('p',['a','b','a']))
        self.assertEqual(self.contents,['old'])
        self.assertEqual(self.repo.load('playlist_write_recovery'),{})

if __name__=='__main__':unittest.main()
