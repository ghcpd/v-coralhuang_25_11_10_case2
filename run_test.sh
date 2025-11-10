#!/usr/bin/env bash
python -m pytest -q | tee -a logs/test_run.log
