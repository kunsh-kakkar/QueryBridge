# Test data

| File | Contents |
|---|---|
| `test_inputs.csv` | The 14 test inputs (T1–T14) with their type and expected output |
| `test_results.md` | The test table with actual outputs and pass/fail, plus the score of each prompt version |
| `outputs/v1_config_A.jsonl` … `v4_config_C.jsonl` | Every test's raw result for each prompt version (SQL generated, SQL executed, status, AI calls, time) |
| `outputs/prompt_sample_runs.json` | The real sample runs used in the prompt sheets: the full prompt sent and the model's JSON answer for P1–P4 |

The gold-standard SQL for T1–T10, T13 and T14 comes from `evaluation/test_dataset.json` (IDs Q01–Q47); T11 (Hinglish) and
T12 (typos) were added for Milestone 2. Inputs are fictional; the database is the generated demo company database.
