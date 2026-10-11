"""A model reply naming two actions in one object is split into two actions (sandbox: {"record", "done"} in one object
failed validation on every step it appeared)."""
import json
import unittest

from swarm.browser_agent import normalize_actions


class NormalizeActions(unittest.TestCase):
    def test_two_actions_in_one_object_are_split_with_done_last(self):
        text = json.dumps({"memory": "m", "action": [{"done": {"text": "x", "success": True}, "record": {"kind": "k", "data": {}}}]})
        out = json.loads(normalize_actions(text))
        self.assertEqual([list(a) for a in out["action"]], [["record"], ["done"]])

    def test_a_single_object_becomes_a_list_and_good_replies_are_untouched(self):
        self.assertEqual(json.loads(normalize_actions('{"action": {"click": {"index": 1}}}'))["action"], [{"click": {"index": 1}}])
        good = '{"memory": "m", "action": [{"click": {"index": 1}}]}'
        self.assertEqual(normalize_actions(good), good)
        self.assertEqual(normalize_actions("not json"), "not json")


if __name__ == "__main__":
    unittest.main()
