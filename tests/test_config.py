import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graphrag_lite.config import load_env


class ConfigTests(unittest.TestCase):
    def test_dotenv_loads_values_without_overriding_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('LLM_API_KEY="local#key"\nexport LLM_MODEL=test-model # note\nLLM_BASE_URL=https://example.test/v1\n')
            with patch.dict(os.environ, {"LLM_MODEL": "process-model"}, clear=True):
                load_env(path)
                self.assertEqual(os.environ["LLM_API_KEY"], "local#key")
                self.assertEqual(os.environ["LLM_MODEL"], "process-model")
                self.assertEqual(os.environ["LLM_BASE_URL"], "https://example.test/v1")


if __name__ == "__main__":
    unittest.main()
