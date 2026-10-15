C01 fixtures are original synthetic data dedicated to the public domain under
CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/).

The NVDA symbol selects the same stock graph as a real workload, but the company
identity, source answers, prices and model answer are invented. No market feed,
social content, vendor response or model-generated text was downloaded.

`workload.json` pins the workload, graph settings, generated price-series recipe,
scripted output, controlled delays and failure scenarios. Delays are intentional
fixture behavior. Their timings are never evidence of live provider performance.

Cold/warm describes the fixture vendor response cache: each cold run starts
empty; its paired warm run receives the same cache contents. Both runs start a
fresh graph, model, memory log and application cache directory. It does not mean
a fresh Python process, a warm GPU, or the production vendor cache.
