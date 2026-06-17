"""Unit tests for LLM model configs (SQLite, optional encryption, costs)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import llm


class LLMConfigTests(unittest.TestCase):
    def test_plain_roundtrip_with_costs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                self.assertEqual(llm.list_models(), [])
                llm.save_model("m1", "gpt-4o-mini", api_key="sk-test",
                               track_costs=True, cost_input=0.15, cost_output=0.6)
                models = llm.list_models()
                self.assertEqual(len(models), 1)
                self.assertEqual(models[0]["name"], "m1")
                self.assertFalse(models[0]["encrypted"])
                self.assertTrue(models[0]["track_costs"])
                self.assertEqual(models[0]["cost_input"], 0.15)
                loaded = llm.load_model("m1")
                self.assertEqual(loaded["api_key"], "sk-test")
                self.assertEqual(loaded["model"], "gpt-4o-mini")

    def test_user_presets_add_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                self.assertEqual(llm.user_presets(), {"remote": [], "local": []})
                self.assertTrue(llm.add_user_preset("remote", "my/custom-model"))
                self.assertTrue(llm.add_user_preset("local", "ollama/my-model"))
                # duplicates are ignored; empty is a no-op; unknown kind -> remote
                llm.add_user_preset("remote", "my/custom-model")
                self.assertFalse(llm.add_user_preset("remote", "   "))
                llm.add_user_preset("bogus", "x/y")
                p = llm.user_presets()
                self.assertEqual(p["remote"], ["my/custom-model", "x/y"])
                self.assertEqual(p["local"], ["ollama/my-model"])

    def test_encrypted_needs_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                llm.save_model("e", "gpt-4o-mini", api_key="sk-x",
                               encrypt=True, secret="pw")
                self.assertTrue(llm.list_models()[0]["encrypted"])
                with self.assertRaises(llm.LLMError):
                    llm.load_model("e")
                with self.assertRaises(llm.LLMError):
                    llm.load_model("e", secret="wrong")
                self.assertEqual(llm.load_model("e", secret="pw")["api_key"],
                                 "sk-x")

    def test_name_and_model_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                with self.assertRaises(llm.LLMError):
                    llm.save_model("", "gpt-4o-mini")
                with self.assertRaises(llm.LLMError):
                    llm.save_model("m", "")

    def test_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                llm.save_model("m", "ollama/llama3")
                llm.delete_model("m")
                self.assertEqual(llm.list_models(), [])

    def test_edit_keeps_key_when_update_key_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                llm.save_model("m", "gpt-4o-mini", api_key="sk-keep",
                               track_costs=False)
                # edit metadata only - key must survive
                llm.save_model("m", "gpt-4o", api_base="https://x",
                               track_costs=True, cost_input=1.0,
                               update_key=False)
                loaded = llm.load_model("m")
                self.assertEqual(loaded["api_key"], "sk-keep")    # preserved
                self.assertEqual(loaded["model"], "gpt-4o")       # updated
                self.assertEqual(loaded["api_base"], "https://x")
                self.assertTrue(llm.list_models()[0]["track_costs"])
                # typing a new key replaces it
                llm.save_model("m", "gpt-4o", api_key="sk-new", update_key=True)
                self.assertEqual(llm.load_model("m")["api_key"], "sk-new")

    def test_edit_nonexistent_with_update_key_false_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                with self.assertRaises(llm.LLMError):
                    llm.save_model("ghost", "gpt-4o", update_key=False)

    def test_seed_defaults_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                llm.ensure_default_models()
                names = {m["name"] for m in llm.list_models()}
                self.assertIn("gpt-4o-mini", names)
                self.assertIn("ollama-llama3", names)
                # the cloud default carries no API key (uses the env var)
                gpt = next(m for m in llm.list_models()
                           if m["name"] == "gpt-4o-mini")
                self.assertFalse(gpt["has_key"])
                self.assertEqual(llm.load_model("gpt-4o-mini")["api_key"], "")
                # a deleted default is NOT recreated on the next call
                llm.delete_model("gpt-4o-mini")
                llm.ensure_default_models()
                self.assertNotIn(
                    "gpt-4o-mini", {m["name"] for m in llm.list_models()})

    def test_cost_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                llm.log_cost("m", 1000, 200, 0.05)
                llm.log_cost("m", 500, 100, 0.02)
                log = llm.cost_log("m")
                self.assertEqual(log["total"]["calls"], 2)
                self.assertEqual(log["total"]["prompt_tokens"], 1500)
                self.assertAlmostEqual(log["total"]["cost"], 0.07)
                self.assertEqual(len(log["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
