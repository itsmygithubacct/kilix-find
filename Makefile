PYTHON ?= python3

SOURCES := $(shell find src -name '*.py')
TESTS := $(wildcard tests/*.py)

.DEFAULT_GOAL := help

.PHONY: all help check compile test clean

# `all` is what a content-catalog install runs. It builds; it does not test.
all: compile
	@test -x ./kilix-find || { echo 'kilix-find launcher is not executable'; exit 1; }

help:
	@printf '%s\n' \
		'kilix-find' \
		'' \
		'  make all      compile (what an installer runs)' \
		'  make check    compile and run the suite' \
		'  make test     run the suite' \
		'  make clean    remove bytecode'

compile:
	$(PYTHON) -m compileall -q src tests kilix-find

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

check: compile test

clean:
	find src tests -name '__pycache__' -type d -exec rm -rf {} +
	find src tests -name '*.pyc' -delete
