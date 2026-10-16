import tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from harmonia import jobs,musicbrainz_client
from harmonia.storage import Repository
from harmonia.musicbrainz_settings import ordered

class LookupCancellation(unittest.TestCase):
 def test_cancel_releases_identity_and_rejects_late_updates(self):
  with tempfile.TemporaryDirectory() as folder:
   store=jobs.Store(Repository(Path(folder)))
   first=store.enqueue('musicbrainz_lookup',{'title':'Song'})
   store.update(first,status='running')
   store.cancel(first)
   self.assertEqual(store.get(first)['status'],'cancelled')
   second=store.enqueue('musicbrainz_lookup',{'title':'Song'})
   self.assertNotEqual(first,second)
   store.update(first,status='completed',result={'rows':['late']})
   self.assertEqual(store.get(first)['status'],'cancelled')
   self.assertIsNone(store.get(first)['result'])
 def test_cancelled_lookup_does_not_use_live_slot_but_maintenance_waits(self):
  workers=[{'action':'musicbrainz_lookup','account':'owner','cancelled':True} for _ in range(2)]
  self.assertIsNone(jobs.wait_reason('musicbrainz_lookup','owner',workers))
  self.assertIsNotNone(jobs.wait_reason('backup','owner',workers))
  self.assertIsNotNone(jobs.wait_reason('musicbrainz_lookup','owner',workers*4))
 def test_public_rate_lock_does_not_cover_network_wait(self):
  started=threading.Event();finish=threading.Event();errors=[]
  def slow(*args,**kwargs):
   started.set();finish.wait(3);return Mock(json=lambda:{})
  def run():
   try:musicbrainz_client._request('https://musicbrainz.org','recording',{},True,(1,1))
   except Exception as e:errors.append(e)
  with patch.object(musicbrainz_client.network,'get',side_effect=slow),patch.object(musicbrainz_client,'_public_last',0):
   thread=threading.Thread(target=run);thread.start()
   try:
    self.assertTrue(started.wait(2))
    acquired=musicbrainz_client._public_lock.acquire(timeout=.1)
    if acquired:musicbrainz_client._public_lock.release()
    self.assertTrue(acquired)
   finally:finish.set();thread.join(3)
  self.assertEqual(errors,[])
 def test_cancel_during_network_discards_response(self):
  ctx=Mock();ctx.checkpoint.side_effect=[None,jobs.Cancelled()]
  with patch('harmonia.jobs.current',return_value=ctx),patch.object(musicbrainz_client.network,'get',return_value=Mock(json=lambda:{'late':True})):
   with self.assertRaises(jobs.Cancelled):musicbrainz_client._request('http://mirror','recording',{},False,(1,1))
 def test_own_releases_first_and_studio_before_live(self):
  rows=[{'title':'Compilation','type':'Album','other_release':True,'secondary_types':['Compilation']},{'title':'Single','type':'Single','other_release':False},{'title':'Studio','type':'Album','other_release':False},{'title':'Live','type':'Album','other_release':True,'secondary_types':['Live']}]
  result=ordered(rows,{'release_priority':['Album','EP','Single','Other'],'prefer_studio':True})
  self.assertEqual([r['title'] for r in result[:2]],['Studio','Single'])
