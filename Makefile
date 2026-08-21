.PHONY: install demo real-demo validate test acceptance agent-acceptance clean-demo

install:
	python3 -m pip install -e .

demo:
	python3 scripts/create_examples.py examples/generated

real-demo:
	python3 -m dadc ingest-touchstone tests/fixtures/bandpass_filter_run_001_HFSSDesign1.s2p examples/real_hfss --case-id hfss_bandpass_real_001 --device-name "HFSS official interdigital bandpass filter" --filter-order 8 --source-timezone +08:00

validate:
	python3 -m dadc validate examples/generated

test:
	python3 -m unittest discover -s tests -v

agent-acceptance:
	python3 scripts/run_agent_tuning_acceptance.py --output-dir test-results/agent-tuning-acceptance

acceptance: demo test validate agent-acceptance

clean-demo:
	python3 scripts/create_examples.py examples/generated --replace
