"""nfl-ngs-data -- reshape the nfl-ngs-raw JSON tree into the released nfl_ngs_* datasets.

Reads the raw tree over ``raw.githubusercontent.com`` (per-file GETs enumerated
from the raw repo's own schedule parquet -- never a clone, never a directory
listing) and publishes per-season parquet + csv to ``sportsdataverse-data``
release tags. Layer-per-module: ``ingest`` (read), ``reshapers`` (json -> tidy),
``build`` (per-season driver), ``io`` (write), ``publish`` (gh release upload),
``config`` (the dataset registry).
"""
