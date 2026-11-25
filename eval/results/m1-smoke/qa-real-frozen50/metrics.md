# LongMemEval QA

Judge: builtin(gpt-5.4-mini)

| Group | Accuracy | Correct abstention | Answer session found, answer wrong |
| --- | --- | --- | --- |
| G1 | 12.0% (50) | 100.0% (4) | — |
| recent-turns | 8.0% (50) | 100.0% (4) | 100.0% (1) |
| memfxxk-hybrid | 30.0% (50) | 100.0% (4) | 35.0% (20) |
| mem0-raw | 22.0% (50) | 100.0% (4) | 57.9% (19) |

| Group | Question type | Accuracy |
| --- | --- | --- |
| G1 | knowledge-update | 11.1% (9) |
| G1 | multi-session | 12.5% (8) |
| G1 | single-session-assistant | 12.5% (8) |
| G1 | single-session-preference | 12.5% (8) |
| G1 | single-session-user | 12.5% (8) |
| G1 | temporal-reasoning | 11.1% (9) |
| recent-turns | knowledge-update | 11.1% (9) |
| recent-turns | multi-session | 12.5% (8) |
| recent-turns | single-session-assistant | 0.0% (8) |
| recent-turns | single-session-preference | 0.0% (8) |
| recent-turns | single-session-user | 12.5% (8) |
| recent-turns | temporal-reasoning | 11.1% (9) |
| memfxxk-hybrid | knowledge-update | 33.3% (9) |
| memfxxk-hybrid | multi-session | 12.5% (8) |
| memfxxk-hybrid | single-session-assistant | 50.0% (8) |
| memfxxk-hybrid | single-session-preference | 12.5% (8) |
| memfxxk-hybrid | single-session-user | 37.5% (8) |
| memfxxk-hybrid | temporal-reasoning | 33.3% (9) |
| mem0-raw | knowledge-update | 11.1% (9) |
| mem0-raw | multi-session | 12.5% (8) |
| mem0-raw | single-session-assistant | 37.5% (8) |
| mem0-raw | single-session-preference | 12.5% (8) |
| mem0-raw | single-session-user | 37.5% (8) |
| mem0-raw | temporal-reasoning | 22.2% (9) |
