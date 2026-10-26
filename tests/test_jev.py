import unittest

from config import parse_application_status_mode
from jev import (
    JevAssessment,
    JevError,
    assessment_to_status,
    build_channel_state,
    build_questions,
    parse_answer,
)
from main import format_application_status_line


def make_answer(
    choice: str = "申請中",
    confidence: float | None = 0.93,
    noul: float = 0.15,
) -> dict:
    answers: dict = {
        "application_state": {
            "type": "choice",
            "choice": choice,
            "probabilities": {choice: 0.9},
        },
        "no_application_yet": {"type": "noul", "noul": noul},
    }
    if confidence is not None:
        answers["application_state"]["confidence"] = confidence
    return {"data": {"result": {"answers": answers}}}


class ParseAnswerTests(unittest.TestCase):
    def test_extracts_choice_confidence_and_noul(self) -> None:
        assessment = parse_answer(make_answer())
        self.assertEqual(assessment.application_state, "申請中")
        self.assertAlmostEqual(assessment.confidence, 0.93)
        self.assertAlmostEqual(assessment.no_application_probability, 0.15)

    def test_rejects_unknown_choice_state(self) -> None:
        with self.assertRaises(JevError):
            parse_answer(make_answer(choice="その他"))

    def test_rejects_judgment_impossible_state(self) -> None:
        with self.assertRaises(JevError):
            parse_answer(make_answer(choice="判断不能"))

    def test_rejects_missing_noul(self) -> None:
        data = make_answer()
        data["data"]["result"]["answers"]["no_application_yet"] = {"type": "noul"}
        with self.assertRaises(JevError):
            parse_answer(data)


class AssessmentStatusTests(unittest.TestCase):
    def test_low_no_application_probability_means_applied(self) -> None:
        has_application, detail = assessment_to_status(
            JevAssessment("申請中", 0.9, 0.1)
        )
        self.assertIs(has_application, True)
        self.assertEqual(detail, "申請中")

    def test_high_no_application_probability_means_not_applied(self) -> None:
        has_application, detail = assessment_to_status(
            JevAssessment("未申請", 0.8, 0.95)
        )
        self.assertIs(has_application, False)
        self.assertEqual(detail, "未申請")


class ChannelStateTests(unittest.TestCase):
    def test_sends_attachment_metadata_only(self) -> None:
        state = build_channel_state(
            [
                {
                    "author": "user",
                    "content": "申請します",
                    "attachments": [{"content_type": "image/png", "url": "x"}],
                }
            ]
        )
        self.assertEqual(
            state,
            [
                {
                    "author": "user",
                    "content": "申請します",
                    "attachments": [{"content_type": "image/png"}],
                }
            ],
        )

    def test_limits_message_count_and_content_length(self) -> None:
        messages = [
            {"author": f"u{i}", "content": "あ" * 600, "attachments": []}
            for i in range(60)
        ]
        state = build_channel_state(messages, max_messages=50)
        self.assertEqual(len(state), 50)
        for item in state:
            self.assertLessEqual(len(item["content"]), 500)

    def test_questions_use_choice_and_noul(self) -> None:
        questions = build_questions()
        self.assertEqual(questions["application_state"]["type"], "choice")
        self.assertEqual(questions["no_application_yet"]["type"], "noul")


class ApplicationModeConfigTests(unittest.TestCase):
    def test_parses_both_modes(self) -> None:
        self.assertEqual(parse_application_status_mode("JEV"), "jev")
        self.assertEqual(parse_application_status_mode("simple"), "simple")

    def test_rejects_unknown_mode(self) -> None:
        with self.assertRaises(ValueError):
            parse_application_status_mode("gpt")


class FormatApplicationStatusLineTests(unittest.TestCase):
    def test_unknown_status_is_reported(self) -> None:
        self.assertIn("未判定", format_application_status_line(None, 0.7))

    def test_simple_status_contains_the_boolean(self) -> None:
        line = format_application_status_line(
            {"has_application": True, "method": "simple"}, 0.7
        )
        self.assertIn("true", line)
        self.assertIn("simple", line)

    def test_jev_status_contains_state_and_confidence(self) -> None:
        line = format_application_status_line(
            {"has_application": True, "method": "jev", "detail": "申請中",
             "confidence": 0.93},
            0.7,
        )
        self.assertIn("true", line)
        self.assertIn("申請中", line)
        self.assertIn("0.93", line)
        self.assertNotIn("要人確認", line)

    def test_low_confidence_asks_for_human_review(self) -> None:
        line = format_application_status_line(
            {"has_application": False, "method": "jev", "detail": "未申請",
             "confidence": 0.45},
            0.7,
        )
        self.assertIn("要人確認", line)

    def test_fallback_status_explains_jev_failure(self) -> None:
        line = format_application_status_line(
            {"has_application": False, "method": "jev_fallback"}, 0.7
        )
        self.assertIn("false", line)
        self.assertIn("簡易判定", line)


if __name__ == "__main__":
    unittest.main()
