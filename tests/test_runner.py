import io
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import run


class RunnerLifetime(unittest.TestCase):
    def test_health_requires_matching_project_and_healthy_status(self):
        for health, expected in [
            ({"app_id": "funasr-local-studio", "instance": "project", "status": "ok"}, True),
            ({"app_id": "other", "instance": "project", "status": "ok"}, False),
            ({"app_id": "funasr-local-studio", "instance": "other", "status": "ok"}, False),
            ({"app_id": "funasr-local-studio", "instance": "project", "status": "failed"}, False),
            ([], False),
        ]:
            opener = SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(json.dumps(health).encode()))
            with self.subTest(health=health), patch("run.urllib.request.build_opener", return_value=opener):
                self.assertEqual(run.service_is_ready("http://127.0.0.1:8765", "project"), expected)
        with patch("run.urllib.request.build_opener", side_effect=OSError()):
            self.assertFalse(run.service_is_ready("http://127.0.0.1:8765", "project"))

    def test_occupied_port_with_matching_server_is_reused(self):
        with patch("run.socket.socket") as socket, patch("run.service_is_ready", return_value=True):
            socket.return_value.__enter__.return_value.bind.side_effect = OSError()
            self.assertEqual(run.select_port(8765, "project"), (8765, True))

    def test_unrelated_port_is_skipped(self):
        with patch("run.socket.socket") as socket, patch("run.service_is_ready", return_value=False):
            socket.return_value.__enter__.return_value.bind.side_effect = [OSError(), None]
            self.assertEqual(run.select_port(8765, "project"), (8766, False))

    def test_healthy_existing_backend_does_not_end_launcher(self):
        with patch("run.time.sleep", side_effect=[None, KeyboardInterrupt()]), \
             patch("run.service_is_ready", return_value=True) as ready, patch("run.socket.create_connection") as connect:
            with self.assertRaises(KeyboardInterrupt):
                run.monitor_existing("http://127.0.0.1:8765", "project", 8765)
        self.assertEqual(ready.call_count, 1)
        connect.assert_not_called()

    def test_closed_backend_port_triggers_takeover_after_repeated_failures(self):
        with patch("run.time.sleep"), patch("run.service_is_ready", return_value=False) as ready, \
             patch("run.socket.create_connection", side_effect=ConnectionRefusedError()):
            run.monitor_existing("http://127.0.0.1:8765", "project", 8765)
        self.assertEqual(ready.call_count, 3)

    def test_slow_backend_with_open_port_is_not_replaced(self):
        with patch("run.time.sleep", side_effect=[None] * 4 + [KeyboardInterrupt()]), \
             patch("run.service_is_ready", side_effect=[False, False, False, True]), \
             patch("run.socket.create_connection") as connect:
            with self.assertRaises(KeyboardInterrupt):
                run.monitor_existing("http://127.0.0.1:8765", "project", 8765)
        self.assertEqual(connect.call_count, 1)

    def test_main_keeps_monitoring_then_starts_foreground_server_when_original_stops(self):
        uvicorn = MagicMock()
        with patch.object(sys, "argv", ["run.py", "--no-browser"]), \
             patch("run.select_port", side_effect=[(8765, True), (8765, False)]), \
             patch("run.monitor_existing") as monitor, patch.dict(sys.modules, {"uvicorn": uvicorn}), \
             patch("run.webbrowser.open") as browser:
            run.main()
        monitor.assert_called_once()
        uvicorn.run.assert_called_once_with("backend.app:app", host="127.0.0.1", port=8765,
                                            log_level="info", access_log=False)
        browser.assert_not_called()

    def test_stopping_monitor_does_not_start_or_stop_original_server(self):
        uvicorn = MagicMock()
        with patch.object(sys, "argv", ["run.py", "--no-browser"]), \
             patch("run.select_port", return_value=(8765, True)), \
             patch("run.monitor_existing", side_effect=KeyboardInterrupt()), \
             patch.dict(sys.modules, {"uvicorn": uvicorn}):
            run.main()
        uvicorn.run.assert_not_called()

    def test_browser_waits_until_project_backend_is_ready(self):
        with patch("run.service_is_ready", side_effect=[False, True]), \
             patch("run.time.sleep") as sleep, patch("run.webbrowser.open") as browser:
            run.open_when_ready("http://127.0.0.1:8765", "project")
        sleep.assert_called_once()
        browser.assert_called_once_with("http://127.0.0.1:8765")
