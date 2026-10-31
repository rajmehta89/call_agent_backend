"""Compare semantic preparation batch sizes with a warm local model and fresh query vectors.

No CRM access or provider API calls. The model must be available locally. Results are CPU measurements,
not phone-call latency. Alternate run order and verify that vectors remain numerically equivalent.
"""

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(MONGO_URI="", LOCAL_EMBEDDINGS="true", MEANING_TRANSLATION="false")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("logs/semantic-preparation-benchmark.json"))
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("Choose between one and ten repeats")

    import numpy as np
    import brain_service as brain
    import meaning
    import voice_turns
    from shopify_service import shopify_service

    if shopify_service.configured:
        sys.exit("This local diagnostic requires a disconnected store")
    if brain._local_embedder() is None:
        sys.exit("The local embedding model is unavailable")

    def no_provider(*args, **kwargs):
        raise RuntimeError("The diagnostic cannot fall back to a network embedding provider")
    brain.brain_service._embeddings = no_provider

    caller = "I just bought a new house and want to make it good. Could you give me some general guidance on where to start?"
    agent = "We sell bathroom and kitchen products online, including tapware, showers, basins, tiles and appliances. What are you looking to get for your new place?"
    earlier = "What does your business do? Could you tell me that again?"
    voice_turns.prepare_turn_meaning(caller, agent, earlier)  # import/model warmup excluded
    rows, references = [], {}
    for repeat in range(args.repeats):
        for size in ((8, 1) if repeat % 2 == 0 else (1, 8)):
            os.environ["VOICE_SEMANTIC_BATCH_SIZE"] = str(size)
            with meaning._CACHE_LOCK:
                meaning._CACHE.clear()
            started = perf_counter()
            voice_turns.prepare_turn_meaning(caller, agent, earlier)
            rows.append({"repeat": repeat + 1, "batch_size": size,
                         "preparation_ms": round((perf_counter() - started) * 1000, 2)})
            references[size] = {key: value.copy() for key, value in meaning._CACHE.items()}
    if references[8].keys() != references[1].keys():
        raise RuntimeError("Batch sizes produced different semantic inputs")
    maximum = 0.0
    for key, vector in references[8].items():
        np.testing.assert_allclose(vector, references[1][key], atol=2e-5, rtol=2e-5)
        maximum = max(maximum, float(np.max(np.abs(vector - references[1][key]))))
    report = {"scope": "Local semantic preparation only; no full-call saving is implied", "results": rows,
              "vector_max_absolute_difference": maximum,
              "medians_ms": {str(size): statistics.median(row["preparation_ms"] for row in rows if row["batch_size"] == size)
                             for size in (8, 1)}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
