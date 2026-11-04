#!/usr/bin/env python3
"""
Sentinel-2 Soil Salinity Screening System

Uses reference-based spectral classification.

The system compares new Sentinel-2 observations against known
low- and high-salinity reference locations.

IMPORTANT:
The output percentage is a spectral similarity score, NOT actual
soil salt concentration or a calibrated probability.

Actual soil salinity (ECe, dS/m) requires field measurements.

Requirements:
    pip install requests numpy rasterio

Environment:
    CDSE_CLIENT_ID
    CDSE_CLIENT_SECRET

Usage:
    python main.py predict --lat 30.05 --lon 31.25

    python main.py predict --lat 30.05 --lon 31.25 --buffer 100 --days 90
"""

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
from rasterio.io import MemoryFile


# ============================================================
# COPERNICUS CONFIGURATION
# ============================================================

TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/"
    "auth/realms/CDSE/protocol/openid-connect/token"
)

PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"


EVALSCRIPT = """//VERSION=3

function setup() {
    return {
        input: [
            "B02", "B03", "B04",
            "B08", "B11", "B12",
            "SCL", "dataMask"
        ],
        output: {
            bands: 8,
            sampleType: "FLOAT32"
        }
    };
}

function evaluatePixel(s) {
    return [
        s.B02,
        s.B03,
        s.B04,
        s.B08,
        s.B11,
        s.B12,
        s.SCL,
        s.dataMask
    ];
}
"""


INDEX_NAMES = ["NDSI", "SI", "SI2", "SWIR_ND"]


# ============================================================
# REFERENCE DATABASE
# ============================================================

# These are the initial reference measurements.
#
# LOW:
# 31.0, 30.9
#
# HIGH:
# 30.933668, 32.392993
# 30.974611, 32.457639
#
# IMPORTANT:
# These references are provisional.
# Add more independently verified measurements as they become available.

REFERENCES = {

    "low": [

        {
            "name": "Kafr El-Sheikh",
            "NDSI": -0.1781,
            "SI": 0.1156,
            "SI2": 0.2607,
            "SWIR_ND": 0.1247
        }

    ],

    "high": [

        {
            "name": "El-Tina H1",
            "NDSI": -0.1671,
            "SI": 0.1277,
            "SI2": 0.2626,
            "SWIR_ND": 0.1079
        },

        {
            "name": "El-Tina H2",
            "NDSI": -0.1820,
            "SI": 0.1697,
            "SI2": 0.3606,
            "SWIR_ND": 0.1510
        }

    ]

}


# ============================================================
# DISTANCE NORMALIZATION
# ============================================================

# Minimum standard deviations prevent individual indices from
# dominating the distance calculation due to small sample sizes.
#
# These are numerical safeguards, NOT scientifically established
# salinity thresholds. Update them as the reference dataset grows.

MIN_SCALES = {
    "NDSI": 0.03,
    "SI": 0.03,
    "SI2": 0.06,
    "SWIR_ND": 0.04
}


# ============================================================
# AUTHENTICATION
# ============================================================

def get_token():

    cid = os.environ.get("CDSE_CLIENT_ID")
    secret = os.environ.get("CDSE_CLIENT_SECRET")

    if not cid or not secret:
        sys.exit(
            "Set CDSE_CLIENT_ID and CDSE_CLIENT_SECRET."
        )

    try:

        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": secret
            },
            timeout=60
        )

        r.raise_for_status()

        return r.json()["access_token"]

    except requests.exceptions.RequestException as e:

        sys.exit(
            "\nCopernicus authentication failed.\n"
            "Check your internet connection and try again.\n\n"
            f"Details: {e}"
        )


# ============================================================
# FETCH SENTINEL-2 DATA
# ============================================================

def fetch_bands(
    lat,
    lon,
    token,
    buffer_m=100,
    days=90,
    max_cloud=30
):

    dlat = buffer_m / 111320.0

    dlon = buffer_m / (
        111320.0 * math.cos(math.radians(lat))
    )

    n = max(
        3,
        min(2500, int(2 * buffer_m / 10))
    )

    end = datetime.now(timezone.utc)

    start = end - timedelta(days=days)

    payload = {

        "input": {

            "bounds": {

                "bbox": [
                    lon - dlon,
                    lat - dlat,
                    lon + dlon,
                    lat + dlat
                ],

                "properties": {
                    "crs": "http://www.opengis.net/def/crs/EPSG/0/4326"
                }

            },

            "data": [

                {

                    "type": "sentinel-2-l2a",

                    "dataFilter": {

                        "timeRange": {

                            "from": start.strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            ),

                            "to": end.strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            )

                        },

                        "maxCloudCoverage": max_cloud,

                        "mosaickingOrder": "leastCC"

                    }

                }

            ]

        },

        "output": {

            "width": n,
            "height": n,

            "responses": [

                {

                    "identifier": "default",

                    "format": {
                        "type": "image/tiff"
                    }

                }

            ]

        },

        "evalscript": EVALSCRIPT

    }

    try:

        r = requests.post(
            PROCESS_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "image/tiff"
            },
            timeout=120
        )

        r.raise_for_status()

        with MemoryFile(r.content) as mem:

            with mem.open() as ds:

                return ds.read()

    except requests.exceptions.RequestException as e:

        sys.exit(
            "\nSentinel-2 data request failed.\n"
            "This may be a temporary Copernicus connectivity issue.\n\n"
            f"Details: {e}"
        )


# ============================================================
# COMPUTE SPECTRAL INDICES
# ============================================================

def compute_indices(arr):

    b02, b03, b04, b08, b11, b12, scl, mask = arr

    eps = 1e-9

    ndvi = (
        (b08 - b04) /
        (b08 + b04 + eps)
    )

    valid = (
        (mask == 1) &
        np.isin(scl, [4, 5])
    )

    bare = valid & (ndvi < 0.25)

    note = ""

    if bare.sum() >= 3:

        sel = bare

    elif valid.sum() >= 3:

        sel = valid

        note = (
            "Limited bare soil. "
            "Salinity classification is less reliable."
        )

    else:

        return None, (
            "No clear pixels found. "
            "Try increasing --days or --buffer."
        )

    ndsi = (
        (b04 - b08) /
        (b04 + b08 + eps)
    )

    si = np.sqrt(
        np.maximum(b03 * b04, 0)
    )

    si2 = np.sqrt(
        b03**2 + b04**2 + b08**2
    )

    swir_nd = (
        (b11 - b12) /
        (b11 + b12 + eps)
    )

    out = {

        "NDSI": float(np.median(ndsi[sel])),

        "SI": float(np.median(si[sel])),

        "SI2": float(np.median(si2[sel])),

        "SWIR_ND": float(np.median(swir_nd[sel])),

        "bare_soil_fraction": float(
            bare.sum() /
            (arr.shape[1] * arr.shape[2])
        ),

        "mean_NDVI": float(
            np.mean(ndvi[sel])
        )

    }

    return out, note


# ============================================================
# REFERENCE STATISTICS
# ============================================================

def get_reference_statistics():

    statistics = {}

    for group_name, samples in REFERENCES.items():

        if not samples:

            raise ValueError(
                f"No reference samples for {group_name}"
            )

        statistics[group_name] = {}

        for index in INDEX_NAMES:

            values = np.array(
                [sample[index] for sample in samples],
                dtype=float
            )

            statistics[group_name][index] = {

                "mean": float(np.mean(values)),

                "std": float(
                    np.std(values, ddof=1)
                    if len(values) > 1
                    else 0.0
                ),

                "count": len(values)

            }

    return statistics


# ============================================================
# CALCULATE FEATURE SCALES
# ============================================================

def get_feature_scales():

    scales = {}

    all_samples = (
        REFERENCES["low"] +
        REFERENCES["high"]
    )

    for index in INDEX_NAMES:

        values = np.array(
            [sample[index] for sample in all_samples],
            dtype=float
        )

        observed_std = float(
            np.std(values, ddof=1)
        ) if len(values) > 1 else 0.0

        scales[index] = max(
            observed_std,
            MIN_SCALES[index]
        )

    return scales


# ============================================================
# STANDARDIZED EUCLIDEAN DISTANCE
# ============================================================

def calculate_distance(
    location,
    reference_mean,
    scales
):

    total = 0.0

    for index in INDEX_NAMES:

        difference = (
            location[index] -
            reference_mean[index]
        )

        normalized = (
            difference / scales[index]
        )

        total += normalized ** 2

    return math.sqrt(total)


# ============================================================
# SALINITY SIMILARITY CLASSIFICATION
# ============================================================

def classify_salinity(indices):

    statistics = get_reference_statistics()

    scales = get_feature_scales()

    low_mean = {
        index: statistics["low"][index]["mean"]
        for index in INDEX_NAMES
    }

    high_mean = {
        index: statistics["high"][index]["mean"]
        for index in INDEX_NAMES
    }

    d_low = calculate_distance(
        indices,
        low_mean,
        scales
    )

    d_high = calculate_distance(
        indices,
        high_mean,
        scales
    )

    denominator = d_low + d_high

    if denominator < 1e-12:

        score = 50.0

    else:

        score = (
            d_low / denominator
        ) * 100.0

    # Classification thresholds are provisional.
    # They represent relative similarity, not measured EC.

    if score < 25:

        classification = (
            "Strong low-salinity spectral similarity"
        )

    elif score < 40:

        classification = (
            "Leaning toward low salinity"
        )

    elif score < 60:

        classification = (
            "Intermediate / uncertain"
        )

    elif score < 75:

        classification = (
            "Leaning toward high salinity"
        )

    else:

        classification = (
            "Strong high-salinity spectral similarity"
        )

    return {

        "score": score,

        "classification": classification,

        "distance_low": d_low,

        "distance_high": d_high,

        "low_reference": low_mean,

        "high_reference": high_mean,

        "scales": scales

    }


# ============================================================
# DISPLAY RESULTS
# ============================================================

def cmd_predict(args):

    lat = args.lat
    lon = args.lon

    if lat is None or lon is None:

        lat = float(
            input("Latitude: ")
        )

        lon = float(
            input("Longitude: ")
        )

    if not -90 <= lat <= 90:

        sys.exit("Invalid latitude.")

    if not -180 <= lon <= 180:

        sys.exit("Invalid longitude.")

    print("\nConnecting to Copernicus...")

    token = get_token()

    arr = fetch_bands(
        lat,
        lon,
        token,
        args.buffer,
        args.days
    )

    indices, note = compute_indices(arr)

    if indices is None:

        sys.exit(note)

    result = classify_salinity(indices)

    print("\n" + "=" * 55)

    print("SENTINEL-2 SOIL SALINITY SCREENING")

    print("=" * 55)

    print(f"\nLocation: {lat}, {lon}")

    print(
        f"Area: approximately {2 * args.buffer} m wide"
    )

    print(
        f"Observation period: {args.days} days"
    )

    print("\nSPECTRAL INDICES")

    for index in INDEX_NAMES:

        print(
            f"{index:12s} = {indices[index]:.4f}"
        )

    print(
        f"{'Bare soil':12s} = "
        f"{indices['bare_soil_fraction']:.1%}"
    )

    print(
        f"{'Mean NDVI':12s} = "
        f"{indices['mean_NDVI']:.4f}"
    )

    if note:

        print("\nNOTE:", note)

    print("\n" + "=" * 55)

    print("REFERENCE-BASED CLASSIFICATION")

    print("=" * 55)

    print(
        f"\nDistance to low-salinity reference: "
        f"{result['distance_low']:.4f}"
    )

    print(
        f"Distance to high-salinity reference: "
        f"{result['distance_high']:.4f}"
    )

    print("\nRESULT")

    print("-" * 55)

    print(
        f"High-salinity similarity: "
        f"{result['score']:.1f}%"
    )

    print(
        f"Classification: {result['classification']}"
    )

    # Data quality warning

    bare = indices["bare_soil_fraction"]

    print("\nDATA RELIABILITY")

    print("-" * 55)

    if bare >= 0.70:

        print("Good bare-soil coverage.")

    elif bare >= 0.40:

        print("Moderate bare-soil coverage.")

    else:

        print(
            "Low bare-soil coverage. "
            "Classification may be unreliable."
        )

    print("\nREFERENCE DATA")

    print("-" * 55)

    print(
        f"Low-salinity samples: "
        f"{len(REFERENCES['low'])}"
    )

    print(
        f"High-salinity samples: "
        f"{len(REFERENCES['high'])}"
    )

    print("\nREFERENCE AVERAGES")

    stats = get_reference_statistics()

    print(
        f"{'INDEX':12s} "
        f"{'LOW':>12s} "
        f"{'HIGH':>12s}"
    )

    for index in INDEX_NAMES:

        low = stats["low"][index]["mean"]

        high = stats["high"][index]["mean"]

        print(
            f"{index:12s} "
            f"{low:12.4f} "
            f"{high:12.4f}"
        )

    print("\n" + "=" * 55)

    print(
        "IMPORTANT: The percentage represents spectral "
        "similarity to the high-salinity reference group."
    )

    print(
        "It is NOT actual soil salinity, ECe, or a "
        "calibrated probability."
    )

    print("=" * 55)


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="Sentinel-2 soil salinity screening"
    )

    sub = parser.add_subparsers(
        dest="cmd",
        required=True
    )

    predict = sub.add_parser("predict")

    predict.add_argument(
        "--lat",
        type=float
    )

    predict.add_argument(
        "--lon",
        type=float
    )

    predict.add_argument(
        "--buffer",
        type=int,
        default=100
    )

    predict.add_argument(
        "--days",
        type=int,
        default=90
    )

    predict.set_defaults(
        fn=cmd_predict
    )

    args = parser.parse_args()

    args.fn(args)


if __name__ == "__main__":

    main()
