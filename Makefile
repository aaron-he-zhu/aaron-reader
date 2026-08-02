.PHONY: sync status test doctor render serve

sync:
	./aaron-reader sync

status:
	./aaron-reader status

test:
	PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests -v

doctor:
	./aaron-reader doctor --live

render:
	./aaron-reader render

serve:
	./aaron-reader serve --open
