"""swarm.decide against a fake Clef endpoint (no network)."""
import asyncio
import json
import os
import unittest

import httpx

from swarm import decide


def fake(handler):
    decide._transport = httpx.MockTransport(handler)


class Clef(unittest.TestCase):
    def setUp(self):
        os.environ["CLEF_BASE_URL"] = "http://clef.invalid/clef"
        os.environ["CLEF_API_KEY"] = "test-key"
        self.seen = []

    def tearDown(self):
        decide._transport = None

    def test_choice_sends_criteria_and_images(self):
        def handler(request):
            self.seen.append((request.url.path, request.headers["authorization"], json.loads(request.content)))
            return httpx.Response(200, json={"answers": {"q": {"type": "choice", "choice": "login",
                                                               "confidence": 0.9}},
                                             "usage": {"input_tokens": 10}})
        fake(handler)
        kind, p = asyncio.run(decide.choose({"page": "x"}, "What?", {"login": "a", "other": "b"}, [b"\x89PNG"]))
        self.assertEqual((kind, p), ("login", 0.9))
        path, auth, body = self.seen[0]
        self.assertEqual((path, auth), ("/clef/v1/decide", "Bearer test-key"))
        self.assertEqual(body["questions"]["q"]["criteria"], {"login": "a", "other": "b"})
        self.assertTrue(body["images"][0].startswith("data:image/png;base64,"))

    def test_yes_and_score(self):
        def handler(request):
            q = json.loads(request.content)["questions"]["q"]
            if q["type"] == "noul":
                return httpx.Response(200, json={"answers": {"q": {"noul": 0.25}}})
            self.assertEqual(q["criteria"], ["low", "high"])
            return httpx.Response(200, json={"answers": {"q": {"score": 1.0}}})
        fake(handler)
        self.assertEqual(asyncio.run(decide.yes("s", "?")), 0.25)
        self.assertEqual(asyncio.run(decide.score("s", "?", ["low", "high"])), 1.0)

    def test_bad_question_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(400, json={"error": {"code": "invalid_question"}})
        fake(handler)
        with self.assertRaises(decide.ClefError):
            asyncio.run(decide.yes("s", "?"))
        self.assertEqual(len(calls), 1)

    def test_outage_is_retried_then_raises(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503)
        fake(handler)
        with self.assertRaises(decide.ClefError):
            asyncio.run(decide.decide("s", {"q": {"type": "noul", "instructions": "?"}}, retries=1))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
