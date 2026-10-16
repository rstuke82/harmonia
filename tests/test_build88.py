"""Package and database naming contracts for fresh Harmonia installations."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from harmonia.storage import Repository
from harmonia import __version__, __build__
import re
from scripts.package_source import package

class HarmoniaNames(unittest.TestCase):
    def test_new_package_launcher(self):
        result = subprocess.run([sys.executable, '-m', 'harmonia', 'version'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), __version__)

    def test_root_and_personal_database_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Repository(folder)
            personal = Repository(Path(folder)/'users'/'123')
            for repo in (root, personal):
                self.assertEqual(repo.path.name, 'harmonia.db')
                repo.put('example', 'key', {'ok':True})
                self.assertEqual(repo.load_one('example', 'key'), {'ok':True})

    def test_source_archive_contains_application_package(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'source'
            (root/'harmonia').mkdir(parents=True)
            (root/'harmonia'/'__init__.py').write_text('')
            (root/'harmonia.db').write_text('runtime data')
            archive = Path(folder)/'source.zip'
            package(root, archive)
            with zipfile.ZipFile(archive) as z:
                self.assertIn('harmonia/__init__.py', z.namelist())
                self.assertNotIn('harmonia.db', z.namelist())

    def test_container_metadata_matches_application(self):
        dockerfile = (Path(__file__).resolve().parents[1]/'Dockerfile').read_text()
        self.assertEqual(re.search(r'^ARG VERSION=(.+)$', dockerfile, re.M)[1], __version__)
        self.assertEqual(re.search(r'^ARG BUILD=(.+)$', dockerfile, re.M)[1], __build__)
        self.assertEqual(__version__.rsplit('.', 1)[1], __build__)
