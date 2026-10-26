PY ?= python3

.PHONY: setup data run queries report test demo-dq sample scale clean all

all: data run queries report test

setup:
	$(PY) -m pip install -r requirements.txt

data:                       ## clean synthetic data -> data/
	$(PY) data_generator.py

run:                        ## bronze -> silver -> gold -> Parquet
	$(PY) -m pipeline.run

queries:                    ## run queries/sample_queries.sql and print results
	$(PY) scripts/run_sample_queries.py

report:                     ## self-contained HTML report with charts -> reports/index.html
	$(PY) scripts/build_report.py

test:
	$(PY) -m pytest -q

demo-dq:                    ## inject ~1% bad data and watch the quality layer work
	$(PY) data_generator.py --dirty-rate 0.01 --output-dir data_dirty
	$(PY) -m pipeline.run --source-dir data_dirty --db warehouse/dirty_demo.duckdb --no-export
	$(PY) scripts/run_sample_queries.py --db warehouse/dirty_demo.duckdb --only Q18,Q19
	@echo "\nNow with a tighter circuit breaker (expect exit code 2):"
	-$(PY) -m pipeline.run --source-dir data_dirty --db warehouse/breaker_demo.duckdb --no-export --max-reject-rate 0.02

sample:                     ## run the pipeline on the challenge's own SQLite file (adapter)
	$(PY) -m pipeline.run --sqlite sample_data/Carton_Caps_Data.sqlite --db warehouse/sample.duckdb --no-export

scale:                      ## 100x volume: 100k users, 1M purchases, 100k referrals
	$(PY) data_generator.py --users 100000 --schools 500 --purchases 1000000 --referrals 100000 --output-dir data_scale
	$(PY) -m pipeline.run --source-dir data_scale --db warehouse/scale.duckdb --export-dir warehouse/export_scale

clean:
	rm -rf warehouse data_dirty data_scale .pytest_cache
