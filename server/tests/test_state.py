import unittest

from camera_server.state import RuntimeState


class RuntimeStateTest(unittest.TestCase):
    def test_revision_and_bounded_events(self):
        state = RuntimeState(max_events=2)
        state.update("one", status="starting")
        state.update("two", status="running")
        state.update("three", status="healthy")
        self.assertEqual(3, state.snapshot()["revision"])
        self.assertEqual(["two", "three"], [event["type"] for event in state.events()])


if __name__ == "__main__":
    unittest.main()
