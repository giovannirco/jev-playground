"""Every Jev call across the arcade: a ring buffer for the live feed and counters for /metrics."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

BUCKETS = (100, 250, 400, 600, 800, 1000, 1500, 2500, 5000, 10000)


class Telemetry:
    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.events: deque[dict] = deque(maxlen=400)
        self.seq = 0
        self.started = time.time()
        self.calls = defaultdict(int)
        self.errors = defaultdict(int)
        self.questions = defaultdict(int)
        self.tokens = defaultdict(int)
        self.cost = defaultdict(float)
        self.latency_sum = defaultdict(float)
        self.hist = defaultdict(lambda: [0] * (len(BUCKETS) + 1))

    def record(self, site: str, ok: bool, ms: float, questions: int, tokens: int = 0, cost: float = 0.0, kinds: dict | None = None) -> None:
        with self.lock:
            self.seq += 1
            if ok:
                self.calls[site] += 1
            else:
                self.errors[site] += 1
            self.questions[site] += questions
            self.tokens[site] += tokens
            self.cost[site] += cost
            self.latency_sum[site] += ms
            row = self.hist[site]
            for index, edge in enumerate(BUCKETS):
                if ms <= edge:
                    row[index] += 1
                    break
            else:
                row[-1] += 1
            self.events.append({
                "seq": self.seq,
                "t": time.time(),
                "site": site,
                "ok": ok,
                "ms": ms,
                "q": questions,
                "tokens": tokens,
                "cost": cost,
                "kinds": kinds or {},
            })
            self.lock.notify_all()

    def since(self, seq: int, timeout: float) -> list[dict]:
        with self.lock:
            if self.seq <= seq:
                self.lock.wait(timeout)
            return [event for event in self.events if event["seq"] > seq]

    def snapshot(self) -> dict:
        with self.lock:
            sites = sorted(set(self.calls) | set(self.errors))
            per_site = {
                site: {
                    "calls": self.calls[site],
                    "errors": self.errors[site],
                    "questions": self.questions[site],
                    "tokens": self.tokens[site],
                    "cost": round(self.cost[site], 6),
                    "avg_ms": round(self.latency_sum[site] / max(1, self.calls[site] + self.errors[site]), 1),
                }
                for site in sites
            }
            return {
                "uptime_s": round(time.time() - self.started),
                "calls": sum(self.calls.values()),
                "questions": sum(self.questions.values()),
                "cost": round(sum(self.cost.values()), 6),
                "sites": per_site,
                "recent": list(self.events)[-40:],
                "seq": self.seq,
            }

    def prometheus(self) -> str:
        lines = [
            "# HELP jev_calls_total Jev Decisions API calls by site and outcome.",
            "# TYPE jev_calls_total counter",
        ]
        with self.lock:
            for site in sorted(set(self.calls) | set(self.errors)):
                lines.append(f'jev_calls_total{{site="{site}",outcome="ok"}} {self.calls[site]}')
                lines.append(f'jev_calls_total{{site="{site}",outcome="error"}} {self.errors[site]}')
            lines += ["# HELP jev_questions_total Questions sent to Jev.", "# TYPE jev_questions_total counter"]
            lines += [f'jev_questions_total{{site="{s}"}} {v}' for s, v in sorted(self.questions.items())]
            lines += ["# HELP jev_input_tokens_total Input tokens billed.", "# TYPE jev_input_tokens_total counter"]
            lines += [f'jev_input_tokens_total{{site="{s}"}} {v}' for s, v in sorted(self.tokens.items())]
            lines += ["# HELP jev_cost_usd_total OpenRouter cost reported per call.", "# TYPE jev_cost_usd_total counter"]
            lines += [f'jev_cost_usd_total{{site="{s}"}} {v:.9f}' for s, v in sorted(self.cost.items())]
            lines += ["# HELP jev_latency_ms Round trip to Jev in milliseconds.", "# TYPE jev_latency_ms histogram"]
            for site, row in sorted(self.hist.items()):
                running = 0
                for edge, count in zip(BUCKETS, row):
                    running += count
                    lines.append(f'jev_latency_ms_bucket{{site="{site}",le="{edge}"}} {running}')
                running += row[-1]
                lines.append(f'jev_latency_ms_bucket{{site="{site}",le="+Inf"}} {running}')
                lines.append(f'jev_latency_ms_sum{{site="{site}"}} {self.latency_sum[site]:.1f}')
                lines.append(f'jev_latency_ms_count{{site="{site}"}} {running}')
        return "\n".join(lines) + "\n"


TELEMETRY = Telemetry()
