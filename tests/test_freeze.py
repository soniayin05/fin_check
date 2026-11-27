"""Frozen inputs/settings must match before a held-out comparison can start."""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fincheck.freeze import configuration,snapshot,verify
from fincheck.retrieval import PassageStore


class FreezeTests(unittest.TestCase):
    def test_freeze_detects_data_code_and_settings_changes_without_credentials(self):
        with TemporaryDirectory() as d:
            root=Path(d);facts=root/'facts.json';facts.write_text('{}')
            index=root/'data/indexes/passages.json';index.parent.mkdir(parents=True);index.write_text('[]')
            source=root/'fincheck/scoring.py';source.parent.mkdir();source.write_text('score = 1\n')
            args=SimpleNamespace(systems='baseline,agent,multi',max_calls=12,max_turns=10)
            with patch.dict('os.environ',{'GEMINI_API_KEY':'private-test-credential'}):
                config=configuration(args,PassageStore(default_method='keyword'))
                frozen=snapshot(root,facts,config)
            path=root/'freeze.json';path.write_text(json.dumps(frozen))
            self.assertNotIn('private-test-credential',path.read_text())
            self.assertEqual(len(verify(root,facts,config,path)),64)
            facts.write_text('{"changed":true}')
            with self.assertRaises(ValueError):verify(root,facts,config,path)
            facts.write_text('{}')
            with self.assertRaises(ValueError):verify(root,facts,dict(config,max_calls=13),path)
            source.write_text('score = 2\n')
            with self.assertRaises(ValueError):verify(root,facts,config,path)

    def test_heldout_dry_run_requires_freeze_before_model_access(self):
        from fincheck.__main__ import main
        facts=Path(__file__).resolve().parent.parent/'data/sample/financial_facts.json'
        with TemporaryDirectory() as d:
            root=Path(d);cases=root/'claims.jsonl'
            cases.write_text(json.dumps(dict(id='test-only',claim='Example claim',expected={'verdict':'insufficient_evidence'})))
            argv=['fincheck','compare-systems','--dry-run','--split','heldout','--cases',str(cases),'--facts',str(facts)]
            error=io.StringIO()
            with patch('fincheck.__main__.ROOT',root),patch('sys.argv',argv),patch('sys.stderr',error),patch('fincheck.model.Gemini',side_effect=AssertionError('No model access')):
                with self.assertRaises(SystemExit) as stopped:main()
            self.assertEqual(stopped.exception.code,2)
            self.assertIn('need --freeze',error.getvalue())
