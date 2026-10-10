"""The brief reader's safeguards (ported from test_brief_reader): logins masked, invented values dropped."""
import unittest

from agents.read_brief import checked, mask_secrets

BRIEF = ("Prepare a bid for REF-1. Register as Dummy Werke GmbH, USt-IdNr. XX123456789. Use the latest message from "
         "chef@ plus the bidder domain. Authorized subjects/senders: Technikfreigabe/werk, Projektbonus/geld. "
         "Login: user dummy_user, password dummy-secret-123")
TASK = {"access": [{"siteId": "a", "credentials": {"username": "dummy_user", "password": "dummy-secret-123"}}]}


class BriefSafeguards(unittest.TestCase):
    def test_logins_never_reach_the_model(self):
        masked = mask_secrets(BRIEF, TASK)
        self.assertNotIn("dummy-secret-123", masked)
        self.assertNotIn("dummy_user", masked)
        self.assertIn("<password>", masked)

    def test_values_not_in_the_brief_are_dropped(self):
        facts = checked({"bidder_company": "Dummy Werke GmbH", "bidder_vat_id": "XX999999999",
                         "director_sender": "chef@",
                         "authorized_senders": {"technical_release": "werk", "project_bonus": "erfunden"}}, BRIEF)
        self.assertEqual(facts["bidder_company"], "Dummy Werke GmbH")
        self.assertEqual(facts["bidder_vat_id"], "")                      # invented
        self.assertEqual(facts["director_sender"], "chef@")               # kept as written
        self.assertEqual(facts["authorized_senders"], {"technical_release": "werk"})


if __name__ == "__main__":
    unittest.main()
