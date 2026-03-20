Pre-handoff validation checklist. Run this before declaring a task complete.

## Prerequisites

1. Load `/mcp-tools-reference` if this is the first MCP call in this session

## Validation Steps

2. **Snapshot performance**: Call `get_project_performance` with `include_hotspots=5` and record the result

3. **Check for errors**: Call `get_op_errors` with `recurse=true` on `/` — report any errors or warnings found

4. **Check externalization health**: Call `get_externalizations` — flag any dirty (unsaved) operators

5. **Evaluate performance metrics** against these thresholds:

   | Metric | Condition | Severity |
   |--------|-----------|----------|
   | FPS | Below `cookRate` by more than 10% | WARNING |
   | `droppedFrames` | Greater than 0 | NOTE (cumulative counter — compare against baseline if available) |
   | `gpuMemUsedMB` | Above 80% of `totalGpuMemMB` | WARNING |
   | `cpuMemUsedMB` | Above 4000 MB | NOTE |
   | `gpuTemp` | Above 85°C | WARNING |

6. **Hotspot analysis**: If any of the top 5 COMPs have `combinedCookTimeMs` above 2.0 ms, report them as potential bottlenecks

7. **Check logs**: Read `dev/logs/` for recent ERROR or WARNING entries since the task began

## Verdict

8. Report a verdict based on the findings:

   - **PASS**: No errors, no warnings, performance nominal
   - **WARN**: No errors but performance warnings or dirty externalizations present
   - **FAIL**: Errors present in the network

   Include a summary table of all metrics with pass/warn/fail status for each.

## Baseline Mode

If $ARGUMENTS contains "baseline", take a performance snapshot only and report the values. Do not run the full checklist — this captures a reference point for later comparison.
