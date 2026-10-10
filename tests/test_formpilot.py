"""formpilot helpers that need no browser or model."""
import unittest

from swarm.formpilot import requests_tan


class TanButton(unittest.TestCase):
    def test_tan_request_buttons(self):
        self.assertTrue(requests_tan({"tag": "button", "type": "submit", "text": "TAN anfordern"}))
        self.assertTrue(requests_tan({"tag": "a", "text": "Neue TAN senden"}))
        self.assertTrue(requests_tan({"tag": "input", "type": "submit", "value": "TAN anfordern"}))

    def test_not_a_tan_request(self):
        self.assertFalse(requests_tan({"tag": "input", "type": "text", "label": "Signatur-TAN"}))   # the field
        self.assertFalse(requests_tan({"tag": "button", "text": "Weiter"}))
        self.assertFalse(requests_tan({"tag": "button", "text": "Tankstelle suchen"}))              # not the word
        self.assertFalse(requests_tan({"tag": "select", "label": "TAN-Verfahren"}))
        self.assertFalse(requests_tan(None))


if __name__ == "__main__":
    unittest.main()
