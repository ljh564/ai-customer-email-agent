import unittest
from unittest.mock import patch

from app.models import EmailRecord
from app.workflow import process_email


def make_email(subject: str, body: str, *, sender_email: str = "customer@example.com") -> EmailRecord:
    return EmailRecord(
        customer_name="Customer",
        customer_email=sender_email,
        subject=subject,
        body=body,
    )


def capture_progress(target: list[tuple[str, str, int, str]]):
    def callback(email: EmailRecord) -> None:
        target.append(
            (
                email.processing_status,
                email.processing_stage,
                email.processing_progress,
                email.processing_message,
            )
        )

    return callback


class ProcessingProgressTests(unittest.TestCase):
    @patch("app.workflow.retrieve_knowledge", return_value=[])
    def test_support_email_reports_real_workflow_stages(self, _retrieve) -> None:
        email = make_email(
            "Duplicate subscription charge",
            "I was charged twice. Please refund the duplicate payment.",
        )
        snapshots: list[tuple[str, str, int, str]] = []

        result = process_email(
            email,
            use_llm=False,
            progress_callback=capture_progress(snapshots),
        )

        stages = [snapshot[1] for snapshot in snapshots]
        self.assertEqual(snapshots[0][0:3], ("running", "preprocess", 8))
        self.assertIn("relevance_gate", stages)
        self.assertIn("semantic_analysis", stages)
        self.assertIn("retrieve", stages)
        self.assertIn("draft", stages)
        self.assertIn("review", stages)
        self.assertEqual(snapshots[-1][0:3], ("completed", "completed", 100))
        self.assertIsNotNone(result.processing_started_at)
        self.assertIsNotNone(result.processing_finished_at)

    def test_non_support_email_finishes_without_rag_or_draft_stages(self) -> None:
        email = make_email(
            "Your verification code",
            "Your security verification code is 123456. Do not reply to this message.",
            sender_email="noreply@github.com",
        )
        snapshots: list[tuple[str, str, int, str]] = []

        result = process_email(
            email,
            use_llm=False,
            progress_callback=capture_progress(snapshots),
        )

        stages = [snapshot[1] for snapshot in snapshots]
        self.assertEqual(result.status, "irrelevant")
        self.assertNotIn("retrieve", stages)
        self.assertNotIn("draft", stages)
        self.assertEqual(snapshots[-1][0:3], ("completed", "completed", 100))

    @patch("app.workflow.retrieve_knowledge", side_effect=RuntimeError("retrieval unavailable"))
    def test_failure_is_reported_instead_of_remaining_running(self, _retrieve) -> None:
        email = make_email(
            "Cannot access my workspace",
            "Please help. I cannot log in to my workspace.",
        )
        snapshots: list[tuple[str, str, int, str]] = []

        with self.assertRaises(RuntimeError):
            process_email(
                email,
                use_llm=False,
                progress_callback=capture_progress(snapshots),
            )

        self.assertEqual(snapshots[-1][0], "failed")
        self.assertEqual(snapshots[-1][1], "retrieve")
        self.assertGreaterEqual(snapshots[-1][2], 50)
        self.assertIn("RuntimeError", snapshots[-1][3])
        self.assertIsNotNone(email.processing_finished_at)


if __name__ == "__main__":
    unittest.main()
