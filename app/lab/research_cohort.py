"""Local multi-run research export with a common chronological holdout."""

from collections.abc import Iterable

from app.lab.engine import INTERVALS, LabError
from app.lab.export_split import FOLLOWUP_SECONDS
from app.lab.store import content_hash


MAX_COHORT_RUNS = 8
MAX_COHORT_INPUT_CASES = 40000


def build_research_cohort(bundles: Iterable[dict], *, boundary: int) -> dict:
    """Merge only verified LAB exports. Deduplicate equivalent series/rule/time rows."""
    series, rules, sources, merged = {}, {}, {}, {}
    counts = dict(input_cases=0, incomplete=0, purged_boundary=0, duplicates=0,
                  development=0, validation=0)
    for bundle in bundles:
        if bundle["verification"]["verified"] is not True:
            raise LabError("Todas las consultas deben superar la verificación")
        dataset, run, rule = bundle["dataset"], bundle["run"], bundle["rule"]
        run_id = run["id"]
        market = {"symbol": dataset["symbol"], "timeframe": dataset["timeframe"],
                  "source_sha256": content_hash(dataset["source"])}
        series_id = content_hash(market)
        rule_hash = rule["hash"]
        step = INTERVALS[market["timeframe"]]
        series[series_id] = market
        rules[rule_hash] = rule["definition"]
        sources[run_id] = {
            "series_id": series_id, "rule_hash": rule_hash, "dataset_hash": dataset["hash"],
            "verified_public": dataset["verified_public"], "result_hash": run["result_hash"],
            "engine_version": run["engine_version"], "lab_engine_version": run["lab_engine_version"],
        }
        counts["input_cases"] += len(bundle["cases"])
        if counts["input_cases"] > MAX_COHORT_INPUT_CASES:
            raise LabError("El conjunto supera 40.000 casos. Selecciona menos consultas.")
        for case in bundle["cases"]:
            labels = case["future_labels"]
            if labels["full_7d_followup"] is not True:
                counts["incomplete"] += 1
                continue
            features = case["features_at_close"]
            signal = features["signal_time"]
            end = labels["followup_end_time"]
            if (type(signal) is not int or type(end) is not int or end - signal != FOLLOWUP_SECONDS
                    or labels["outcomes_pct"][str(FOLLOWUP_SECONDS)] is None):
                raise LabError("La etiqueta de siete días no coincide con su intervalo")
            label_available = end + step
            if signal < boundary <= label_available:
                counts["purged_boundary"] += 1
                continue
            phase = "development" if signal < boundary else "validation"
            key = signal, series_id, rule_hash
            value = {"series_id":series_id, "rule_hash":rule_hash, "features_at_close":features,
                     "future_labels":labels, "signal_known_at_t":signal+step,
                     "labels_known_at_t":label_available}
            if key in merged:
                previous, run_ids, _ = merged[key]
                if previous != value:
                    raise LabError("Dos consultas discrepan en un caso de la misma fuente, regla y vela")
                run_ids.append(run_id)
                counts["duplicates"] += 1
            else:
                merged[key] = (value, [run_id], phase)
                counts[phase] += 1
    if min(counts["development"], counts["validation"]) == 0:
        raise LabError("El corte UTC necesita casos completos a ambos lados tras aplicar la purga")
    partitions = {"development": [], "validation": []}
    for key in sorted(merged):
        row, run_ids, phase = merged[key]
        partitions[phase].append({**row, "run_ids": sorted(run_ids)})
    result = {"schema":"orbis.lab.research.cohort.v1", "environment":"LAB", "theoretical_only":True,
              "boundary_open_t":boundary, "horizon_seconds":FOLLOWUP_SECONDS,
              "series":series, "rules":rules, "sources":sources, "counts":counts, **partitions,
              "warnings":["Las ventanas dentro de cada partición pueden solaparse",
                          "La reserva depende de la fecha elegida y de las consultas seleccionadas"]}
    return {**result, "content_sha256":content_hash(result)}
