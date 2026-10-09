"""Claude API client tests. urlopen is faked; nothing is sent."""
import io
import json
import unittest
import urllib.error
from unittest import mock

import fakes  # noqa: F401  (puts src/ on sys.path)

import claude_api
import config

API_KEY = "sk-ant-test-key"
SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def api_message(text='{"events": [], "clarify": null}', stop_reason="end_turn", content=None):
    return {
        "model": "claude-haiku-5-5",
        "stop_reason": stop_reason,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def http_error(code, error_type="overloaded_error", message="Overloaded"):
    body = json.dumps({"type": "error", "error": {"type": error_type, "message": message}}).encode()
    return urllib.error.HTTPError(claude_api.API_URL, code, "error", {}, io.BytesIO(body))


class ClaudeApiTest(unittest.TestCase):
    def setUp(self):
        self.responses = []   # each entry: a message dict to return, or an exception to raise
        self.requests = []
        patches = [
            mock.patch.object(config, "secret", return_value=API_KEY),
            mock.patch.object(claude_api.urllib.request, "urlopen", side_effect=self.urlopen),
            mock.patch.object(claude_api.time, "sleep"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def urlopen(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())

    def call(self):
        return claude_api.complete_json("system text", "user text", SCHEMA)

    def test_request_follows_the_plan(self):
        self.responses = [api_message()]
        self.call()
        request, timeout = self.requests[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-api-key"), API_KEY)
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertLessEqual(timeout, claude_api.TIMEOUT_SECONDS)
        self.assertEqual(json.loads(request.data), {
            "model": "claude-haiku-5-5",
            "max_tokens": claude_api.MAX_TOKENS,
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            "system": "system text",
            "messages": [{"role": "user", "content": "user text"}],
        })

    def test_returns_parsed_json_and_the_raw_message(self):
        self.responses = [api_message('{"events": [], "clarify": "Which one?"}')]
        data, message = self.call()
        self.assertEqual(data, {"events": [], "clarify": "Which one?"})
        self.assertEqual(message["usage"]["input_tokens"], 10)

    def test_reads_text_blocks_by_type(self):
        content = [{"type": "thinking", "thinking": "", "signature": "x"}, {"type": "text", "text": '{"a": 1}'}]
        self.responses = [api_message(content=content)]
        self.assertEqual(self.call()[0], {"a": 1})

    def test_refusal_and_truncation_are_failures(self):
        for stop_reason in ("refusal", "max_tokens"):
            with self.subTest(stop_reason=stop_reason):
                self.responses = [api_message(stop_reason=stop_reason)]
                with self.assertRaisesRegex(claude_api.ClaudeError, stop_reason):
                    self.call()

    def test_output_that_is_not_json_is_a_failure(self):
        self.responses = [api_message("not json")]
        with self.assertRaises(claude_api.ClaudeError):
            self.call()

    def test_retries_once_when_overloaded_or_rate_limited(self):
        for code in claude_api.RETRY_STATUSES:
            with self.subTest(code=code):
                self.requests.clear()
                self.responses = [http_error(code), api_message()]
                self.call()
                self.assertEqual(len(self.requests), 2)

    def test_gives_up_after_the_second_failure(self):
        self.responses = [http_error(529), http_error(529)]
        with self.assertRaisesRegex(claude_api.ClaudeError, "HTTP 529 overloaded_error: Overloaded"):
            self.call()
        self.assertEqual(len(self.requests), 2)

    def test_bad_request_is_not_retried(self):
        self.responses = [http_error(400, "invalid_request_error", "bad schema")]
        with self.assertRaisesRegex(claude_api.ClaudeError, "HTTP 400 invalid_request_error: bad schema"):
            self.call()
        self.assertEqual(len(self.requests), 1)

    def test_network_errors_never_leak_the_api_key(self):
        for failure in (urllib.error.URLError(f"dns failure {API_KEY}"), TimeoutError()):
            with self.subTest(failure=type(failure).__name__):
                self.responses = [failure]
                with self.assertRaises(claude_api.ClaudeError) as caught:
                    self.call()
                self.assertNotIn(API_KEY, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
