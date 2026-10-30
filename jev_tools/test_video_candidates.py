import json
import math
import unittest

import video_candidates


def candidate(candidate_id, start, end, transcript):
    return {
        "id": candidate_id,
        "start_seconds": start,
        "end_seconds": end,
        "transcript": transcript,
    }


def answer_for(questions, scores_by_index, missing=()):
    answers = {}
    for question_id in questions:
        prefix, key = question_id.split("__", 1)
        index = int(prefix.removeprefix("candidate_"))
        if (index, key) in missing or index not in scores_by_index:
            continue
        answers[question_id] = {
            "type": "noul",
            "noul": scores_by_index[index][key],
        }
    return {"answers": answers}


class ScriptedAsk:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.unexpected_calls = 0

    def __call__(self, state, questions):
        self.calls.append((state, questions))
        try:
            response = next(self.responses)
        except StopIteration:
            self.unexpected_calls += 1
            raise RuntimeError("unexpected extra call") from None
        if callable(response):
            response = response(questions)
        if isinstance(response, Exception):
            raise response
        return response


class RankCandidatesTests(unittest.TestCase):
    def test_remote_questions_use_generated_labels_and_not_caller_ids(self):
        ask = ScriptedAsk([lambda q: answer_for(q, {0: {
            "relevance": 0.9, "standalone_completeness": 0.8, "opening_attractiveness": 0.7,
        }})])
        item = candidate("private-project-name", 0, 2, "A short transcript.")
        result = video_candidates.rank_candidates([item], "brief", ask)
        state, questions = ask.calls[0]
        self.assertEqual(result["candidates"][0]["id"], "private-project-name")
        self.assertNotIn("private-project-name", state)
        self.assertTrue(all("candidate_0" in value["instructions"] for value in questions.values()))

    def test_wholly_invalid_batch_stops_later_requests(self):
        ask = ScriptedAsk([{"answers": {}}])
        items = [candidate("one", 0, 1, "a" * 13000), candidate("two", 1, 2, "b" * 13000)]
        result = video_candidates.rank_candidates(items, "brief", ask)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(ask.calls), 1)
        self.assertTrue(all(x["verification"] == "unscored" for x in result["candidates"]))

    def test_sorts_scored_candidates_stably_and_preserves_raw_fields(self):
        first = candidate(
            "first", 1, 4.5, "  First line\nsecond line  "
        )
        second = candidate("second", 8, 12, "A stronger independent opening.")
        third = candidate("third", 20.25, 24, "An equally strong opening.")
        scores = {
            0: {
                "relevance": 0.4,
                "standalone_completeness": 0.5,
                "opening_attractiveness": 0.6,
            },
            1: {
                "relevance": 0.9,
                "standalone_completeness": 0.9,
                "opening_attractiveness": 0.9,
            },
            2: {
                "relevance": 0.9,
                "standalone_completeness": 0.9,
                "opening_attractiveness": 0.9,
            },
        }
        ask = ScriptedAsk(
            [lambda questions: answer_for(questions, scores)]
        )

        result = video_candidates.rank_candidates(
            [first, second, third], "Choose useful spoken highlights.", ask
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [item["id"] for item in result["candidates"]],
            ["second", "third", "first"],
        )
        self.assertEqual(result["candidates"][2]["start_seconds"], 1)
        self.assertEqual(result["candidates"][2]["end_seconds"], 4.5)
        self.assertEqual(
            result["candidates"][2]["transcript"], "  First line\nsecond line  "
        )
        self.assertAlmostEqual(result["candidates"][0]["score"], 0.9)
        self.assertTrue(math.isfinite(result["candidates"][0]["score"]))
        self.assertTrue(
            all(
                item["verification"] == "scored"
                for item in result["candidates"]
            )
        )
        self.assertNotIn("reason", result["candidates"][0])
        self.assertNotIn("visual", result["candidates"][0])
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(ask.unexpected_calls, 0)

        state, questions = ask.calls[0]
        self.assertIn("  First line\nsecond line  ", state)
        self.assertEqual(len(questions), 9)
        self.assertTrue(
            all(
                any(
                    question_id.endswith(f"__{key}")
                    for key in video_candidates.SCORE_KEYS
                )
                for question_id in questions
            )
        )
        self.assertTrue(all(item["type"] == "noul" for item in questions.values()))
        self.assertLessEqual(
            len(
                json.dumps(
                    {"state": state, "questions": questions},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
            video_candidates.MAX_REQUEST_BYTES,
        )

    def test_rejects_bad_inputs_before_calling_jev(self):
        ask = ScriptedAsk([])
        cases = [
            ([], "brief", ask),
            (
                [
                    candidate(f"c{index}", index, index + 1, "text")
                    for index in range(video_candidates.MAX_CANDIDATES + 1)
                ],
                "brief",
                ask,
            ),
            (
                [
                    candidate("same", 0, 1, "one"),
                    candidate("same", 1, 2, "two"),
                ],
                "brief",
                ask,
            ),
            ([candidate("bad", 2, 2, "text")], "brief", ask),
            ([candidate("bad", -1, 2, "text")], "brief", ask),
            ([candidate("bad", 0, float("inf"), "text")], "brief", ask),
            ([candidate("bad", False, 2, "text")], "brief", ask),
            ([candidate("bad", 0, 2, "   ")], "brief", ask),
            ([candidate("ok", 0, 2, "text")], "   ", ask),
            ([candidate("ok", 0, 2, "text")], "brief", None),
        ]

        for candidates, brief, callback in cases:
            with self.subTest(candidates=candidates, brief=brief, callback=callback):
                with self.assertRaises(ValueError):
                    video_candidates.rank_candidates(candidates, brief, callback)
        self.assertEqual(ask.calls, [])

    def test_batches_candidates_within_budget_and_preserves_batch_fields(self):
        candidates = [
            candidate(
                f"candidate-{index}",
                index + 0.25,
                index + 2.75,
                f"{index}:" + ("x" * 10_000),
            )
            for index in range(5)
        ]
        scores = {
            index: {
                "relevance": 0.5,
                "standalone_completeness": 0.6,
                "opening_attractiveness": 0.7,
            }
            for index in range(5)
        }
        responder = lambda questions: answer_for(questions, scores)
        ask = ScriptedAsk([responder] * len(candidates))

        result = video_candidates.rank_candidates(candidates, "brief", ask)

        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(len(ask.calls), 2)
        self.assertLess(len(ask.calls), len(candidates))
        self.assertEqual(ask.unexpected_calls, 0)
        for state, questions in ask.calls:
            self.assertLessEqual(
                len(
                    json.dumps(
                        {"state": state, "questions": questions},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
                video_candidates.MAX_REQUEST_BYTES,
            )
        by_id = {item["id"]: item for item in result["candidates"]}
        self.assertEqual(
            by_id["candidate-3"]["start_seconds"], 3.25
        )
        self.assertEqual(
            by_id["candidate-3"]["end_seconds"], 5.75
        )
        self.assertEqual(
            by_id["candidate-3"]["transcript"], "3:" + ("x" * 10_000)
        )

    def test_partial_batch_scores_keep_good_results_and_mark_bad_scores(self):
        candidates = [
            candidate("good", 0, 2, "complete relevant text"),
            candidate("range", 2, 4, "out of range"),
            candidate("missing", 4, 6, "missing one score"),
            candidate("other", 6, 8, "another good result"),
        ]
        scores = {
            0: {
                "relevance": 0.7,
                "standalone_completeness": 0.8,
                "opening_attractiveness": 0.9,
            },
            1: {
                "relevance": 1.1,
                "standalone_completeness": 0.8,
                "opening_attractiveness": 0.8,
            },
            2: {
                "relevance": 0.8,
                "standalone_completeness": 0.8,
                "opening_attractiveness": 0.8,
            },
            3: {
                "relevance": 0.5,
                "standalone_completeness": 0.5,
                "opening_attractiveness": 0.5,
            },
        }
        ask = ScriptedAsk(
            [
                lambda questions: answer_for(
                    questions,
                    scores,
                    missing={(2, "opening_attractiveness")},
                )
            ]
        )

        result = video_candidates.rank_candidates(candidates, "brief", ask)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            [item["id"] for item in result["candidates"]],
            ["good", "other", "range", "missing"],
        )
        self.assertEqual(result["candidates"][0]["verification"], "scored")
        self.assertEqual(result["candidates"][1]["verification"], "scored")
        for item in result["candidates"][2:]:
            self.assertEqual(item["verification"], "unscored")
            self.assertNotIn("score", item)
            self.assertNotIn("scores", item)
            self.assertTrue(item["error"])
        self.assertIn(
            "relevance", result["candidates"][2]["error"]
        )
        self.assertIn(
            "opening_attractiveness", result["candidates"][3]["error"]
        )
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(ask.unexpected_calls, 0)

    def test_batch_exception_stops_later_requests_and_redacts_message(self):
        candidates = [
            candidate(
                f"candidate-{index}",
                index + 0.5,
                index + 3.5,
                f"{index}:" + ("z" * 10_000),
            )
            for index in range(5)
        ]
        scores = {
            index: {
                "relevance": 0.6,
                "standalone_completeness": 0.7,
                "opening_attractiveness": 0.8,
            }
            for index in range(5)
        }
        responder = lambda questions: answer_for(questions, scores)
        secret = "SECRET_PROVIDER_TOKEN_123"
        ask = ScriptedAsk(
            [
                responder,
                RuntimeError(secret),
                responder,
                responder,
                responder,
            ]
        )

        result = video_candidates.rank_candidates(candidates, "brief", ask)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(ask.calls), 2)
        self.assertEqual(ask.unexpected_calls, 0)
        self.assertTrue(any(item["verification"] == "scored" for item in result["candidates"]))
        self.assertTrue(any(item["verification"] == "unscored" for item in result["candidates"]))
        self.assertNotIn(secret, json.dumps(result))
        self.assertTrue(
            any(
                item.get("error") == "Jev request failed (RuntimeError)."
                for item in result["candidates"]
            )
        )
        self.assertTrue(
            any(
                item.get("error")
                == "Not evaluated after an earlier ask failure."
                for item in result["candidates"]
            )
        )

    def test_total_failure_is_explicit_and_preserves_input_order(self):
        candidates = [
            candidate("b", 5, 8.5, " second text "),
            candidate("a", 0, 2, "first text"),
        ]
        secret = "SECRET_TIMEOUT_DETAIL"
        ask = ScriptedAsk(
            [TimeoutError(secret), ValueError("another secret")]
        )

        result = video_candidates.rank_candidates(candidates, "brief", ask)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"], "Jev unavailable: no candidate could be scored.")
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(ask.unexpected_calls, 0)
        self.assertEqual(
            [item["id"] for item in result["candidates"]], ["b", "a"]
        )
        self.assertEqual(
            [
                (
                    item["start_seconds"],
                    item["end_seconds"],
                    item["transcript"],
                )
                for item in result["candidates"]
            ],
            [(5, 8.5, " second text "), (0, 2, "first text")],
        )
        self.assertTrue(
            all(
                item["verification"] == "unscored"
                and "score" not in item
                and "scores" not in item
                for item in result["candidates"]
            )
        )
        self.assertNotIn(secret, json.dumps(result))
        self.assertTrue(
            all(
                item["error"] == "Jev request failed (TimeoutError)."
                for item in result["candidates"]
            )
        )

    def test_rejects_payloads_over_request_and_total_budgets(self):
        ask = ScriptedAsk([])
        oversized = "x" * (video_candidates.MAX_REQUEST_BYTES + 1)
        with self.assertRaises(ValueError):
            video_candidates.rank_candidates(
                [candidate("large", 0, 1, oversized)], "brief", ask
            )

        total_candidates = [
            candidate(f"c{index}", index, index + 1, "y" * 20_000)
            for index in range(5)
        ]
        with self.assertRaises(ValueError):
            video_candidates.rank_candidates(total_candidates, "brief", ask)
        self.assertEqual(ask.calls, [])


if __name__ == "__main__":
    unittest.main()
