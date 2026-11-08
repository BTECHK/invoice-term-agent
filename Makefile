PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
OUTPUT ?= build/demo-output
RUN = PYTHONPATH=src AP_AGENT_ROOT="$(CURDIR)" "$(PYTHON)"

.PHONY: read demo test check eval package

read:
	$(RUN) -m invoice_agent.cli read --inputs evals/golden/invoices evals/golden/injection --out transcripts

demo:
	$(RUN) -m invoice_agent.cli --output "$(OUTPUT)"

test:
	$(RUN) -m unittest discover -s tests -p 'test_*.py'

check:
	$(RUN) scripts/check.py
	$(RUN) scripts/smoke_mcp.py

eval: demo
	$(RUN) evals/grade.py "$(OUTPUT)/results.jsonl"

package:
	$(RUN) scripts/package_release.py --output dist/invoice-term-agent.zip
