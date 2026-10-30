import unittest

from route_advice import advise_route


def answer(complexity, bounded, blocked):
    return {"answers": {name: {"noul": value} for name, value in (
        ("complex", complexity), ("bounded", bounded), ("external_block", blocked))}}


class RouteAdviceTests(unittest.TestCase):
    def test_external_block_takes_priority_over_expensive_escalation(self):
        result = advise_route("investigate", ["the network is offline"], "subagent", lambda *_: answer(0.9, 0.9, 0.9))
        self.assertEqual(result["recommendation"], "check_external_block")

    def test_complex_and_bounded_are_only_advisory(self):
        complex_result = advise_route("task", [], "main", lambda *_: answer(0.65, 0.2, 0.1))
        simple_result = advise_route("task", [], "subagent", lambda *_: answer(0.1, 0.9, 0.1))
        self.assertEqual(complex_result["recommendation"], "consider_critical")
        self.assertEqual(simple_result["recommendation"], "consider_bounded")
        self.assertIn("不会切换", simple_result["note"])

    def test_malformed_and_failure_do_not_produce_a_model_choice(self):
        result = advise_route("task", [], "subagent", lambda *_: {"answers": {}})
        self.assertEqual((result["status"], result["recommendation"]), ("partial", "use_existing_rules"))
        def failure(*_):
            raise TimeoutError("private detail")
        result = advise_route("task", [], "main", failure)
        self.assertEqual(result["recommendation"], "use_existing_rules")
        self.assertNotIn("private detail", str(result))

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(ValueError):
            advise_route("", [], "main", lambda *_: {})
        with self.assertRaises(ValueError):
            advise_route("task", [], "unknown", lambda *_: {})


if __name__ == "__main__":
    unittest.main()
