import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class LocalStartupTests(unittest.TestCase):
    def test_bind_failure_stops_before_browser_scheduler_or_cleanup(self):
        with patch.object(app, 'LocalServer', side_effect=SystemExit(1)) as server, \
             patch.object(app.threading, 'Thread') as thread, \
             patch.object(app, '_ensure_classified') as initialize:
            with self.assertRaises(SystemExit):
                app.run_local_app()
        server.assert_called_once_with('127.0.0.1', 8000, app.app)
        thread.assert_not_called()
        initialize.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusive loopback socket')
    def test_windows_server_owns_an_exclusive_listener(self):
        with app.LocalServer('127.0.0.1', 0, app.app) as server:
            self.assertTrue(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE))
            self.assertFalse(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR))
            self.assertEqual(server.socket.getsockname()[0], '127.0.0.1')
