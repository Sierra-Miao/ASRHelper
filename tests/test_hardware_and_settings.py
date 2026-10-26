import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.config import AISettings, LEGACY_PROMPT, PROMPT, SettingsStore
from hardware import backend_matches, choose_backend, select_runtime_device

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / "test-results" / "tmp"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def nvidia_info(cuda="12.8", capability=8.6, memory=8192):
    return {"logical_cores": 12, "physical_cores": 6, "gpu_names": ["NVIDIA Test GPU"],
            "cuda_version": cuda, "nvidia_gpus": [{"index": 0, "name": "NVIDIA Test GPU",
            "memory_mb": memory, "compute_cap": capability}]}


class HardwareSelection(unittest.TestCase):
    def test_amd_windows_keeps_cpu_and_uses_physical_cores(self):
        plan = choose_backend({"logical_cores": 12, "physical_cores": 6,
                               "gpu_names": ["AMD Radeon(TM) Graphics"]})
        self.assertEqual(plan["backend"], "cpu")
        self.assertEqual(plan["cpu_threads"], 6)
        self.assertIn("AMD", plan["reason"])

    def test_cuda_backend_matches_driver_and_architecture(self):
        for driver, capability, expected in (("12.8", 8.6, "cu128"), ("13.0", 12.0, "cu128"),
                ("12.6", 8.6, "cu126"), ("13.0", 6.1, "cu126"), ("12.5", 8.6, "cpu"),
                ("12.6", 12.0, "cpu"), ("12.8", 3.5, "cpu"), ("12.8", None, "cpu")):
            with self.subTest(driver=driver, capability=capability):
                self.assertEqual(choose_backend(nvidia_info(driver, capability))["backend"], expected)

    def test_gpu_selection_and_forced_cpu(self):
        info = nvidia_info()
        info["nvidia_gpus"].append({"index": 1, "name": "Larger GPU", "memory_mb": 16384, "compute_cap": 8.9})
        self.assertEqual(choose_backend(info)["gpu_index"], 1)
        self.assertEqual(choose_backend(info, force_cpu=True)["device"], "cpu")

    def test_gpu_package_variant_is_not_mistaken_for_cpu(self):
        self.assertFalse(backend_matches("2.8.0+cpu", "cu128"))
        self.assertFalse(backend_matches("2.8.0+cu126", "cu128"))
        self.assertTrue(backend_matches("2.8.0+cu128", "cu128"))
        self.assertTrue(backend_matches("2.8.0+cu128", "cpu"))
        lock = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8-sig")
        self.assertIn("torch==2.8.0\n", lock)
        self.assertNotIn("torch==2.8.0+cpu", lock)

    def test_runtime_verifies_real_cuda_operation_and_falls_back(self):
        torch = MagicMock()
        torch.cuda.is_available.return_value = True
        torch.cuda.device_count.return_value = 1
        torch.cuda.get_device_name.return_value = "Test GPU"
        with patch("hardware.hardware_plan", return_value=choose_backend(nvidia_info())):
            device, _ = select_runtime_device(torch)
            self.assertEqual(device, "cuda:0")
            torch.tensor.assert_called_once_with([1.0], device="cuda:0")
            torch.tensor.side_effect = RuntimeError("no compatible kernel")
            device, reason = select_runtime_device(torch)
            self.assertEqual(device, "cpu")
            self.assertIn("回退", reason)


class PersistentSettings(unittest.TestCase):
    def test_legacy_default_migrates_without_losing_credentials(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory).resolve()
            self.assertTrue(root.is_relative_to(TEMP_ROOT.resolve()))
            path = root / "settings.json"
            path.write_text(json.dumps({"base_url": "http://localhost:1234/v1", "model": "local", "enabled": True,
                                       "api_key": "test-persisted-key", "prompt": LEGACY_PROMPT}), encoding="utf-8")
            store = SettingsStore(path)
            self.assertEqual(store.snapshot().prompt, PROMPT)
            self.assertEqual(store.snapshot().api_key, "test-persisted-key")
            self.assertTrue(store.snapshot().enabled)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["prompt"], PROMPT)
            self.assertNotIn("test-persisted-key", json.dumps(store.public()))

    def test_custom_prompt_survives_restart(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory).resolve()
            self.assertTrue(root.is_relative_to(TEMP_ROOT.resolve()))
            path = root / "settings.json"
            store = SettingsStore(path)
            store.update(AISettings(base_url="http://localhost:1234/v1", model="local", api_key="test-only",
                                    prompt="这是用户自定义的提示词", enabled=True))
            restored = SettingsStore(path)
            self.assertEqual(restored.snapshot().prompt, "这是用户自定义的提示词")
            self.assertEqual(restored.snapshot().api_key, "test-only")
            self.assertTrue(restored.snapshot().enabled)

    def test_default_prompt_sets_editorial_boundaries(self):
        for requirement in ("没有听到原始音频", "同一句话", "不同说话人", "无法确定时保留原文", "不总结", "不漏行", "时间戳"):
            self.assertIn(requirement, PROMPT)


if __name__ == "__main__":
    unittest.main()
