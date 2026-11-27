"""Provider request pacing without network access or real waiting."""

import io
import json
import unittest
from unittest.mock import patch

from fincheck.model import Gemini,ModelError


class ModelTests(unittest.TestCase):
    def test_request_spacing_and_invalid_interval(self):
        content={'candidates':[{'content':{'role':'model','parts':[{'text':'ok'}]}}]}
        with patch.dict('os.environ',{'GEMINI_API_KEY':'test-only','GEMINI_MODEL':'test-model','GEMINI_MIN_INTERVAL_SECONDS':'3'}):
            model=Gemini()
            with patch('fincheck.model.time.monotonic',side_effect=[100,100,101,103]),patch('fincheck.model.time.sleep') as sleep,patch('fincheck.model.urllib.request.urlopen',side_effect=lambda *a,**k:io.BytesIO(json.dumps(content).encode())):
                model._post({});model._post({})
                sleep.assert_called_once_with(2)
        for invalid in ('-1','nan','61','invalid'):
            with patch.dict('os.environ',{'GEMINI_API_KEY':'test-only','GEMINI_MODEL':'test-model','GEMINI_MIN_INTERVAL_SECONDS':invalid}):
                with self.assertRaises(ModelError):Gemini()
