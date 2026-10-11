"""Facts a mail agent recorded on the mail site reach the mail analyst, whatever label it gave them (sandbox: the price
index rates were recorded as 'Preisindizes ...' while the reading pane showed no text, and no rate reached the bid).
Invented values."""
import asyncio
import unittest
from types import SimpleNamespace as NS

from swarm.blackboard import Blackboard, Evidence
from swarm.extract import pages


class MailRecords(unittest.TestCase):
    def test_a_record_on_the_mail_site_is_a_mail_page_with_its_facts(self):
        board = Blackboard()

        async def fill():
            await board.set("sites", {"mail": "webmail", "erp": "erp"})
            await board.post(Evidence("Preisindizes REF-1", "webmail",
                                      {"index": "IDX-1", "rate_eur_per_kg": "1,23", "_observed_text": "(frame not readable)"},
                                      source="https://mail.example/?message=7", note="exact reference"))
            await board.post(Evidence("erp material X", "erp", {"status": "01", "_observed_text": "record"}))
        asyncio.run(fill())
        ctx = NS(board=board)

        mail = pages(ctx, "mail", site_records=True)
        self.assertEqual(len(mail), 1)
        self.assertIn('"rate_eur_per_kg": "1,23"', mail[0].data["text"])
        self.assertIn("note: exact reference", mail[0].data["text"])
        self.assertEqual(pages(ctx, "mail"), [])            # default unchanged: kind prefix only
        self.assertEqual(pages(ctx, "erp"), [])             # other roles unchanged


if __name__ == "__main__":
    unittest.main()
