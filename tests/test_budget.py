from forge.config import Budget
from forge.budget import BudgetTracker


def test_iteration_cap():
    bt = BudgetTracker(Budget(max_iterations=2, max_wall_secs=9999), clock=lambda: 0.0)
    bt.start()
    assert bt.stop_reason() is None
    bt.tick(); assert bt.stop_reason() is None
    bt.tick(); assert bt.stop_reason() == "iterations"


def test_wall_clock_cap():
    t = {"now": 0.0}
    bt = BudgetTracker(Budget(max_iterations=99, max_wall_secs=30), clock=lambda: t["now"])
    bt.start()
    t["now"] = 29.0
    assert bt.stop_reason() is None
    t["now"] = 30.0
    assert bt.stop_reason() == "wall_clock"
