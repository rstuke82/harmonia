import unittest

from harmonia.album_catalog import catalog_rows, library_state
from harmonia.jobs import worker_for
from harmonia.tasks import DEFINITIONS


ARTIST = '11111111-1111-1111-1111-111111111111'
OWNED = '22222222-2222-2222-2222-222222222222'
MISSING = '33333333-3333-3333-3333-333333333333'


class Beta8Library(unittest.TestCase):
    def test_bulk_metadata_refresh_is_a_metadata_task(self):
        self.assertEqual(worker_for('library_metadata_all'), 'metadata')
        self.assertIn(('library_metadata_all', 'all', 'Refresh Library Metadata', 168, '03:30', 'metadata'), DEFINITIONS)

    def test_lidarr_state_is_mutually_exclusive(self):
        self.assertEqual(library_state({'in_lidarr': True, 'monitored': True, 'statistics': {'trackFileCount': 1}}), 'In Library')
        self.assertEqual(library_state({'in_lidarr': True, 'monitored': True, 'statistics': {'trackFileCount': 0}}), 'Wanted')
        self.assertEqual(library_state({'in_lidarr': True, 'monitored': False, 'statistics': {'trackFileCount': 0}}), 'Missing')
        self.assertEqual(library_state({'in_lidarr': False, 'monitored': True, 'statistics': {'trackFileCount': 10}}), 'Missing')

    def test_catalog_adds_missing_album_and_apple_identity(self):
        lidarr = [{'album_id': OWNED, 'title': 'Owned', 'artist': 'Artist', 'artist_id': 7,
                   'artist_mbid': ARTIST, 'monitored': True, 'statistics': {'trackFileCount': 8}}]
        artists = [{'id': 7, 'name': 'Artist', 'mbid': ARTIST}]
        catalogs = {ARTIST: {'rows': [
            {'id': OWNED, 'title': 'Owned', 'primary_type': 'Album', 'official': True},
            {'id': MISSING, 'title': 'Missing', 'primary_type': 'Album', 'first_release_date': '2001-02-03', 'official': True},
        ]}}
        apple = {ARTIST: {'albums': [{'id': 42, 'title': 'Missing', 'url': 'https://music.apple.com/album/42'}]}}
        rows = catalog_rows(lidarr, artists, catalogs, apple)
        self.assertEqual(len(rows), 2)
        absent = next(r for r in rows if r['album_id'] == MISSING)
        self.assertFalse(absent['in_lidarr'])
        self.assertEqual(absent['library_state'], 'Missing')
        self.assertEqual(absent['itunes_id'], 42)
        self.assertEqual(absent['year'], '2001')
        owned = next(r for r in rows if r['album_id'] == OWNED)
        self.assertEqual(owned['identity_source'], 'Lidarr')
        self.assertEqual(owned['library_state'], 'In Library')

    def test_unique_catalog_title_supplies_missing_lidarr_identity(self):
        rows = catalog_rows(
            [{'album_id': '', 'title': 'Only Match', 'artist': 'Artist', 'artist_id': 7,
              'artist_mbid': ARTIST, 'monitored': False, 'statistics': {}}],
            [{'id': 7, 'name': 'Artist', 'mbid': ARTIST}],
            {ARTIST: {'rows': [{'id': MISSING, 'title': 'Only Match', 'official': True}]}},
            {},
        )
        self.assertEqual(rows[0]['musicbrainz_id'], MISSING)
        self.assertEqual(rows[0]['identity_source'], 'MusicBrainz')

    def test_unverified_or_unofficial_catalog_rows_are_hidden(self):
        rows = catalog_rows([], [{'id': 7, 'name': 'Artist', 'mbid': ARTIST}], {
            ARTIST: {'rows': [
                {'id': OWNED, 'title': 'Old cache without status'},
                {'id': MISSING, 'title': 'Official', 'official': True},
            ]}}, {})
        self.assertEqual([r['title'] for r in rows], ['Official'])


if __name__ == '__main__':
    unittest.main()
