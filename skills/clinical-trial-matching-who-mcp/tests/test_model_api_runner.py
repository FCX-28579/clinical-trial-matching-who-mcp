from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PIPELINE = Path(__file__).resolve().parents[1] / "scripts" / "pipeline"
sys.path.insert(0, str(PIPELINE))

import model_api_runner
import model_batch_executor


class ModelApiRunnerTests(unittest.TestCase):
    def test_api_backend_is_selected_explicitly(self) -> None:
        with patch.dict(
            os.environ,
            {"MODEL_EXECUTION_BACKEND": "api", "MODEL_PROVIDER": "openai"},
            clear=True,
        ):
            command = model_batch_executor._runner_template()
        self.assertTrue(command[1].endswith("model_api_runner.py"))

    def test_openai_compatible_requires_https_except_local_opt_in(self) -> None:
        values = {
            "MODEL_PROVIDER": "openai-compatible",
            "MODEL_NAME": "local-model",
            "MODEL_BASE_URL": "http://127.0.0.1:11434/v1",
            "MODEL_ALLOW_EMPTY_API_KEY": "1",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                model_api_runner._configuration()
        values["MODEL_ALLOW_INSECURE_HTTP"] = "1"
        with patch.dict(os.environ, values, clear=True):
            _, provider, model, base, key = model_api_runner._configuration()
        self.assertEqual(provider.protocol, "openai-chat")
        self.assertEqual(model, "local-model")
        self.assertEqual(base, "http://127.0.0.1:11434/v1")
        self.assertEqual(key, "")

    def test_stage_specific_model_configuration_overrides_global_values(self) -> None:
        values = {
            "MODEL_PROVIDER": "openai",
            "MODEL_NAME": "global-model",
            "OPENAI_API_KEY": "global-key",
            "GATER_MODEL_PROVIDER": "minimax",
            "GATER_MODEL_NAME": "MiniMax-M2.7-highspeed",
            "GATER_MODEL_API_KEY": "stage-key",
        }
        with patch.dict(os.environ, values, clear=True):
            name, _, model, _, key = model_api_runner._configuration("gater")
        self.assertEqual(name, "minimax")
        self.assertEqual(model, "MiniMax-M2.7-highspeed")
        self.assertEqual(key, "stage-key")

    def test_prompt_embeds_referenced_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "skills" / "trial-gater" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("GATE-INSTRUCTION", encoding="utf-8")
            envelope = {
                "skill_paths": {"trial_gater": str(skill)},
                "job": {"trials": [{"id": "NCT1"}]},
            }
            prompt = model_api_runner.build_prompt(envelope)
        self.assertIn("GATE-INSTRUCTION", prompt)
        self.assertIn("NCT1", prompt)

    def test_prompt_loads_linked_schema_and_only_required_stage_skill(self) -> None:
        skills_root = Path(__file__).resolve().parents[2]
        envelope = {
            "skill_paths": {
                name: str(skills_root / name / "SKILL.md")
                for name in (
                    "trial-gater",
                    "trial-risk-annotator",
                    "trial-efficacy-contextualizer",
                    "decision-synthesizer",
                )
            },
            "job": {
                "stage": "gater",
                "required_execution_order": ["trial-gater"],
                "patient": {},
                "trials": [{"id": "NCT1"}],
            },
            "required_output": {"expected_trial_ids": ["NCT1"]},
        }
        prompt = model_api_runner.build_prompt(envelope)
        self.assertIn("trial-gater/rules/output-gating-verdict-schema.md", prompt)
        self.assertIn('"confidence"', prompt)
        self.assertIn('"blockers_pending"', prompt)
        self.assertGreater(
            prompt.index("REQUIRED OUTPUT SCHEMAS"),
            prompt.index("--- JOB ENVELOPE ---"),
        )
        self.assertNotIn("trial-risk-annotator/output", prompt)
        self.assertNotIn("trial-risk-annotator output schema", prompt)

    def test_deep_prompt_loads_only_applicable_risk_rules(self) -> None:
        skills_root = Path(__file__).resolve().parents[2]
        envelope = {
            "skill_paths": {
                name: str(skills_root / name / "SKILL.md")
                for name in (
                    "trial-risk-annotator",
                    "trial-efficacy-contextualizer",
                )
            },
            "job": {
                "stage": "deep",
                "required_execution_order": [
                    "trial-risk-annotator",
                    "trial-efficacy-contextualizer",
                ],
                "patient": {"cancer_type": "NSCLC"},
                "trials": [{"id": "NCT1", "phases": ["PHASE1"]}],
            },
            "required_output": {"expected_trial_ids": ["NCT1"]},
        }
        prompt = model_api_runner.build_prompt(envelope)
        self.assertIn("output-risk-annotation-schema.md", prompt)
        self.assertIn("output-efficacy-context-schema.md", prompt)
        self.assertIn("risk-phase-1-dose-escalation.md", prompt)
        self.assertNotIn("--- BEGIN trial-risk-annotator/rules/risk-kras-g12c-by-cancer.md ---", prompt)
        self.assertIn("combine risk and efficacy into the same array item", prompt)

    def test_recursive_resource_loading_is_bounded_to_skill_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "trial-gater" / "SKILL.md"
            outside = root / "outside.md"
            skill.parent.mkdir(parents=True)
            outside.write_text("SECRET", encoding="utf-8")
            skill.write_text("[escape](../../outside.md)", encoding="utf-8")
            envelope = {
                "skill_paths": {"trial-gater": str(skill)},
                "job": {"required_execution_order": ["trial-gater"], "trials": []},
            }
            with self.assertRaisesRegex(ValueError, "escapes"):
                model_api_runner.build_prompt(envelope)

    def test_missing_required_skill_resource_fails_before_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "skills" / "trial-gater" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("[schema](rules/missing-schema.md)", encoding="utf-8")
            envelope = {
                "skill_paths": {"trial-gater": str(skill)},
                "job": {"required_execution_order": ["trial-gater"], "trials": []},
            }
            with self.assertRaises(FileNotFoundError):
                model_api_runner.build_prompt(envelope)

    def test_prompt_repeats_batch_contract_after_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "skills" / "trial-gater" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("GATE-INSTRUCTION", encoding="utf-8")
            envelope = {
                "skill_paths": {"trial_gater": str(skill)},
                "job": {"trials": [{"id": "NCT1"}, {"id": "NCT2"}]},
                "required_output": {"expected_trial_ids": ["NCT1", "NCT2"]},
            }
            prompt = model_api_runner.build_prompt(envelope)
        self.assertTrue(prompt.endswith('["NCT1", "NCT2"]. Do not return a single trial object directly.'))

    def test_openai_chat_response_is_parsed(self) -> None:
        payload = {
            "choices": [{"message": {"content": '{"analyzed_trials":[{"trial_id":"NCT1"}]}'}}]
        }
        text = model_api_runner._response_text("openai-chat", payload)
        self.assertEqual(model_api_runner._json_payload(text)["analyzed_trials"][0]["trial_id"], "NCT1")

    def test_reasoning_prefix_before_json_is_parsed(self) -> None:
        text = (
            "<think>reasoning omitted</think>\n"
            '{"analyzed_trials":[{"trial_id":"NCT1"}]}\nDone.'
        )
        self.assertEqual(
            model_api_runner._json_payload(text)["analyzed_trials"][0]["trial_id"],
            "NCT1",
        )

    def test_minimax_uses_its_current_token_parameter_without_invalid_temperature(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            _, _, body = model_api_runner._request(
                "minimax", "openai-chat", "https://api.minimax.io/v1",
                "secret", "MiniMax-M2.7", "prompt",
            )
        self.assertIn("max_completion_tokens", body)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)
        self.assertNotIn("response_format", body)

    def test_stage_specific_token_parameter_is_model_agnostic(self) -> None:
        with patch.dict(os.environ, {
            "TRANSLATION_MODEL_TOKEN_PARAMETER": "max_tokens",
            "TRANSLATION_MODEL_MAX_OUTPUT_TOKENS": "4096",
        }, clear=True):
            _, _, body = model_api_runner._request(
                "minimax", "openai-chat", "https://api.minimaxi.com/v1",
                "secret", "any-model-name", "prompt", stage="translation",
            )
        self.assertIn("max_tokens", body)
        self.assertNotIn("max_completion_tokens", body)
        self.assertEqual(body["max_tokens"], 4096)

    def test_post_retries_transient_http_error(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        error = __import__("urllib").error.HTTPError(
            "https://example.test", 503, "busy", {"Retry-After": "0"}, io.BytesIO(b"busy")
        )
        with patch.dict(os.environ, {"MODEL_API_RETRIES": "1"}, clear=True), patch(
            "model_api_runner.ssl.create_default_context", return_value=MagicMock()
        ), patch(
            "model_api_runner.urllib.request.urlopen", side_effect=[error, response]
        ) as opener:
            result = model_api_runner._post("https://example.test", {}, {})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(opener.call_count, 2)

    def test_explicit_token_truncation_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            model_api_runner._assert_complete_response(
                "openai-chat", {"choices": [{"finish_reason": "length"}]}
            )
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            model_api_runner._assert_complete_response(
                "anthropic-messages", {"stop_reason": "max_tokens"}
            )
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            model_api_runner._assert_complete_response(
                "openai-responses", {"status": "incomplete"}
            )


if __name__ == "__main__":
    unittest.main()
