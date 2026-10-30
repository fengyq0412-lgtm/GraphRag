import io
import json
import os
import unittest
from unittest.mock import patch

from graphrag_lite import model


class ModelTests(unittest.TestCase):
    def test_chat_uses_timeout_and_optional_thinking_setting(self):
        settings = {"LLM_API_KEY": "test-key", "LLM_MODEL": "test-model",
                    "LLM_TIMEOUT": "180", "LLM_ENABLE_THINKING": "false"}
        response = io.BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')
        with patch.dict(os.environ, settings, clear=True), patch.object(model, "load_env"), \
                patch.object(model.urllib.request, "urlopen", return_value=response) as open_request:
            self.assertEqual(model.chat("system", "text", json_mode=True), "{}")
        request = open_request.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(open_request.call_args.kwargs["timeout"], 180)
        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_timeout_has_actionable_error(self):
        settings = {"LLM_API_KEY": "test-key", "LLM_MODEL": "test-model", "LLM_TIMEOUT": "30"}
        with patch.dict(os.environ, settings, clear=True), patch.object(model, "load_env"), \
                patch.object(model.urllib.request, "urlopen", side_effect=TimeoutError("read operation timed out")):
            with self.assertRaisesRegex(model.ModelError, "超过 30 秒"):
                model.chat("system", "text")


if __name__ == "__main__":
    unittest.main()
