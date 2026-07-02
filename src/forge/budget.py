from forge.config import Budget


class BudgetTracker:
    def __init__(self, budget: Budget, clock):
        self.budget = budget
        self.clock = clock
        self.iterations = 0
        self._start = 0.0

    def start(self) -> None:
        self._start = self.clock()

    def tick(self) -> None:
        self.iterations += 1

    def stop_reason(self):
        if self.iterations >= self.budget.max_iterations:
            return "iterations"
        if self.clock() - self._start >= self.budget.max_wall_secs:
            return "wall_clock"
        return None
