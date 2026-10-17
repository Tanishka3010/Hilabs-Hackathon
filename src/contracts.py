import pandas as pd

RATES_LONG_COLUMNS = {
    "file_key": "string",
    "in_network_idx": "int64",
    "negotiation_arrangement": "string",
    "billing_code_type": "string",
    "billing_code_type_version": "string",
    "billing_code": "string",
    "name": "string",
    "rate_idx": "int64",
    "price_idx": "int64",
    "provider_group_id": "string",
    "provider_ref_resolved": "bool",
    "npi": "Int64",
    "tin_type": "string",
    "tin": "string",
    "negotiated_type": "string",
    "negotiated_rate": "float64",
    "expiration_date": "string",
    "billing_class": "string",
    "service_code": "string",
    "billing_code_modifier": "string",
    "additional_information": "string",
}

PROVIDER_REFS_COLUMNS = {
    "file_key": "string",
    "provider_group_id": "string",
    "npi": "Int64",
    "tin_type": "string",
    "tin": "string",
    "business_name": "string",
    "network_name": "string",
}

RATES_SELECTED_COLUMNS = {
    **{k: RATES_LONG_COLUMNS[k] for k in ("file_key", "billing_code_type", "billing_code", "npi", "tin", "billing_class", "service_code", "billing_code_modifier", "negotiated_type", "negotiated_rate")},
    "n_candidate_rates": "int64",
    "rate_min": "float64",
    "rate_max": "float64",
    "selection_rule": "string",
    "has_conflict": "bool",
}

RATES_COLUMNS = {
    **{k: RATES_LONG_COLUMNS[k] for k in ("file_key", "in_network_idx", "negotiation_arrangement", "billing_code_type", "billing_code_type_version",
                                          "billing_code", "name", "rate_idx", "price_idx", "provider_group_id", "negotiated_type",
                                          "negotiated_rate", "expiration_date", "billing_class")},
    "setting": "string",
    **{k: RATES_LONG_COLUMNS[k] for k in ("service_code", "billing_code_modifier", "additional_information")},
}

FILE_HEADER_KEYS = (
    "file_key", "file_url", "reporting_entity_name", "reporting_entity_type", "version", "last_updated_on",
    "bytes_read", "bytes_total", "complete", "n_provider_references", "n_in_network_items", "n_negotiated_rates",
    "n_negotiated_prices", "n_rows", "invalid_records",
)


def empty_frame(columns):
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in columns.items()})


def conform(df, columns):
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"missing contract columns: {missing}")
    return df[list(columns)].astype(columns)


def validate_header(header):
    missing = [k for k in FILE_HEADER_KEYS if k not in header]
    if missing:
        raise ValueError(f"missing file_header keys: {missing}")
    return header
