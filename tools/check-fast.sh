#!/usr/bin/env bash
# Offline smoke check. Requires Node 22+, npm ci in workers/, Python 3 and Ruff 0.17.6.
# Ruff 0.17.6 was tested; Ruff 0.14.14 reports F821 in rehearse_decisions.py.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

npm --prefix workers run typecheck
ruff check --select E9,F63,F7,F82 campaign_tool

# unittest exits nonzero for a failed or missing named test. Also require
# four actual passes: a smaller selection or a skip is not a passing gate.
# PYTHON_COLORS=0 takes precedence over FORCE_COLOR in Python 3.14; any
# escape codes still printed are stripped before the summary is matched.
status=0
output=$(PYTHON_COLORS=0 NO_COLOR=1 python3 -B -m unittest \
  tests.records.test_console_home.Home.test_newest_counts_links_metadata_and_privacy \
  tests.records.test_console_health.HealthPage.test_health_verdict_and_failed_source_isolation \
  tests.records.test_console_check.ConsoleCheckTests.test_checks_report_only_codes_and_refuse_unsafe_targets \
  tests.records.test_console_documents.ScreenC.test_panel_route 2>&1) || status=$?
printf '%s\n' "$output"
if (( status != 0 )); then
  exit "$status"
fi
plain=$(printf '%s\n' "$output" | sed -E $'s/\x1b\\[[0-9;]*[A-Za-z]//g')
report=$'\nRan 4 tests in [0-9]+(\\.[0-9]+)?s\n\nOK$'
if [[ ! $plain =~ $report ]]; then
  printf 'Expected exactly 4 passing tests with no skips or expected failures.\n' >&2
  exit 1
fi
