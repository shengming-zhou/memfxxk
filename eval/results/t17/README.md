# T17 evaluation results

| Run | Contents |
| --- | --- |
| [P11 fallback](p11-smoke/metrics.json) | First six source-resolution checks during gateway downtime |
| [P11 live](p11-smoke-live/metrics.json) | Six DeepSeek Flash source-resolution checks after gateway recovery |
| [P12 deterministic](p12-smoke/metrics.json) | Six adaptive allocation requests without model planning |
| [P12 live](p12-smoke-live/metrics.json) | Six requests with live plan coefficients and measured usage |
| [P14 live](p14-smoke-live/metrics.json) | Three contradictory drafts and three clean drafts on the production check path |
| [P15 evidence](p15-evidence/manifest.json) | Six-arm evidence check using BGE vectors and fixed simulated planning coefficients |
| [P15 initial live](p15-live/manifest.json) | Original six-arm GPT and DeepSeek Flash run |
| [P15 reviewed cases](p15-rescored/cases.json) | Reviewed citations, evidence receipts, usage, and decisions from the initial run |
| [P15 neutral pilot](p15-neutral-pilot/metrics.json) | Six additional GPT requests using a neutral conflict prompt |

The paid pilot covers two contradictory drafts and one clean draft under fixed and adaptive allocation. Keep these scores separate from the 96 original rows. Follow the methodology and practical conclusions in the [writing evaluation](https://shengming-zhou.github.io/memfxxk/writing-evaluation/).
