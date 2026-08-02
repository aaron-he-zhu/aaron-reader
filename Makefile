.PHONY: sync status test doctor render

sync:
	./aaron-reader sync

status:
	./aaron-reader status

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

doctor:
	./aaron-reader doctor --live

render:
	./aaron-reader render
