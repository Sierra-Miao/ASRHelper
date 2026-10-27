import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from backend.app import create_app
from backend.config import AISettings, SettingsStore
from backend.diagnostics import coverage
from backend.numbers import normalize_numbers
from backend.service import TranscriptionService, recognition_error
from asr_config import PROFILES, local_models, manifest_path, models_ready, recognition_options, selected_profile

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / "test-results" / "tmp"
TEMP.mkdir(parents=True, exist_ok=True)


class NumbersAndCoverage(unittest.TestCase):
    def test_quantities_dates_percent_and_leading_zeroes(self):
        pairs = {
            "二〇二六年十月二十六日花了一百二十八元": "2026年10月26日花了128元",
            "百分之十五，三点一四倍，第十二章，编号零零七": "15%，3.14倍，第12章，编号007",
            "十二点三十分，二十个人，两万三千元": "12点30分，20个人，23000元",
            "三心二意，万一失败，千万不要改，张三，一心一意": "三心二意，万一失败，千万不要改，张三，一心一意",
            "一百二元、百分之一百二": "一百二元、百分之一百二",
        }
        for before, after in pairs.items():
            with self.subTest(before=before):
                self.assertEqual(normalize_numbers(before), after)

    def test_coverage_does_not_silently_accept_missing_tail(self):
        report, warnings = coverage([{"start_ms": 0, "end_ms": 60000, "content": "正文"}], 180000)
        self.assertEqual(report["tail_gap_ms"], 120000)
        self.assertEqual(len(warnings), 1)
        report, warnings = coverage([{"start_ms": 0, "end_ms": 179900, "content": "正文"}], 180000)
        self.assertEqual(report["tail_gap_ms"], 100)
        self.assertFalse(warnings)

    def test_sparse_tail_and_memory_failure_have_actionable_messages(self):
        rows = [{"start_ms": i * 1000, "end_ms": i * 1000 + 900, "content": "嗯"} for i in range(300)]
        _, warnings = coverage(rows, 300000)
        self.assertTrue(any("极短句段" in warning for warning in warnings))
        self.assertIn("内存", recognition_error(MemoryError()))
        self.assertIn("内存", recognition_error(RuntimeError("CUDA out of memory")))


class Models(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_profiles_use_matching_itn_and_small_vad_batches(self):
        nano = recognition_options("nano", 2)
        self.assertTrue(nano["itn"])
        self.assertEqual(nano["preset_spk_num"], 2)
        self.assertEqual(nano["max_length"], 1024)
        self.assertTrue(recognition_options("sensevoice")["use_itn"])
        self.assertIn("punc_model", PROFILES["sensevoice"])
        self.assertNotIn("itn", recognition_options("paraformer"))

    def test_model_manifests_do_not_confuse_old_weights_with_nano(self):
        paths = {}
        for key in ("model", "vad_model", "punc_model", "spk_model"):
            folder = self.root / key
            folder.mkdir()
            (folder / "config.yaml").touch()
            (folder / ("campplus_cn_common.bin" if key == "spk_model" else "model.pt")).touch()
            paths[key] = key
        manifest = manifest_path("paraformer", self.root)
        manifest.parent.mkdir()
        manifest.write_text(json.dumps(paths), encoding="utf-8")
        self.assertTrue(models_ready("paraformer", self.root, speaker="cam++"))
        self.assertFalse(models_ready("nano", self.root))
        self.assertEqual(set(local_models("nano", self.root, speaker="cam++")), {"vad_model", "spk_model"})

    def service(self):
        with patch("backend.service.hardware_plan", return_value={"cpu_threads": 2}):
            service = TranscriptionService(self.root)
        self.addCleanup(lambda: service.executor.shutdown(wait=True))
        return service

    def test_web_download_requires_explicit_action_and_selection_persists(self):
        service = self.service()
        fake = MagicMock(kwargs={"device": "cpu"})
        with TestClient(create_app(service, SettingsStore(self.root / "settings.json"))) as client:
            catalog = client.get("/api/models").json()
            self.assertEqual({m["id"] for m in catalog["models"]}, set(PROFILES))
            with patch("backend.service.models_ready", return_value=False), patch("transcribe.load_pipeline", return_value=fake) as loader:
                self.assertEqual(client.post("/api/models/activate", json={"profile": "sensevoice"}).status_code, 409)
                self.assertFalse(loader.called)
                response = client.post("/api/models/activate", json={"profile": "sensevoice", "allow_download": True})
                self.assertEqual(response.status_code, 202)
                for _ in range(100):
                    if service.model_action["status"] != "loading":
                        break
                    time.sleep(.02)
                self.assertEqual(service.model_action["status"], "completed", service.model_action)
                loader.assert_called_once_with("sensevoice", speaker="eres2netv2")
                with patch.dict("os.environ", {"FUNASR_ASR_MODEL": ""}):
                    # Explicit overrides are tested separately from persisted defaults.
                    import os
                    os.environ.pop("FUNASR_ASR_MODEL")
                    self.assertEqual(selected_profile(self.root), "sensevoice")

    def test_failed_switch_retains_old_selection_and_blocks_concurrent_work(self):
        service = self.service()
        previous = service.profile
        with patch("transcribe.load_pipeline", side_effect=MemoryError()):
            service._activate_model("sensevoice")
        self.assertEqual(service.profile, previous)
        self.assertFalse((self.root / "asr_settings.json").exists())
        self.assertIn("内存", service.model_action["error"])
        service.model_action["status"] = "loading"
        with self.assertRaises(ValueError):
            service.register("audio.wav", 1, False)
        with self.assertRaises(ValueError):
            service.activate_model("paraformer")

    def test_batch_rows_persist_with_api_settings(self):
        store = SettingsStore(self.root / "settings.json")
        store.update(AISettings(batch_rows=150))
        self.assertEqual(SettingsStore(store.path).snapshot().batch_rows, 150)

    def test_requested_api_defaults_and_low_memory_preflight(self):
        self.assertEqual(AISettings().timeout, 900)
        self.assertEqual(AISettings().max_tokens, 40960)
        from transcribe import load_pipeline
        with patch("hardware.memory_status", return_value={"total_gb": 14, "available_gb": .5}):
            with self.assertRaisesRegex(MemoryError, "当前可用内存"):
                load_pipeline("nano")


if __name__ == "__main__":
    unittest.main()
