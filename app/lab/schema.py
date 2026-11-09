"""LAB schema compatibility for readers that attach its legacy dataset tables.

Adding a schema requires checking that those tables keep their published shape;
new financial engines/data formats still need their own explicit version checks.
"""
CURRENT_SCHEMA_VERSION = 15
DATASET_READ_VERSIONS = frozenset({6, 7, 8, 9, 10, 11, 12, 13, 14, 15})
