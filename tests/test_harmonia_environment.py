"""Check configured startup paths, update channel and web listener together."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class HarmoniaEnvironment(unittest.TestCase):
    def test_named_environment_controls_startup(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "saved"
            script = """
import json
from unittest.mock import patch
from harmonia import api, updates
from harmonia.runtime_paths import CONFIG_DIR
from harmonia.storage import get_repository
repository = get_repository(CONFIG_DIR)
repository.save({'environment_test': {'retained': True}})
with patch('uvicorn.run') as run:
    api.run()
    print(json.dumps({'data': str(CONFIG_DIR), 'channel': updates.CHANNEL,
        'port': run.call_args.kwargs['port'],
        'proxies': run.call_args.kwargs['forwarded_allow_ips'],
        'saved': repository.load('environment_test')}))
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=root,
                env={
                    **os.environ,
                    "HARMONIA_DATA_DIR": str(data),
                    "HARMONIA_PORT": "8184",
                    "HARMONIA_UPDATE_CHANNEL": "beta",
                    "HARMONIA_HTTPS_ONLY": "true",
                    "HARMONIA_TRUSTED_PROXIES": "10.0.0.2",
                },
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "data": str(data.resolve()),
                    "channel": "beta",
                    "port": 8184,
                    "proxies": "10.0.0.2",
                    "saved": {"retained": True},
                },
            )
            self.assertTrue((data / "harmonia.db").exists())
