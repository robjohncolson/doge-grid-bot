#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sys
import time
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import state_machine as sm


FIXTURE_DIR = ROOT / "tests" / "fixtures" / "cross_language"


EVENT_BUILDERS = {
    "PriceTick": sm.PriceTick,
    "TimerTick": sm.TimerTick,
    "FillEvent": sm.FillEvent,
    "RecoveryFillEvent": sm.RecoveryFillEvent,
    "RecoveryCancelEvent": sm.RecoveryCancelEvent,
}


@dataclass(frozen=True)
class Scenario:
    name: str
    cfg: sm.EngineConfig
    initial_state: sm.PairState
    events: list[sm.Event]
    order_size_usd: float
    order_sizes: dict[str, float] | None


def _load_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        cfg = sm.EngineConfig(**raw["config"])
        initial_state = sm.from_dict(raw["initial_state"])
        events: list[sm.Event] = []
        for event_raw in raw["events"]:
            event_type = str(event_raw["type"])
            payload = dict(event_raw["payload"])
            builder = EVENT_BUILDERS[event_type]
            events.append(builder(**payload))
        order_size_usd = float(raw["order_size_usd"])
        raw_order_sizes = raw.get("order_sizes")
        order_sizes = None
        if isinstance(raw_order_sizes, dict):
            order_sizes = {str(k): float(v) for k, v in raw_order_sizes.items()}
        scenarios.append(
            Scenario(
                name=str(raw.get("name", path.stem)),
                cfg=cfg,
                initial_state=initial_state,
                events=events,
                order_size_usd=order_size_usd,
                order_sizes=order_sizes,
            )
        )
    return scenarios


def _run_once(
    transition_fn: Callable[
        [sm.PairState, sm.Event, sm.EngineConfig, float, dict[str, float] | None],
        tuple[sm.PairState, list[sm.Action]],
    ],
    scenarios: list[Scenario],
    loops: int,
) -> tuple[float, int]:
    transition_count = 0
    t0 = time.perf_counter()
    for _ in range(loops):
        for scenario in scenarios:
            state = scenario.initial_state
            for event in scenario.events:
                state, _actions = transition_fn(
                    state,
                    event,
                    scenario.cfg,
                    scenario.order_size_usd,
                    scenario.order_sizes,
                )
                transition_count += 1
    elapsed = time.perf_counter() - t0
    return elapsed, transition_count


def _benchmark_backend(
    mode: str,
    scenarios: list[Scenario],
    loops: int,
    warmup_loops: int,
    haskell_exe: str | None,
) -> tuple[float, int]:
    old_backend = os.environ.get("DOGE_CORE_BACKEND")
    old_exe = os.environ.get("DOGE_CORE_EXE")
    old_persistent = os.environ.get("DOGE_CORE_PERSISTENT")
    try:
        if mode == "pure-python":
            transition_fn = sm.transition
        else:
            if mode == "adapter-python":
                os.environ["DOGE_CORE_BACKEND"] = "python"
            elif mode in {"adapter-haskell", "adapter-haskell-persistent", "adapter-haskell-oneshot"}:
                os.environ["DOGE_CORE_BACKEND"] = "haskell"
                if haskell_exe:
                    os.environ["DOGE_CORE_EXE"] = haskell_exe
                elif "DOGE_CORE_EXE" not in os.environ:
                    raise RuntimeError(
                        "adapter-haskell mode requires --haskell-exe or DOGE_CORE_EXE"
                    )
                if mode == "adapter-haskell-oneshot":
                    os.environ["DOGE_CORE_PERSISTENT"] = "0"
                else:
                    os.environ["DOGE_CORE_PERSISTENT"] = "1"
            else:
                raise ValueError(f"Unsupported mode: {mode}")

            import doge_core as dc

            dc = importlib.reload(dc)
            transition_fn = dc.transition

        # Warmup to stabilize first-run effects (imports/process spin-up).
        if warmup_loops > 0:
            _run_once(transition_fn, scenarios, warmup_loops)

        elapsed, transition_count = _run_once(transition_fn, scenarios, loops)
        return elapsed, transition_count
    finally:
        if old_backend is None:
            os.environ.pop("DOGE_CORE_BACKEND", None)
        else:
            os.environ["DOGE_CORE_BACKEND"] = old_backend
        if old_exe is None:
            os.environ.pop("DOGE_CORE_EXE", None)
        else:
            os.environ["DOGE_CORE_EXE"] = old_exe
        if old_persistent is None:
            os.environ.pop("DOGE_CORE_PERSISTENT", None)
        else:
            os.environ["DOGE_CORE_PERSISTENT"] = old_persistent


def _fmt(elapsed: float, count: int) -> tuple[float, float]:
    ms_per = (elapsed / count) * 1000.0
    tps = count / elapsed if elapsed > 0 else 0.0
    return ms_per, tps


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark transition latency across Python and Haskell backends."
    )
    parser.add_argument(
        "--loops",
        type=int,
        default=200,
        help="Measured loops over the full fixture corpus (default: 200)",
    )
    parser.add_argument(
        "--warmup-loops",
        type=int,
        default=10,
        help="Warmup loops over the full fixture corpus before timing (default: 10)",
    )
    parser.add_argument(
        "--haskell-exe",
        type=str,
        default=os.getenv("DOGE_CORE_EXE"),
        help="Path to compiled doge-core-exe binary (or use DOGE_CORE_EXE env var)",
    )
    args = parser.parse_args()

    if args.loops <= 0:
        raise SystemExit("--loops must be > 0")
    if args.warmup_loops < 0:
        raise SystemExit("--warmup-loops must be >= 0")
    if not args.haskell_exe:
        raise SystemExit("--haskell-exe is required for Haskell adapter benchmarks")
    haskell_exe_path = Path(args.haskell_exe).expanduser()
    if not haskell_exe_path.exists():
        raise SystemExit(f"Haskell executable not found: {haskell_exe_path}")

    scenarios = _load_scenarios()
    transition_per_corpus = sum(len(s.events) for s in scenarios)
    print(
        f"Loaded {len(scenarios)} scenarios from {FIXTURE_DIR} "
        f"({transition_per_corpus} transitions per corpus loop)"
    )

    modes = ["pure-python", "adapter-python", "adapter-haskell-oneshot", "adapter-haskell-persistent"]
    results: dict[str, tuple[float, int]] = {}
    for mode in modes:
        elapsed, count = _benchmark_backend(
            mode=mode,
            scenarios=scenarios,
            loops=args.loops,
            warmup_loops=args.warmup_loops,
            haskell_exe=str(haskell_exe_path),
        )
        results[mode] = (elapsed, count)
        ms_per, tps = _fmt(elapsed, count)
        print(
            f"{mode:>15}: {count:6d} transitions in {elapsed:8.3f}s  "
            f"{ms_per:8.3f} ms/transition  {tps:10.1f} tps"
        )

    py_ms, _ = _fmt(*results["pure-python"])
    one_ms, _ = _fmt(*results["adapter-haskell-oneshot"])
    persistent_ms, _ = _fmt(*results["adapter-haskell-persistent"])
    if one_ms > 0:
        ratio = py_ms / one_ms
        print(f"\nSpeed ratio (pure-python / adapter-haskell-oneshot): {ratio:.3f}x")
    if persistent_ms > 0:
        ratio = py_ms / persistent_ms
        print(f"Speed ratio (pure-python / adapter-haskell-persistent): {ratio:.3f}x")


if __name__ == "__main__":
    main()
